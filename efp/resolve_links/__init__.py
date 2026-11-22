"""resolve_links component (build 06): every URL in a row's Source cell, resolved and captured.

``run`` parses ``rows.source_raw`` into URLs, writes one ``links`` row per URL (a row with no URL
gets one ``no_url`` row, so the link-less count is in the data), and walks each unresolved link
down the module ladder in ``[stages.resolve_links]``. A row is ``done`` when every link has a
terminal status; ``failed`` only on an internal error. Links already ``live`` or ``archived`` are
never fetched again; ``dead`` and ``unresolved`` ones are re-attempted on a re-run, which is cheap
(the HTTP cache) and is how a newly enabled module reaches the links it can now help.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from typing import Any

from efp import modules as chain
from efp.modules import REGISTRY, Registry
from efp.resolve_links import modules as ladder
from efp.resolve_links.parse import ParsedUrl, parse_source_cell
from efp.runner import input_hash as hash_values
from efp.types import Context, Row, StageResult

BUILD = "06"
IMPLEMENTED = True
TOOL_VERSION = "1"
STAGE = "resolve_links"
NO_URL = "no_url"

log = logging.getLogger(__name__)


def gate(row: Row) -> bool | str:
    """Every in-scope row enters: a row with no URL is recorded as such, not skipped."""
    return True


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _available_modules(ctx: Context, registry: Registry) -> list[str]:
    return sorted(m.name for m in chain.describe(ctx.config, ctx, registry, STAGE) if m.ok)


def input_hash(row: Row, ctx: Context, registry: Registry = REGISTRY) -> str:
    """The Source cell plus the modules that can run: enabling one changes the hash, so
    ``efp run resolve_links --recheck`` revisits exactly the rows it may now improve."""
    return hash_values(row.source.get("source_raw"), _available_modules(ctx, registry))


def _ensure_links(conn: sqlite3.Connection, row: Row, parsed: list[ParsedUrl]) -> None:
    if not parsed:
        cell = str(row.source.get("source_raw") or "").strip()
        conn.execute(
            "INSERT OR IGNORE INTO links (row_id, url_raw, expansion_method, status, resolved_at, "
            "note) VALUES (?, ?, 'unresolved', 'unresolved', ?, ?)",
            (row.row_id, cell, _now(), NO_URL),
        )
        return
    for url in parsed:
        conn.execute(
            "INSERT OR IGNORE INTO links (row_id, url_raw, status) VALUES (?, ?, 'unresolved')",
            (row.row_id, url.raw),
        )


def _task(row: Row, link_id: int, url: ParsedUrl, state: ladder.LinkState) -> ladder.LinkTask:
    return ladder.LinkTask(
        row_id=row.row_id,
        year=row.year,
        state=row.state,  # (the Row's own "state" field is the US state)
        source=row.source,
        link_id=link_id,
        url_raw=url.raw,
        fetch_url=url.fetch_url,
        has_scheme=url.has_scheme,
        host=url.host,
        is_shortener=url.is_shortener,
        ladder=state,
    )


def resolve_row(row: Row, ctx: Context, registry: Registry = REGISTRY) -> StageResult:
    """Resolve every pending link of one row and say what happened."""
    conn = ctx.db
    if conn is None:
        raise RuntimeError("resolve_links needs the database connection in ctx.db")
    parsed = parse_source_cell(str(row.source.get("source_raw") or ""))
    _ensure_links(conn, row, parsed)
    config_chain = chain.chains_for(ctx.config, STAGE)[0]
    summary: dict[str, Any] = {"no_source": not parsed, "links": []}
    for link_id, url_raw, status, resolved_at, note in conn.execute(
        "SELECT link_id, url_raw, status, resolved_at, note FROM links WHERE row_id = ? "
        "ORDER BY link_id",
        (row.row_id,),
    ).fetchall():
        if note == NO_URL:
            continue
        if resolved_at is not None and status in ("live", "archived"):
            summary["links"].append(
                {"link_id": link_id, "url": url_raw, "status": status, "kept": True}
            )
            continue
        found = parse_source_cell(url_raw)
        if not found:
            continue
        task_state = ladder.LinkState()
        task = _task(row, link_id, found[0], task_state)
        run = chain.run_chain(config_chain, task, ctx, registry)
        status_out, record = _finalise(conn, task, task_state)
        summary["links"].append(
            {
                "link_id": link_id,
                "url": url_raw,
                "status": status_out,
                **record,
                "modules": run.records,
            }
        )
    return StageResult(
        row_id=row.row_id,
        stage=STAGE,
        status="done",
        input_hash=input_hash(row, ctx, registry),
        tool_version=TOOL_VERSION,
        summary_json=summary,
    )


def _finalise(
    conn: sqlite3.Connection, task: ladder.LinkTask, st: ladder.LinkState
) -> tuple[str, dict[str, Any]]:
    """Turn what the ladder learned into the link's terminal status, and write it."""
    expansion = st.expansion or (None if task.is_shortener else task.fetch_url)
    if st.status in ("live", "archived"):
        status, capture_id = st.status, st.capture_id
    elif expansion:
        status, capture_id = "dead", st.evidence_capture_id
    else:
        status, capture_id = "unresolved", st.evidence_capture_id
    method = st.method or ("live" if expansion else "unresolved")
    note = "; ".join(dict.fromkeys(st.notes))[:500] or None
    conn.execute(
        "UPDATE links SET url_expanded = ?, expansion_method = ?, status = ?, capture_id = ?, "
        "resolved_at = ?, note = ? WHERE link_id = ?",
        (expansion, method, status, capture_id, _now(), note, task.link_id),
    )
    return status, {"method": method, "expansion": expansion, "capture_id": capture_id}


def run(batch: list[Row], ctx: Context) -> list[StageResult]:
    results: list[StageResult] = []
    for row in batch:
        try:
            results.append(resolve_row(row, ctx))
        except Exception as exc:  # an internal error fails this row, never the batch
            log.exception("resolve_links failed for row %s", row.row_id)
            results.append(
                StageResult(
                    row_id=row.row_id,
                    stage=STAGE,
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}"[:300],
                )
            )
    return results

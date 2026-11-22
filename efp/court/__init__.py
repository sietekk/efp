"""court component (build 07): locate the primary record for a row.

``run`` classifies the row (federal, state or unknown), then walks the module chain in
``[stages.court]`` (DOJ press releases, the PIN reports, state AG, the CourtListener bulk index,
the CourtListener API, RECAP, state dockets) under ``first_hit``: the chain stops at the first
module that yields a document with a case number. Every attempt, hit or miss, is in
``summary_json.modules``; ``court_records.strategy`` names the module that found each document.

The stage is ``done`` when every applicable strategy has been attempted, including when nothing
was found (the row's ``court_status`` is then ``not_found``); it is ``failed`` only on an internal
error. Modules that later builds implement are registered unavailable, so the chain gains them
without a change here.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from typing import Any

from efp import modules as chain
from efp.court import courtlistener, doj
from efp.court.bulk import index_info
from efp.court.classify import classify, link_hosts
from efp.court.identifiers import extract_identifiers
from efp.court.located import CaseState, CourtTask, Located
from efp.court.names import parse_name
from efp.court.recap import QueueItem, append_queue
from efp.modules import REGISTRY, Registry
from efp.runner import input_hash as hash_values
from efp.types import Context, Row, StageResult

BUILD = "07"
IMPLEMENTED = True
TOOL_VERSION = "1"
STAGE = "court"
DOCKET_KINDS = ("federal_docket", "state_docket")

log = logging.getLogger(__name__)


def gate(row: Row) -> bool | str:
    """Every in-scope row enters; whether federal strategies apply is decided inside."""
    return True


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _available(ctx: Context, registry: Registry) -> list[str]:
    return sorted(m.name for m in chain.describe(ctx.config, ctx, registry, STAGE) if m.ok)


def _doj_freshness(row: Row, ctx: Context, hosts: list[str]) -> str | None:
    """What of the DOJ feed's fresh end can change this row's answer.

    Only a row the DOJ module can apply to depends on the feed at all, and a new release changes
    its answer only while the feed's newest date is still inside the row's release window: once
    the newest release is past the window's end, later releases cannot fall in it. So the date is
    part of the hash for those rows only, and it stops mattering ("past") once it leaves the
    window, instead of making every court row look changed whenever the feed moves."""
    slack = ctx.config.court.doj_year_slack
    if not row.year or row.year + slack < doj.COVERAGE_FROM:
        return None
    classification = classify(row.source.get("details"), row.source.get("source_raw"), hosts)
    if not classification.federal_strategies_apply:
        return None
    if not parse_name(str(row.source.get("name") or "")).usable:
        return None
    newest = doj.newest_indexed(ctx.config)
    if newest is None:
        return "no-index"
    return "past" if int(newest[:4]) > row.year + slack else newest


def input_hash(row: Row, ctx: Context, registry: Registry = REGISTRY) -> str:
    """The row's inputs, the modules that can run, and the bulk dataset: a new quarter or a newly
    enabled module changes it, so ``--recheck`` revisits exactly the rows that could change."""
    info = index_info(ctx.config)
    hosts = link_hosts(ctx.db, row.row_id) if ctx.db is not None else []
    return hash_values(
        row.source.get("name"),
        row.year,
        row.state,
        row.source.get("details"),
        row.source.get("source_raw"),
        hosts,
        _available(ctx, registry),
        info.dataset_date if info else None,
        ctx.config.court.pin_report_urls,
        _doj_freshness(row, ctx, hosts),
    )


def court_status(
    located: list[Located],
    *,
    quota_exhausted: bool = False,
    errored: bool = False,
    coverage_gap: bool = False,
) -> str:
    """``primary_record`` needs a docket that is not weak; weak dockets are ``candidate_only``.
    A row whose search could not be completed (a module failed, the API budget ran out, the DOJ
    harvest did not reach its years) is ``deferred_error`` / ``deferred_quota`` /
    ``deferred_coverage`` even when a press release was found: the docket search is unfinished, so
    it is not counted as a finished ``press_release_only``. None of them is a negative result."""
    dockets = [item for item in located if item.source_kind in DOCKET_KINDS]
    if any(not item.weak for item in dockets):
        return "primary_record"
    if dockets:
        return "candidate_only"
    if errored:
        return "deferred_error"
    if quota_exhausted:
        return "deferred_quota"
    if coverage_gap:
        return "deferred_coverage"  # the DOJ harvest did not reach the row's years
    if any(not item.weak for item in located):
        return "press_release_only"  # a press release (or other record) that is not a candidate
    # only weak records (a release whose state is not corroborated, a report paragraph): a
    # candidate, not a finished press-release result
    return "candidate_only" if located else "not_found"


def _identity(conn: sqlite3.Connection, item: Located) -> str:
    """What makes a non-docket record the same record on a later run.

    The release's own id when the module gives one (a page's markup, and above all a listing
    response, changes between fetches); otherwise the content hash of the extracted text, then of
    the capture (a PDF is content-addressed and stable)."""
    release = item.basis.get("release_uuid")
    if release:
        return f"uuid:{release}"
    for label, capture_id in (("text", item.text_capture_id), ("capture", item.capture_id)):
        if capture_id is not None:
            held = conn.execute(
                "SELECT content_sha256 FROM captures WHERE capture_id = ?", (capture_id,)
            ).fetchone()
            if held:
                return f"{label}:{held[0]}"
    return f"url:{item.url or ''}:{item.case_number or ''}"


def _upsert_record(conn: sqlite3.Connection, row_id: int, item: Located) -> int:
    """Insert the record, or update it if this document was recorded before. Returns its id.

    A docket is identified by its docket id (a search response changes from run to run; the
    docket does not), anything else by :func:`_identity`, kept in ``basis_json``. When the same
    document is found again its weak flag, evidence and capture are brought up to date: a
    candidate that was confirmed last quarter and is ambiguous now must not stay marked as
    confirmed."""
    kind = item.document_kind or ""
    basis_dict = dict(item.basis)
    if item.document_kind == "docket" and item.docket_id is not None:
        found = conn.execute(
            "SELECT record_id FROM court_records WHERE row_id = ? AND source_kind = ? "
            "AND strategy = ? AND coalesce(document_kind, '') = ? AND docket_id = ? "
            "ORDER BY record_id LIMIT 1",
            (row_id, item.source_kind, item.strategy, kind, item.docket_id),
        ).fetchone()
    else:
        key = _identity(conn, item)
        basis_dict["identity"] = key
        found = conn.execute(
            "SELECT record_id FROM court_records WHERE row_id = ? AND source_kind = ? "
            "AND strategy = ? AND coalesce(document_kind, '') = ? "
            "AND json_extract(basis_json, '$.identity') = ? ORDER BY record_id LIMIT 1",
            (row_id, item.source_kind, item.strategy, kind, key),
        ).fetchone()
    basis = json.dumps(basis_dict, sort_keys=True, default=str)
    if found is not None:
        conn.execute(
            "UPDATE court_records SET weak = ?, basis_json = ?, capture_id = ?, "
            "text_capture_id = ?, court = ?, "
            "jurisdiction = ?, case_number = ?, current = 1 WHERE record_id = ?",
            (
                int(item.weak),
                basis,
                item.capture_id,
                item.text_capture_id,
                item.court,
                item.jurisdiction,
                item.case_number,
                found[0],
            ),
        )
        return int(found[0])
    cursor = conn.execute(
        "INSERT INTO court_records (row_id, source_kind, jurisdiction, court, case_number, "
        "document_kind, strategy, capture_id, found_at, weak, basis_json, docket_id, current, "
        "text_capture_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
        (
            row_id,
            item.source_kind,
            item.jurisdiction,
            item.court,
            item.case_number,
            item.document_kind,
            item.strategy,
            item.capture_id,
            _now(),
            int(item.weak),
            basis,
            item.docket_id,
            item.text_capture_id,
        ),
    )
    return int(cursor.lastrowid or 0)


def _supersede(conn: sqlite3.Connection, row_id: int, evaluated: set[str], seen: set[int]) -> int:
    """Mark records no longer current: a strategy that ran this time and did not find them again.

    A strategy that was skipped or failed this time says nothing about its earlier records, and a
    filing (a PDF captured from the RECAP archive) is kept: its absence may only mean the archive
    was not asked (quota, token)."""
    if not evaluated:
        return 0
    marks = ",".join("?" * len(evaluated))
    keep = f" AND record_id NOT IN ({','.join('?' * len(seen))})" if seen else ""
    cursor = conn.execute(
        "UPDATE court_records SET current = 0 WHERE row_id = ? AND current = 1 "  # noqa: S608
        f"AND strategy IN ({marks}) AND coalesce(document_kind, '') != 'filing'{keep}",
        [row_id, *sorted(evaluated), *sorted(seen)],
    )
    return int(cursor.rowcount)


def _insert_identifiers(conn: sqlite3.Connection, row_id: int, state: CaseState) -> dict[str, int]:
    """Identifiers read from the located documents, each with its verbatim span.

    A weak record (state not corroborated, an initial-only match, several candidates) may be about
    someone else, so it yields no person-level fact (age, city, birth date, office, employer,
    party); only its case number, which names the document and not the person, is kept."""
    counts: dict[str, int] = {}
    for item in state.located:
        if not item.text or item.text_capture_id is None:
            continue
        for found in extract_identifiers(item.text, state.parts):
            if item.weak and found.kind != "case_number":
                continue
            seen = conn.execute(
                "SELECT 1 FROM identifiers WHERE row_id = ? AND kind = ? AND value = ? "
                "AND capture_id = ? LIMIT 1",
                (row_id, found.kind, found.value, item.text_capture_id),
            ).fetchone()
            if seen is not None:
                continue
            conn.execute(
                "INSERT INTO identifiers (row_id, kind, value, capture_id, span) "
                "VALUES (?, ?, ?, ?, ?)",
                (row_id, found.kind, found.value, item.text_capture_id, found.span),
            )
            counts[found.kind] = counts.get(found.kind, 0) + 1
    return counts


def _queue_items(task: CourtTask, recap: dict[int, dict[str, Any]]) -> list[QueueItem]:
    """Federal dockets whose filings are not in RECAP, for the operator's PACER decision."""
    items: list[QueueItem] = []
    for item in task.cs.located:
        if item.source_kind != "federal_docket" or item.document_kind != "docket":
            continue
        got = recap.get(item.docket_id or -1, {})
        if got.get("documents") or got.get("transient") or got.get("skipped") or item.weak:
            continue  # found, or not known (retried later), or not a confirmed docket
        reason = got.get("reason") or "documents not found in the RECAP archive"
        items.append(
            QueueItem(task.row_id, item.court or "", item.case_number or "", item.docket_id, reason)
        )
    return items


def process_row(row: Row, ctx: Context, registry: Registry = REGISTRY) -> StageResult:
    conn = ctx.db
    if conn is None:
        raise RuntimeError("the court stage needs the database connection in ctx.db")
    parts = parse_name(str(row.source.get("name") or ""))
    classification = classify(
        row.source.get("details"), row.source.get("source_raw"), link_hosts(conn, row.row_id)
    )
    state = CaseState(classification, parts)
    task = CourtTask(
        row_id=row.row_id, year=row.year, state=row.state, source=row.source, case=state
    )
    spec = chain.chains_for(ctx.config, STAGE)[0]
    run = chain.run_chain(spec, task, ctx, registry)

    recap: dict[int, dict[str, Any]] = {}
    found_dockets = [i for i in state.located if i.source_kind == "federal_docket" and i.docket_id]
    # A weak candidate is not worth an API call and is never queued for purchase: the documents of
    # a docket that may be someone else's are not documents we want to buy.
    dockets = [i for i in found_dockets if not i.weak]
    for weak in (i for i in found_dockets if i.weak):
        recap[weak.docket_id or -1] = {
            "checked": False,
            "documents": 0,
            "reason": "weak candidate: its documents are not looked up or queued",
            "skipped": True,
        }
    recap_transient = False
    for docket in dockets[:1]:  # one docket per row: the best candidate
        outcome = courtlistener.capture_recap_documents(ctx, task, docket)
        recap[docket.docket_id or -1] = {
            "checked": outcome.checked,
            "documents": outcome.documents,
            "reason": outcome.reason,
            "transient": outcome.transient,
            "skipped": outcome.skipped,
        }
        recap_transient = recap_transient or outcome.transient
        if outcome.quota:  # the same flag the docket search sets, so the row is retried
            state.flags["api_quota_exhausted"] = True
            state.notes.append("CourtListener daily budget spent: re-run later with --recheck")
    for other in dockets[1:]:  # only the best candidate is looked up; say so for the rest
        recap[other.docket_id or -1] = {
            "checked": False,
            "documents": 0,
            "reason": "documents not checked: only the best candidate per row is looked up",
            "skipped": True,  # never asked, so never "absent from RECAP"
        }
    queued = append_queue(ctx.config, _queue_items(task, recap))

    seen = {_upsert_record(conn, row.row_id, item) for item in state.located}
    quota_hit = bool(state.flags.get("api_quota_exhausted"))
    evaluated = {m["name"] for m in run.records if m["status"] in ("hit", "miss")}
    if quota_hit:  # a spent budget says nothing about what the API found before
        evaluated -= {"court.courtlistener_api", "court.recap"}
    if state.flags.get("doj_coverage_gap"):  # nor does a harvest that did not reach the window
        evaluated.discard("court.doj")
    superseded = _supersede(conn, row.row_id, evaluated, seen)
    identifiers = _insert_identifiers(conn, row.row_id, state)

    errored = bool(run.errors)
    coverage_gap = bool(state.flags.get("doj_coverage_gap"))
    status = court_status(
        state.located, quota_exhausted=quota_hit, errored=errored, coverage_gap=coverage_gap
    )
    summary: dict[str, Any] = {
        "court_status": status,
        "classification": {"kind": classification.kind, "basis": classification.basis[:6]},
        "federal_strategies_applied": classification.federal_strategies_apply,
        "located": [
            {
                "source_kind": i.source_kind,
                "strategy": i.strategy,
                "case_number": i.case_number,
                "court": i.court,
                "document_kind": i.document_kind,
                "capture_id": i.capture_id,
                "url": i.url,
                "weak": i.weak,
                "basis": i.basis,  # why it matched: court_records has no column for it
            }
            for i in state.located
        ],
        "identifiers": identifiers,
        "superseded_records": superseded,
        "recap": {str(k): v for k, v in recap.items()},
        "pacer_queued": queued,
        "notes": state.notes[:10],
        "errors": run.errors[:10],  # a module that raised: the row is retried, not a negative
        "flags": state.flags,
        "modules": run.records,
    }
    return StageResult(
        row_id=row.row_id,
        stage=STAGE,
        status="done",
        # a row that could not be fully asked must not look finished to ``--recheck``
        input_hash=input_hash(row, ctx, registry)
        + (":quota" if quota_hit else "")
        + (":error" if errored else "")
        + (":coverage" if coverage_gap else "")
        + (":recap" if recap_transient else ""),
        tool_version=TOOL_VERSION,
        summary_json=summary,
    )


def run(batch: list[Row], ctx: Context) -> list[StageResult]:
    results: list[StageResult] = []
    for row in batch:
        try:
            results.append(process_row(row, ctx))
        except Exception as exc:  # an internal error fails this row, never the batch
            log.exception("court failed for row %s", row.row_id)
            results.append(
                StageResult(
                    row_id=row.row_id,
                    stage=STAGE,
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}"[:300],
                )
            )
    return results

"""The stage runner: eligibility, gates, claiming, idempotence (spec sections 2 and 10).

SQLite is the orchestrator. A stage run claims a bounded batch of eligible rows in one
``BEGIN IMMEDIATE`` transaction, marks them ``running``, processes them outside the transaction
and writes each row's result as it finishes. The runner is the only code that changes
``case_stage`` after ingest seeds it; it never writes ``rows`` and never deletes anything.

A component is a package ``efp.<stage>`` that sets ``IMPLEMENTED = True`` and exposes
``gate(row) -> bool | str`` and ``run(batch, ctx) -> list[StageResult]``; it may also expose
``input_hash(row, ctx) -> str | None`` (used by ``--recheck``) and ``TOOL_VERSION``.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from efp.capture import LazyCapture
from efp.config import Config
from efp.logging import redact
from efp.scope import population_sql
from efp.types import Context, Row, StageResult

log = logging.getLogger(__name__)

BUILD = "04"

# Every predecessor must be done or skipped. A skipped predecessor counts as satisfied so a row
# that stopped at a gate still reaches ``code`` and is coded ``unknown`` with the gate recorded.
PREDECESSORS: dict[str, tuple[str, ...]] = {
    "resolve_links": (),
    "court": ("resolve_links",),
    "identity": ("court",),
    "documents": ("court",),
    "news": ("identity",),
    "social": ("news",),
    "extract": ("documents", "news", "social"),
    "code": ("extract",),
}

# A failed gate also skips these stages, with the same reason. ``news`` and ``social`` failures
# cascade nowhere: a row without a handle must still be extracted from its court documents, and
# ``extract`` and ``code`` are never cascaded (``code`` writes ``unknown`` with the reason).
CASCADE: dict[str, tuple[str, ...]] = {
    "identity": ("news", "social"),
}

SOURCE_COLUMNS = (
    "year",
    "state",
    "name",
    "case_type",
    "fraud_type",
    "details",
    "source_raw",
    "ideological_motivation",
    "ideological_source",
)
ROW_SELECT = "SELECT row_id, " + ", ".join(SOURCE_COLUMNS) + " FROM rows WHERE row_id = ?"

Gate = Callable[[sqlite3.Connection, Config, int], str | None]


class RunnerError(Exception):
    pass


class Component(Protocol):
    def gate(self, row: Row) -> bool | str: ...

    def run(self, batch: list[Row], ctx: Context) -> list[StageResult]: ...


def validate_stages(config: Config) -> None:
    """Every configured stage must be one the predecessor map knows, with its predecessors
    configured too (the eligibility query reads their ``case_stage`` records)."""
    configured = set(config.runner.stages)
    unknown = sorted(configured - set(PREDECESSORS))
    orphaned = sorted(
        s for s in configured & set(PREDECESSORS) if not set(PREDECESSORS[s]) <= configured
    )
    if unknown or orphaned:
        raise RunnerError(
            f"runner.stages invalid: unknown {unknown}, stages missing a predecessor {orphaned}"
        )


def input_hash(*values: object) -> str:
    """The helper components use to hash the inputs they read for a row."""
    payload = json.dumps(values, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# --- gates ---------------------------------------------------------------------------------


def _stage_status(conn: sqlite3.Connection, row_id: int, stage: str) -> str | None:
    found = conn.execute(
        "SELECT status FROM case_stage WHERE row_id = ? AND stage = ?", (row_id, stage)
    ).fetchone()
    return None if found is None else str(found[0])


def _identity_outcome(conn: sqlite3.Connection, row_id: int) -> str | None:
    """What the gates read: the latest identity decision of the pre-registered ``main`` batch.

    That is the model's own ``identity`` decision, or the operator's later ``review`` /
    ``override`` decision for the identity stage (``confirmed`` or ``unresolved``). An audit
    verdict ``audit_wrong`` withdraws a confirmation (read as ``unresolved``); ``audit_correct``,
    labels and the ``sensitivity`` batch are never read.
    """
    found = conn.execute(
        "SELECT outcome FROM decisions WHERE row_id = ? AND batch = 'main' AND ("
        "  kind = 'identity' OR (kind IN ('review', 'override') AND stage = 'identity' "
        "  AND outcome IN ('confirmed', 'unresolved', 'audit_wrong'))) "
        "ORDER BY decision_id DESC LIMIT 1",
        (row_id,),
    ).fetchone()
    if found is None:
        return None
    return "unresolved" if str(found[0]) == "audit_wrong" else str(found[0])


def gate_identity(conn: sqlite3.Connection, config: Config, row_id: int) -> str | None:
    return None if _stage_status(conn, row_id, "court") == "done" else "court_not_done"


def gate_news(conn: sqlite3.Connection, config: Config, row_id: int) -> str | None:
    if _stage_status(conn, row_id, "identity") != "done":
        return "identity_not_done"
    outcome = _identity_outcome(conn, row_id)
    # No identity decision at all is treated as unresolved: news must never run on a guess.
    return "identity_unresolved" if outcome in (None, "unresolved") else None


def gate_social(conn: sqlite3.Connection, config: Config, row_id: int) -> str | None:
    if _stage_status(conn, row_id, "news") != "done":
        return "news_not_done"
    if not config.first_pass.social_enabled:
        return "social_disabled"
    if _identity_outcome(conn, row_id) != "confirmed":
        return "identity_not_confirmed"
    has_identifier = conn.execute(
        "SELECT 1 FROM identifiers WHERE row_id = ? AND kind IN ('handle','email') LIMIT 1",
        (row_id,),
    ).fetchone()
    return None if has_identifier else "no_handle"


def gate_code(conn: sqlite3.Connection, config: Config, row_id: int) -> str | None:
    ok = _stage_status(conn, row_id, "extract") in ("done", "skipped")
    return None if ok else "extract_not_done"


GATES: dict[str, Gate] = {
    "identity": gate_identity,
    "news": gate_news,
    "social": gate_social,
    "code": gate_code,
}


# --- selection ------------------------------------------------------------------------------


@dataclass
class RunOptions:
    stage: str
    limit: int | None = None
    state: str | None = None
    year_from: int | None = None
    sample: bool = False
    dry_run: bool = False
    reset: bool = False
    recover: bool = False
    retry_failed: bool = False
    recheck: bool = False  # also look at ``done`` rows: unchanged input hash is a no-op
    force: bool = False  # recheck, ignoring the hash


def load_sample_ids(config: Config) -> list[int]:
    path = Path(config.scope.sample_file)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[1] / path
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        return sorted(int(r["row_id"]) for r in document["rows"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RunnerError(f"cannot read the frozen sample {path}: {exc}") from None


def _scope_clause(
    config: Config, options: RunOptions, sample_ids: Sequence[int]
) -> tuple[str, list[Any]]:
    clause = population_sql(config.scope.population, alias="r")
    params: list[Any] = []
    if options.state:
        clause += " AND r.state = ?"
        params.append(options.state.upper())
    if options.year_from is not None:
        clause += " AND r.year >= ?"
        params.append(options.year_from)
    if options.sample:
        clause += (
            f" AND r.row_id IN ({','.join('?' * len(sample_ids))})" if sample_ids else " AND 0"
        )
        params.extend(sample_ids)
    return clause, params


def _candidate_sql(
    config: Config,
    options: RunOptions,
    sample_ids: Sequence[int],
    cursor: tuple[int, int] | None,
    page: int | None,
) -> tuple[str, list[Any]]:
    stage = options.stage
    preds = PREDECESSORS[stage]
    scope, params = _scope_clause(config, options, sample_ids)
    statuses = ["'pending'", "'failed'"] + (["'done'"] if options.recheck or options.force else [])
    sql = (
        "SELECT r.row_id, r.year FROM rows r "
        "JOIN case_stage cur ON cur.row_id = r.row_id AND cur.stage = ? "
        f"WHERE {scope} AND cur.status IN ({','.join(statuses)})"
    )
    out: list[Any] = [stage, *params]
    if options.retry_failed:
        pass  # failed rows at the attempt limit get one more attempt
    else:
        sql += " AND (cur.status = 'done' OR cur.attempts < ?)"
        out.append(config.runner.max_attempts_per_stage)
    if preds:
        sql += (
            " AND (SELECT count(*) FROM case_stage p WHERE p.row_id = r.row_id "
            f"AND p.stage IN ({','.join('?' * len(preds))}) "
            "AND p.status IN ('done','skipped')) = ?"
        )
        out.extend(preds)
        out.append(len(preds))
    if cursor is not None:
        sql += " AND (r.year < ? OR (r.year = ? AND r.row_id > ?))"
        out.extend([cursor[0], cursor[0], cursor[1]])
    sql += " ORDER BY r.year DESC, r.row_id"
    if page is not None:
        sql += " LIMIT ?"
        out.append(page)
    return sql, out


def load_row(conn: sqlite3.Connection, row_id: int) -> Row:
    found = conn.execute(ROW_SELECT, (row_id,)).fetchone()
    if found is None:
        raise RunnerError(f"row {row_id} not found")
    source = dict(zip(SOURCE_COLUMNS, found[1:], strict=True))
    return Row(row_id=int(found[0]), year=source["year"], state=source["state"], source=source)


# --- state changes (the only writers of case_stage) -----------------------------------------


def _skip(
    conn: sqlite3.Connection, row_id: int, stage: str, reason: str, from_statuses: Sequence[str]
) -> None:
    marks = ",".join("?" * len(from_statuses))
    conn.execute(
        "UPDATE case_stage SET status = 'skipped', gate_reason = ?, finished_at = ?, "
        "last_error = NULL "
        f"WHERE row_id = ? AND stage = ? AND status IN ({marks})",
        (reason, _now(), row_id, stage, *from_statuses),
    )


def apply_gate_failure(
    conn: sqlite3.Connection, row_id: int, stage: str, reason: str, from_statuses: Sequence[str]
) -> None:
    """Skip the stage with the reason, and cascade to the stages that depend on it."""
    _skip(conn, row_id, stage, reason, from_statuses)
    for downstream in CASCADE.get(stage, ()):
        _skip(conn, row_id, downstream, reason, ("pending", "failed"))


def recover(
    conn: sqlite3.Connection, stage: str, *, stale_after_minutes: int | None, force: bool
) -> int:
    """Reset ``running`` rows to ``pending`` without counting the interrupted attempt."""
    sql = (
        "UPDATE case_stage SET status = 'pending', attempts = max(attempts - 1, 0), "
        "started_at = NULL WHERE stage = ? AND status = 'running'"
    )
    params: list[Any] = [stage]
    if not force:
        if stale_after_minutes is None:
            return 0
        cutoff = (datetime.now(UTC) - timedelta(minutes=stale_after_minutes)).isoformat(
            timespec="seconds"
        )
        sql += " AND started_at < ?"
        params.append(cutoff)
    cursor = conn.execute(sql, params)
    return cursor.rowcount


def _successors(stage: str) -> list[str]:
    found: list[str] = []
    frontier = [stage]
    while frontier:
        current = frontier.pop()
        for name, preds in PREDECESSORS.items():
            if current in preds and name not in found:
                found.append(name)
                frontier.append(name)
    return found


def reset_rows(
    conn: sqlite3.Connection, config: Config, options: RunOptions, sample_ids: Sequence[int]
) -> int:
    """Set the stage back to ``pending`` for the selected rows. Captures are never touched.

    Downstream stages that were *skipped* (a gate, or a cascade from this stage) are re-opened
    too, so a row that now passes the gate is not left stranded; ``done`` ones are left alone.
    """
    scope, params = _scope_clause(config, options, sample_ids)
    ids = [int(r[0]) for r in conn.execute(f"SELECT r.row_id FROM rows r WHERE {scope}", params)]
    reopened = 0
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        marks = ",".join("?" * len(chunk))
        cursor = conn.execute(
            "UPDATE case_stage SET status = 'pending', attempts = 0, gate_reason = NULL, "
            "last_error = NULL, input_hash = NULL, summary_json = NULL, started_at = NULL, "
            f"finished_at = NULL WHERE stage = ? AND status != 'running' AND row_id IN ({marks})",
            (options.stage, *chunk),
        )
        reopened += cursor.rowcount
        for downstream in _successors(options.stage):
            conn.execute(
                "UPDATE case_stage SET status = 'pending', gate_reason = NULL, "
                "finished_at = NULL WHERE stage = ? AND status = 'skipped' "
                f"AND row_id IN ({marks})",
                (downstream, *chunk),
            )
    return reopened


# --- the run --------------------------------------------------------------------------------


@dataclass
class RunSummary:
    stage: str
    claimed: int = 0
    done: int = 0
    failed: int = 0
    needs_review: int = 0
    skipped: int = 0
    noop: int = 0
    gate_skipped: dict[str, int] = field(default_factory=dict)
    recovered: int = 0
    reset: int = 0
    no_component: bool = False

    def count_gate(self, reason: str) -> None:
        self.gate_skipped[reason] = self.gate_skipped.get(reason, 0) + 1


def load_component(stage: str) -> Component | None:
    """The built component for a stage, or None until its build lands."""
    module = importlib.import_module(f"efp.{stage}")
    if not getattr(module, "IMPLEMENTED", False):
        return None
    component: Component = module
    return component


def _component_gate(component: Component | None, row: Row, stage: str) -> str | None:
    if component is None:
        return None
    verdict = component.gate(row)
    if verdict is True:
        return None
    return verdict if isinstance(verdict, str) and verdict else f"{stage}_gate"


def _claim(
    conn: sqlite3.Connection,
    config: Config,
    options: RunOptions,
    sample_ids: Sequence[int],
    component: Component | None,
    ctx: Context,
    cursor: tuple[int, int] | None,
    page: int,
    summary: RunSummary,
) -> tuple[list[Row], tuple[int, int] | None, bool]:
    """One claiming transaction. Returns (claimed rows, new cursor, whether anything was seen)."""
    stage = options.stage
    conn.execute("BEGIN IMMEDIATE")
    try:
        sql, params = _candidate_sql(config, options, sample_ids, cursor, page)
        candidates = [(int(r[0]), int(r[1])) for r in conn.execute(sql, params)]
        claimed: list[Row] = []
        for row_id, _year in candidates:
            row = load_row(conn, row_id)
            current = _stage_status(conn, row_id, stage) or "pending"
            reason = GATES[stage](conn, config, row_id) if stage in GATES else None
            reason = reason or _component_gate(component, row, stage)
            if reason is not None:
                if current != "done":
                    apply_gate_failure(conn, row_id, stage, reason, ("pending", "failed"))
                    summary.count_gate(reason)
                continue
            if current == "done":
                if options.force:
                    pass
                else:
                    new_hash = getattr(component, "input_hash", None)
                    stored = conn.execute(
                        "SELECT input_hash FROM case_stage WHERE row_id = ? AND stage = ?",
                        (row_id, stage),
                    ).fetchone()[0]
                    if callable(new_hash) and stored is not None and new_hash(row, ctx) == stored:
                        summary.noop += 1
                        continue
            claimed.append(row)
        if claimed:
            marks = ",".join("?" * len(claimed))
            conn.execute(
                "UPDATE case_stage SET status = 'running', attempts = attempts + 1, "
                "started_at = ?, finished_at = NULL "
                f"WHERE stage = ? AND row_id IN ({marks})",
                (_now(), stage, *[r.row_id for r in claimed]),
            )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    new_cursor = (candidates[-1][1], candidates[-1][0]) if candidates else cursor
    return claimed, new_cursor, bool(candidates)


def _write_result(conn: sqlite3.Connection, stage: str, result: StageResult) -> None:
    if result.status == "skipped":
        reason = result.gate_reason or f"{stage}_skipped"
        conn.execute(
            "UPDATE case_stage SET input_hash = ?, tool_version = ?, summary_json = ? "
            "WHERE row_id = ? AND stage = ? AND status = 'running'",
            (
                result.input_hash,
                result.tool_version,
                redact(json.dumps(result.summary_json)),
                result.row_id,
                stage,
            ),
        )
        apply_gate_failure(conn, result.row_id, stage, reason, ("running",))
        return
    conn.execute(
        "UPDATE case_stage SET status = ?, input_hash = ?, tool_version = ?, last_error = ?, "
        "gate_reason = NULL, summary_json = ?, finished_at = ? "
        "WHERE row_id = ? AND stage = ? AND status = 'running'",
        (
            result.status,
            result.input_hash,
            result.tool_version,
            redact(result.error) if result.error else None,
            redact(json.dumps(result.summary_json)),
            _now(),
            result.row_id,
            stage,
        ),
    )


def _fail(conn: sqlite3.Connection, stage: str, row_id: int, error: str) -> None:
    _write_result(
        conn,
        stage,
        StageResult(row_id=row_id, stage=stage, status="failed", error=redact(error)[:500]),
    )


def run_stage(
    conn: sqlite3.Connection,
    config: Config,
    options: RunOptions,
    ctx: Context,
    *,
    component: Component | None = None,
    use_registered_component: bool = True,
) -> RunSummary:
    """Run one stage over every eligible row, batch by batch, each row visited once per run."""
    stage = options.stage
    if stage not in PREDECESSORS:
        raise RunnerError(f"unknown stage {stage!r}")
    validate_stages(config)
    if options.reset and options.limit is not None:
        raise RunnerError(
            "--reset applies to every selected row; it cannot be combined with --limit "
            "(narrow it with --state, --year-from or --sample)"
        )
    clear = getattr(ctx.capture, "clear_throttles", None)
    if callable(clear):
        clear()  # a 429 remembered from an earlier run of this process says nothing about this one
    previous = conn.isolation_level
    conn.isolation_level = None  # the runner issues BEGIN/COMMIT itself
    summary = RunSummary(stage)
    try:
        sample_ids = load_sample_ids(config) if options.sample else []
        if component is None and use_registered_component:
            component = load_component(stage)
        summary.recovered = recover(
            conn,
            stage,
            stale_after_minutes=config.runner.stale_after_minutes,
            force=options.recover,
        )
        if options.reset:
            summary.reset = reset_rows(conn, config, options, sample_ids)
        if component is None:
            summary.no_component = True
            return summary
        remaining = options.limit
        cursor: tuple[int, int] | None = None
        while remaining is None or remaining > 0:
            page = (
                config.runner.batch_size
                if remaining is None
                else min(config.runner.batch_size, remaining)
            )
            batch, cursor, seen = _claim(
                conn, config, options, sample_ids, component, ctx, cursor, page, summary
            )
            if not seen:
                break
            if not batch:
                continue
            summary.claimed += len(batch)
            if remaining is not None:
                remaining -= len(batch)
            _process(conn, stage, component, batch, ctx, summary)
        return summary
    finally:
        conn.isolation_level = previous


def _process(
    conn: sqlite3.Connection,
    stage: str,
    component: Component,
    batch: list[Row],
    ctx: Context,
    summary: RunSummary,
) -> None:
    try:
        results = component.run(batch, ctx)
    except Exception as exc:
        for row in batch:
            _fail(conn, stage, row.row_id, f"{type(exc).__name__}: {exc}")
        summary.failed += len(batch)
        return
    by_row = {r.row_id: r for r in results}
    for row in batch:
        result = by_row.get(row.row_id)
        if result is None:
            _fail(conn, stage, row.row_id, "component returned no result for this row")
            summary.failed += 1
            continue
        _write_result(conn, stage, result)
        if result.status == "done":
            summary.done += 1
        elif result.status == "failed":
            summary.failed += 1
        elif result.status == "needs_review":
            summary.needs_review += 1
        elif result.status == "skipped":
            summary.skipped += 1
            summary.count_gate(result.gate_reason or f"{stage}_skipped")


# --- dry run --------------------------------------------------------------------------------


@dataclass
class DryRunRow:
    row_id: int
    year: int
    gate: str | None
    predecessors: dict[str, str]


@dataclass
class DryRunReport:
    stage: str
    population: int
    eligible: int
    rows: list[DryRunRow]
    blockers: dict[str, int]  # predecessor stage -> rows in scope not yet done or skipped
    own_status: dict[str, int]
    would_reset: int | None = None
    has_component: bool = False


def dry_run(
    conn: sqlite3.Connection, config: Config, options: RunOptions, ctx: Context
) -> DryRunReport:
    """What a run would claim, with gate results and predecessor status. Writes nothing."""
    stage = options.stage
    sample_ids = load_sample_ids(config) if options.sample else []
    component = load_component(stage)
    scope, params = _scope_clause(config, options, sample_ids)
    population = conn.execute(
        f"SELECT count(*) FROM rows r WHERE {scope}",
        params,
    ).fetchone()[0]
    count_sql, count_params = _candidate_sql(config, options, sample_ids, None, None)
    eligible = conn.execute(
        f"SELECT count(*) FROM ({count_sql})",
        count_params,
    ).fetchone()[0]
    page = (
        min(config.runner.batch_size, options.limit) if options.limit else config.runner.batch_size
    )
    list_sql, list_params = _candidate_sql(config, options, sample_ids, None, page)
    rows: list[DryRunRow] = []
    for row_id, year in conn.execute(list_sql, list_params).fetchall():
        row = load_row(conn, row_id)
        reason = GATES[stage](conn, config, row_id) if stage in GATES else None
        reason = reason or _component_gate(component, row, stage)
        rows.append(
            DryRunRow(
                int(row_id),
                int(year),
                reason,
                {p: _stage_status(conn, row_id, p) or "missing" for p in PREDECESSORS[stage]},
            )
        )
    blockers: dict[str, int] = {}
    for pred in PREDECESSORS[stage]:
        blockers[pred] = conn.execute(
            "SELECT count(*) FROM rows r JOIN case_stage p ON p.row_id = r.row_id "
            f"AND p.stage = ? WHERE {scope} AND p.status NOT IN ('done','skipped')",
            [pred, *params],
        ).fetchone()[0]
    own = {
        str(s): int(n)
        for s, n in conn.execute(
            "SELECT cur.status, count(*) FROM rows r JOIN case_stage cur ON cur.row_id = r.row_id "
            f"AND cur.stage = ? WHERE {scope} GROUP BY cur.status",
            [stage, *params],
        )
    }
    would_reset = None
    if options.reset:
        would_reset = int(
            conn.execute(
                "SELECT count(*) FROM rows r JOIN case_stage cur ON cur.row_id = r.row_id "
                f"AND cur.stage = ? WHERE {scope} AND cur.status != 'running'",
                [stage, *params],
            ).fetchone()[0]
        )
    return DryRunReport(
        stage,
        int(population),
        int(eligible),
        rows,
        blockers,
        own,
        would_reset=would_reset,
        has_component=component is not None,
    )


def render_dry_run(report: DryRunReport) -> str:
    lines = [
        f"stage {report.stage}: {report.eligible:,} eligible of {report.population:,} in scope"
        f" (component: {'registered' if report.has_component else 'not built yet'})"
    ]
    if report.eligible == 0:
        for pred, blocked in report.blockers.items():
            if blocked:
                lines.append(
                    f"  none eligible: predecessor {pred} is not done for {blocked:,} rows"
                )
        if not any(report.blockers.values()):
            states = ", ".join(f"{s}={n:,}" for s, n in sorted(report.own_status.items()))
            lines.append(f"  none eligible: every predecessor is satisfied; own status: {states}")
    for item in report.rows:
        preds = ", ".join(f"{p}={s}" for p, s in item.predecessors.items()) or "-"
        gate = f"gate: skip ({item.gate})" if item.gate else "gate: pass"
        lines.append(f"  row {item.row_id} ({item.year}): {gate}; predecessors: {preds}")
    if report.would_reset is not None:
        lines.append(f"  --reset would set {report.would_reset:,} rows back to pending")
    if len(report.rows) < report.eligible:
        lines.append(f"  ... {report.eligible - len(report.rows):,} more not listed")
    return "\n".join(lines)


def make_context(config: Config, conn: sqlite3.Connection | None) -> Context:
    capture = LazyCapture(config, conn) if conn is not None else None
    return Context(config=config, db=conn, capture=capture, log=logging.getLogger("efp.runner"))

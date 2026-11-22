"""``efp status``: the stage x status matrix, gate reasons, attempts and errors, and the sample.

``build_report`` gathers everything into one :class:`StatusReport`; ``render_md`` and
``render_json`` are the two views. Later builds extend the Markdown form into the first-pass
report, so the renderer takes the dataclass and nothing else.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any

from efp.config import Config
from efp.runner import load_sample_ids
from efp.scope import population_sql

STATUSES = ("pending", "running", "done", "failed", "needs_review", "skipped")


def status_matrix(
    conn: sqlite3.Connection,
    config: Config,
    *,
    stage: str | None = None,
    state: str | None = None,
    sample_ids: list[int] | None = None,
) -> tuple[int, dict[str, dict[str, int]]]:
    """(rows in the population, {stage: {status: count}}) in ``runner.stages`` order."""
    condition = population_sql(config.scope.population, alias="r")
    filters = ""
    params: list[object] = []
    if state:
        filters += " AND r.state = ?"
        params.append(state.upper())
    if sample_ids is not None:
        filters += (
            f" AND r.row_id IN ({','.join('?' * len(sample_ids))})" if sample_ids else " AND 0"
        )
        params.extend(sample_ids)
    # `condition` comes from efp.scope.population_sql (fixed fragments); values are bound.
    count_sql = f"SELECT count(*) FROM rows r WHERE {condition}{filters}"
    population = conn.execute(count_sql, params).fetchone()[0]
    stages = [s for s in config.runner.stages if stage is None or s == stage]
    matrix: dict[str, dict[str, int]] = {s: dict.fromkeys(STATUSES, 0) for s in stages}
    query = (
        "SELECT cs.stage, cs.status, count(*) FROM case_stage cs "
        f"JOIN rows r ON r.row_id = cs.row_id WHERE {condition}{filters} "
        "GROUP BY cs.stage, cs.status"
    )
    for stage_name, status, count in conn.execute(query, params):
        if stage_name in matrix:
            matrix[stage_name][status] = count
    return int(population), matrix


def render(population_name: str, population: int, matrix: dict[str, dict[str, int]]) -> str:
    width = max([len("stage"), *(len(s) for s in matrix)])
    header = f"{'stage':<{width}}  " + "  ".join(f"{s:>12}" for s in STATUSES)
    lines = [f"population: {population_name} ({population:,} rows)", header]
    for stage_name, counts in matrix.items():
        lines.append(f"{stage_name:<{width}}  " + "  ".join(f"{counts[s]:>12,}" for s in STATUSES))
    return "\n".join(lines)


@dataclass
class StatusReport:
    population_name: str
    population: int
    matrix: dict[str, dict[str, int]]
    gate_reasons: dict[str, dict[str, int]] = field(default_factory=dict)
    attempts: dict[str, dict[str, int]] = field(default_factory=dict)
    errors: dict[str, list[tuple[str, int]]] = field(default_factory=dict)
    sample_size: int = 0
    sample_matrix: dict[str, dict[str, int]] = field(default_factory=dict)


def _scoped(
    config: Config, state: str | None, sample_ids: list[int] | None
) -> tuple[str, list[Any]]:
    condition = population_sql(config.scope.population, alias="r")
    params: list[Any] = []
    if state:
        condition += " AND r.state = ?"
        params.append(state.upper())
    if sample_ids is not None:
        condition += (
            f" AND r.row_id IN ({','.join('?' * len(sample_ids))})" if sample_ids else " AND 0"
        )
        params.extend(sample_ids)
    return condition, params


def build_report(
    conn: sqlite3.Connection,
    config: Config,
    *,
    stage: str | None = None,
    state: str | None = None,
    top_errors: int = 5,
) -> StatusReport:
    population, matrix = status_matrix(conn, config, stage=stage, state=state)
    report = StatusReport(config.scope.population, population, matrix)
    condition, params = _scoped(config, state, None)
    stage_clause = " AND cs.stage = ?" if stage else ""
    extra = [stage] if stage else []
    join = "FROM case_stage cs JOIN rows r ON r.row_id = cs.row_id"
    for name, reason, count in conn.execute(
        f"SELECT cs.stage, cs.gate_reason, count(*) {join} WHERE {condition}{stage_clause} "
        "AND cs.status = 'skipped' AND cs.gate_reason IS NOT NULL GROUP BY 1, 2 ORDER BY 1, 3 DESC",
        [*params, *extra],
    ):
        report.gate_reasons.setdefault(name, {})[reason] = count
    for name, tried, peak, at_limit in conn.execute(
        "SELECT cs.stage, sum(cs.attempts > 0), coalesce(max(cs.attempts), 0), "
        f"sum(cs.status = 'failed' AND cs.attempts >= ?) {join} "
        f"WHERE {condition}{stage_clause} GROUP BY 1",
        [config.runner.max_attempts_per_stage, *params, *extra],
    ):
        report.attempts[name] = {
            "rows_attempted": int(tried or 0),
            "max_attempts": int(peak),
            "failed_at_limit": int(at_limit or 0),
        }
    for name, message, count in conn.execute(
        f"SELECT cs.stage, cs.last_error, count(*) {join} WHERE {condition}{stage_clause} "
        "AND cs.last_error IS NOT NULL GROUP BY 1, 2 ORDER BY 1, 3 DESC",
        [*params, *extra],
    ):
        bucket = report.errors.setdefault(name, [])
        if len(bucket) < top_errors:
            bucket.append((message, count))
    try:
        ids = load_sample_ids(config)
    except Exception:  # no sample file yet: the subset is simply absent
        ids = []
    if ids:
        report.sample_size, report.sample_matrix = status_matrix(
            conn, config, stage=stage, state=state, sample_ids=ids
        )
    return report


def _table(matrix: dict[str, dict[str, int]]) -> list[str]:
    lines = ["| stage | " + " | ".join(STATUSES) + " |", "|---|" + "---:|" * len(STATUSES)]
    for stage_name, counts in matrix.items():
        lines.append(f"| {stage_name} | " + " | ".join(f"{counts[s]:,}" for s in STATUSES) + " |")
    return lines


def render_md(report: StatusReport) -> str:
    out = [f"## Stage status: {report.population_name} ({report.population:,} rows)", ""]
    out += _table(report.matrix)
    out += ["", "### Gate reasons", ""]
    if report.gate_reasons:
        out += ["| stage | reason | rows |", "|---|---|---:|"]
        for name, reasons in report.gate_reasons.items():
            out += [f"| {name} | {reason} | {n:,} |" for reason, n in reasons.items()]
    else:
        out.append("None: no row has stopped at a gate.")
    out += ["", "### Attempts and errors", ""]
    if report.attempts and any(a["rows_attempted"] for a in report.attempts.values()):
        out += [
            "| stage | rows attempted | max attempts | failed at limit |",
            "|---|---:|---:|---:|",
        ]
        for name, a in report.attempts.items():
            if a["rows_attempted"]:
                out.append(
                    f"| {name} | {a['rows_attempted']:,} | {a['max_attempts']} "
                    f"| {a['failed_at_limit']:,} |"
                )
        for name, errors in report.errors.items():
            out += ["", f"Top errors for {name}:"]
            out += [f"- {n:,} x {message}" for message, n in errors]
    else:
        out.append("No stage has been attempted yet.")
    if report.sample_size:
        out += ["", f"### Frozen sample ({report.sample_size:,} rows in scope)", ""]
        out += _table(report.sample_matrix)
    return "\n".join(out)


def render_json(report: StatusReport) -> str:
    return json.dumps(asdict(report), indent=2, sort_keys=False)

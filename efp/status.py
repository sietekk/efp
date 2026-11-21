"""``efp status``: the stage x status matrix over the study population (minimal for now).

Build 04 extends it with the sample subset and the runner's eligibility counts.
"""

from __future__ import annotations

import sqlite3

from efp.config import Config
from efp.scope import population_sql

STATUSES = ("pending", "running", "done", "failed", "needs_review", "skipped")


def status_matrix(
    conn: sqlite3.Connection,
    config: Config,
    *,
    stage: str | None = None,
    state: str | None = None,
) -> tuple[int, dict[str, dict[str, int]]]:
    """(rows in the population, {stage: {status: count}}) in ``runner.stages`` order."""
    condition = population_sql(config.scope.population, alias="r")
    filters = ""
    params: list[object] = []
    if state:
        filters += " AND r.state = ?"
        params.append(state.upper())
    # `condition` comes from efp.scope.population_sql (fixed fragments); values are bound.
    count_sql = f"SELECT count(*) FROM rows r WHERE {condition}{filters}"  # noqa: S608
    population = conn.execute(count_sql, params).fetchone()[0]
    stages = [s for s in config.runner.stages if stage is None or s == stage]
    matrix: dict[str, dict[str, int]] = {s: dict.fromkeys(STATUSES, 0) for s in stages}
    query = (
        "SELECT cs.stage, cs.status, count(*) FROM case_stage cs "  # noqa: S608
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

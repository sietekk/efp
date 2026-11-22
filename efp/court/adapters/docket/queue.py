"""The docket work lists written for the operator, under the data directory.

They name rows and the names the operator will search, so they live in the ``queues`` folder of
the data directory (``paths.data_dir``) with ``pacer.csv``: row-level data stays with the data.
``queues/README.md`` describes them.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Any

from efp.config import Config

PORTAL_COLUMNS = ["state", "portal", "url", "wants", "detail", "rows_affected"]
ASSISTED_COLUMNS = [
    "row_id",
    "state",
    "year",
    "portal_url",
    "name",
    "filed_from",
    "filed_to",
    "after",
]


def queues_dir(config: Config) -> Path:
    return Path(config.paths.data_dir) / "queues"


def write_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def merge_csv(
    path: Path, columns: list[str], rows: list[dict[str, Any]], *, replace_states: set[str]
) -> Path:
    """Write ``rows`` into the file, replacing only the rows of ``replace_states``: re-probing one
    state must not erase the work list of every other state."""
    kept: list[dict[str, Any]] = []
    if path.is_file():
        with path.open(newline="", encoding="utf-8") as handle:
            kept = [r for r in csv.DictReader(handle) if r.get("state") not in replace_states]
    return write_csv(path, columns, [*kept, *rows])


def assisted_rows(
    conn: sqlite3.Connection, *, state: str | None = None, limit: int | None = None
) -> list[dict[str, Any]]:
    """The rows waiting on the operator's own search: the constructed query is in the row's court
    summary (``summary_json.assisted``), set by ``court.state_docket`` for an assisted portal."""
    out: list[dict[str, Any]] = []
    cursor = conn.execute(
        "SELECT cs.row_id, r.state, r.year, cs.summary_json FROM case_stage cs "
        "JOIN rows r ON r.row_id = cs.row_id WHERE cs.stage = 'court' "
        "AND json_extract(cs.summary_json, '$.assisted') IS NOT NULL "
        "ORDER BY r.state, r.year, cs.row_id"
    )
    for row_id, row_state, year, summary in cursor:
        query = (json.loads(summary) or {}).get("assisted") or {}
        if state and str(row_state or "").upper() != state.upper():
            continue
        out.append(
            {
                "row_id": row_id,
                "state": row_state,
                "year": year,
                "portal_url": query.get("portal_url", ""),
                "name": query.get("name", ""),
                "filed_from": query.get("filed_from", ""),
                "filed_to": query.get("filed_to", ""),
                "after": query.get("after", ""),
            }
        )
        if limit is not None and len(out) >= limit:
            break
    return out

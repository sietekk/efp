"""``ref_candidates_all``: the candidate universe build 12 matches against, in one shape.

Rebuilt from whichever datasets have a current load (so the identity build works over any
subset), in one transaction. ``years`` is a range (``2004-2018``); DIME and FEC rows, which are per
cycle, are collapsed to one row per candidate and office. The per-source counts and the coverage by
state and year are what ``efp reference index`` prints, so the identity build can see where the
universe is thin.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

# source name -> (current view, SELECT list producing the unified columns, GROUP BY or "")
_SOURCES: dict[str, str] = {
    "dime": """
        SELECT 'dime', bonica_rid, MIN(load_id), surname, given, middle, surname_key, state,
               office, party, 0, MIN(cycle) || '-' || MAX(cycle), MIN(cycle), MAX(cycle)
          FROM ref_dime_current WHERE surname_key <> ''
         GROUP BY bonica_rid, surname_key, given, state, office, party""",
    "fec": """
        SELECT 'fec', cand_id, MIN(load_id), surname, given, middle, surname_key, state,
               office, party, 0, MIN(election_year) || '-' || MAX(election_year),
               MIN(election_year), MAX(election_year)
          FROM ref_fec_candidates_current WHERE surname_key <> ''
         GROUP BY cand_id, surname_key, given, state, office, party""",
    "ftm": """
        SELECT 'ftm', ftm_id, MIN(load_id), surname, given, middle, surname_key, state, office,
               party, 0, MIN(year) || '-' || MAX(year), MIN(year), MAX(year)
          FROM ref_ftm_current WHERE surname_key <> ''
         GROUP BY ftm_id, surname_key, given, state, office, party""",
    "harvard_local": """
        SELECT 'harvard_local', record_id, load_id, surname, given, middle, surname_key, state,
               office, party, party_imputed, CAST(year AS TEXT), year, year
          FROM ref_local_current WHERE surname_key <> ''""",
    "congress": """
        SELECT 'congress', bioguide, load_id, surname, given, middle, surname_key, state,
               'federal', party, 0, first_year || '-' || last_year, first_year, last_year
          FROM ref_congress_current WHERE surname_key <> ''""",
    "state_leg": """
        SELECT 'state_leg', openstates_id, load_id, surname, given, middle, surname_key, state,
               chamber, party, 0, first_year || '-' || last_year, first_year, last_year
          FROM ref_state_leg_current WHERE surname_key <> ''""",
}
_COLUMNS = (
    "source, source_id, load_id, surname, given, middle, surname_key, state, office, party, "
    "party_imputed, years, first_year, last_year"
)


@dataclass
class IndexResult:
    counts: dict[str, int] = field(default_factory=dict)
    total: int = 0


def rebuild(conn: sqlite3.Connection) -> IndexResult:
    """Replace the contents of ``ref_candidates_all`` with the current loads, atomically."""
    result = IndexResult()
    try:
        conn.execute("DELETE FROM ref_candidates_all")
        for source, select in _SOURCES.items():
            cursor = conn.execute(f"INSERT INTO ref_candidates_all ({_COLUMNS}) {select}")
            result.counts[source] = max(cursor.rowcount, 0)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    result.total = sum(result.counts.values())
    return result


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        source: n
        for source, n in conn.execute("SELECT source, count(*) FROM ref_candidates_all GROUP BY 1")
    }


def coverage_by_state(conn: sqlite3.Connection) -> list[tuple[str, int, int | None, int | None]]:
    """``(state, candidates, first_year, last_year)`` over the whole universe, thinnest first."""
    return conn.execute(
        "SELECT coalesce(state, '??'), count(*), min(first_year), max(last_year) "
        "FROM ref_candidates_all GROUP BY 1 ORDER BY 2, 1"
    ).fetchall()


def coverage_by_source_year(conn: sqlite3.Connection) -> list[tuple[str, int, int]]:
    """``(source, year, candidates)`` counting each candidate in every year of its range (capped to
    1980-2030), for the report's coverage table."""
    return conn.execute(
        "WITH RECURSIVE y(n) AS (SELECT 1980 UNION ALL SELECT n + 1 FROM y WHERE n < 2030) "
        "SELECT c.source, y.n, count(*) FROM ref_candidates_all c JOIN y "
        "ON y.n BETWEEN c.first_year AND c.last_year GROUP BY 1, 2 ORDER BY 1, 2"
    ).fetchall()

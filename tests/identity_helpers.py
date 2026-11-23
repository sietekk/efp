"""Synthetic fixtures for the identity tests. No real person appears anywhere: every name,
case number, city and document is made up, and so are the reference tables."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence

from efp.config import Config
from efp.store import write_body
from tests.dbhelpers import STAMP

# Tiny reference tables with round numbers, so priors can be computed by hand.
# 100 adults per birth year and state "ZZ"... see seed_reference().
SURNAME_PROP100K = {"COMMONSURNAME": 800.0, "RARESURNAME": 0.5, "MIDSURNAME": 20.0}
FLOOR_PROP100K = 0.25


def seed_row(
    conn: sqlite3.Connection,
    row_id: int,
    name: str,
    *,
    state: str = "CT",
    year: int = 2018,
    details: str | None = None,
    source_raw: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO rows (row_id, year, state, name, details, source_raw, in_scope, row_sha256, "
        "ingested_at) VALUES (?,?,?,?,?,?,1,?,?)",
        (row_id, year, state, name, details, source_raw, f"{row_id:064d}", STAMP),
    )
    for stage in (
        "resolve_links",
        "court",
        "identity",
        "news",
        "documents",
        "social",
        "extract",
        "code",
    ):
        conn.execute(
            "INSERT INTO case_stage (row_id, stage, status) VALUES (?, ?, ?)",
            (row_id, stage, "done" if stage in ("resolve_links", "court") else "pending"),
        )
    conn.commit()


def _capture(
    conn: sqlite3.Connection,
    config: Config,
    row_id: int,
    body: bytes,
    *,
    url: str,
    content_type: str,
    stage: str = "court",
) -> int:
    sha = write_body(config.paths.store, body)
    cursor = conn.execute(
        "INSERT INTO captures (row_id, stage, url_requested, url_final, retrieved_at, content_type, "
        "content_sha256, body_path, tool, tool_version) VALUES (?,?,?,?,?,?,?,?,'test','0')",
        (row_id, stage, url, url, STAMP, content_type, sha, f"store/{sha[:2]}/{sha}"),
    )
    return int(cursor.lastrowid or 0)


def seed_court(
    conn: sqlite3.Connection,
    config: Config,
    row_id: int,
    *,
    text: str,
    case_number: str | None,
    url: str,
    identifiers: Sequence[tuple[str, str, str]] = (),
    weak: int = 0,
    jurisdiction: str = "CT",
) -> tuple[int, int]:
    """A court record with its source page and extracted text captured; returns the two capture ids."""
    page = _capture(
        conn, config, row_id, f"<html>{text}</html>".encode(), url=url, content_type="text/html"
    )
    plain = _capture(
        conn, config, row_id, text.encode(), url=url + "#text", content_type="text/plain"
    )
    conn.execute(
        "INSERT INTO court_records (row_id, source_kind, jurisdiction, court, case_number, "
        "document_kind, strategy, capture_id, found_at, weak, current, text_capture_id) "
        "VALUES (?, 'doj_release', ?, 'Synthetic District', ?, 'release', 'court.doj', ?, ?, ?, 1, ?)",
        (row_id, jurisdiction, case_number, page, STAMP, weak, plain),
    )
    for kind, value, span in identifiers:
        conn.execute(
            "INSERT INTO identifiers (row_id, kind, value, capture_id, span) VALUES (?,?,?,?,?)",
            (row_id, kind, value, plain, span),
        )
    conn.commit()
    return page, plain


def seed_link(conn: sqlite3.Connection, config: Config, row_id: int, html: str, url: str) -> int:
    capture = _capture(
        conn,
        config,
        row_id,
        html.encode(),
        url=url,
        content_type="text/html",
        stage="resolve_links",
    )
    conn.execute(
        "INSERT INTO links (row_id, url_raw, url_expanded, status, capture_id) "
        "VALUES (?, ?, ?, 'live', ?)",
        (row_id, url, url, capture),
    )
    conn.commit()
    return capture


def seed_ref_source(
    conn: sqlite3.Connection, dataset: str, *, notes: dict[str, object] | None = None
) -> int:
    cursor = conn.execute(
        "INSERT INTO ref_sources (dataset, status, current, loaded_at, notes_json, row_count) "
        "VALUES (?, 'loaded', 1, ?, ?, 1)",
        (dataset, STAMP, json.dumps(notes) if notes else None),
    )
    return int(cursor.lastrowid or 0)


def seed_candidate(
    conn: sqlite3.Connection,
    source: str,
    source_id: str,
    surname: str,
    given: str,
    *,
    middle: str = "",
    state: str = "CT",
) -> None:
    load = seed_ref_source(
        conn, {"fec": "fec_candidates", "state_leg": "openstates"}.get(source, source)
    )
    conn.execute(
        "INSERT INTO ref_candidates_all (source, source_id, load_id, surname, given, middle, "
        "surname_key, state, office, party, party_imputed, years, first_year, last_year) "
        "VALUES (?,?,?,?,?,?,?,?,'Synthetic office','',0,'2010-2018',2010,2018)",
        (
            source,
            source_id,
            load,
            surname,
            given,
            middle,
            "".join(c for c in surname.upper() if c.isalpha()),
            state,
        ),
    )
    conn.commit()


def seed_reference(conn: sqlite3.Connection) -> None:
    """Census surnames, SSA names (national) and a one-state population, all synthetic.

    Population of ``CT``: 1,000,000 people in each five-year age band from 0 to 85+ (so a one-year
    birth cohort inside a band is 200,000 people and all adults is 14 bands x 1,000,000 minus the
    18-19 share). SSA: each birth year 1900-2010 has 1,000,000 births of each sex; JOHN has 50,000
    male births a year (5%), MARY 40,000 female births, ZELDA 100 female births, and ALEX 10,000
    of each sex (ambiguous).
    """
    load = seed_ref_source(conn, "census_surnames", notes={"floor_prop100k": FLOOR_PROP100K})
    for rank, (name, prop) in enumerate(sorted(SURNAME_PROP100K.items(), key=lambda kv: -kv[1]), 1):
        conn.execute(
            "INSERT INTO ref_surname_freq VALUES (?,?,?,?,?)",
            (load, name, rank, int(prop * 100), prop),
        )
    ssa = seed_ref_source(conn, "ssa_names")
    for year in range(1900, 2011):
        for name, sex, count in (
            ("John", "M", 50_000),
            ("Mary", "F", 40_000),
            ("Zelda", "F", 100),
            ("Alex", "M", 10_000),
            ("Alex", "F", 10_000),
            ("Filler", "M", 940_000),
            ("Filler", "F", 949_900),
        ):
            conn.execute(
                "INSERT INTO ref_given_freq VALUES (?,?,?,?,NULL,?)", (ssa, name, sex, year, count)
            )
    pop = seed_ref_source(conn, "census_population")
    bands = [f"{a}-{a + 4}" for a in range(0, 85, 5)] + ["85+"]
    for band in bands:
        conn.execute(
            "INSERT INTO ref_population VALUES (?,?,?,?,?)", (pop, "CT", band, 2023, 1_000_000)
        )
    conn.commit()

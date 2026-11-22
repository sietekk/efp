from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from tests.conftest import REPO
from tests.dbhelpers import STAMP, ZERO, insert_capture, insert_decision, insert_row

CORE_TABLES = (
    "rows",
    "case_stage",
    "captures",
    "identifiers",
    "identity_candidates",
    "decisions",
    "extractions",
    "codes",
)


def _spec_sql() -> str:
    text = (REPO / "docs" / "spec.md").read_text(encoding="utf-8")
    for block in re.findall(r"```sql\n(.*?)```", text, flags=re.S):
        if "CREATE TABLE rows" in block:
            return str(block)
    raise AssertionError("docs/spec.md section 3 SQL block not found")


def _normalised_ddl(conn: sqlite3.Connection, table: str) -> str:
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0]
    sql = re.sub(r"--[^\n]*", "", sql)
    return re.sub(r"\s+", " ", sql).strip().rstrip(";")


@pytest.mark.parametrize("table", CORE_TABLES)
def test_core_ddl_matches_spec_section_3(table: str) -> None:
    """0001_core.sql is the spec's SQL: same columns, types, CHECK lists, keys, comments aside."""
    spec = sqlite3.connect(":memory:")
    spec.executescript(_spec_sql())
    ours = sqlite3.connect(":memory:")
    ours.executescript((REPO / "migrations" / "0001_core.sql").read_text(encoding="utf-8"))
    assert _normalised_ddl(ours, table) == _normalised_ddl(spec, table)
    assert (
        ours.execute(f"PRAGMA table_info({table})").fetchall()
        == spec.execute(f"PRAGMA table_info({table})").fetchall()
    )
    assert (
        ours.execute(f"PRAGMA foreign_key_list({table})").fetchall()
        == spec.execute(f"PRAGMA foreign_key_list({table})").fetchall()
    )


def test_core_migration_adds_only_indexes_to_the_spec() -> None:
    spec = sqlite3.connect(":memory:")
    spec.executescript(_spec_sql())
    ours = sqlite3.connect(":memory:")
    ours.executescript((REPO / "migrations" / "0001_core.sql").read_text(encoding="utf-8"))
    kinds = lambda c: sorted(  # noqa: E731
        (r[0], r[1])
        for r in c.execute("SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")
    )
    extra = set(kinds(ours)) - set(kinds(spec))
    assert extra and all(kind == "index" for kind, _ in extra)


def _expect_rejected(conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, params)


def test_case_stage_stage_and_status_checks(conn: sqlite3.Connection) -> None:
    stages = (
        "resolve_links",
        "court",
        "identity",
        "news",
        "documents",
        "social",
        "extract",
        "code",
    )
    statuses = ("pending", "running", "done", "failed", "needs_review", "skipped")
    insert_row(conn)
    for stage in stages:
        conn.execute("INSERT INTO case_stage (row_id, stage) VALUES (1, ?)", (stage,))
    assert conn.execute("SELECT DISTINCT status FROM case_stage").fetchall() == [("pending",)]
    for status in statuses:
        conn.execute(
            "UPDATE case_stage SET status = ? WHERE row_id = 1 AND stage = 'court'", (status,)
        )
    for bad in ("ingest", "export", "COURT", ""):
        _expect_rejected(conn, "INSERT INTO case_stage (row_id, stage) VALUES (1, ?)", (bad,))
    _expect_rejected(conn, "UPDATE case_stage SET status = 'complete' WHERE stage = 'court'")


def test_identifier_kind_check(conn: sqlite3.Connection) -> None:
    insert_row(conn)
    insert_capture(conn)
    kinds = (
        "middle_name",
        "dob",
        "age",
        "city",
        "case_number",
        "office",
        "employer",
        "party_on_ballot",
        "handle",
        "email",
        "alias",
    )
    for kind in kinds:
        conn.execute(
            "INSERT INTO identifiers (row_id, kind, value, capture_id) VALUES (1, ?, 'v', 1)",
            (kind,),
        )
    _expect_rejected(
        conn, "INSERT INTO identifiers (row_id, kind, value, capture_id) VALUES (1, 'ssn', 'v', 1)"
    )


def test_decision_kind_check(conn: sqlite3.Connection) -> None:
    insert_row(conn)
    for i, kind in enumerate(("identity", "code", "review", "override"), start=1):
        conn.execute(
            "INSERT INTO decisions (decision_id, row_id, kind, outcome, decided_by, decided_at) "
            "VALUES (?, 1, ?, 'o', 'XX', ?)",
            (i, kind, STAMP),
        )
    _expect_rejected(
        conn,
        "INSERT INTO decisions (row_id, kind, outcome, decided_by, decided_at) "
        "VALUES (1, 'guess', 'o', 'XX', ?)",
        (STAMP,),
    )


def test_extraction_alignment_status_check(conn: sqlite3.Connection) -> None:
    insert_row(conn)
    insert_capture(conn)
    good = ("match_exact", "match_greater", "match_lesser", "match_fuzzy", "unaligned", None)
    for status in good:
        conn.execute(
            "INSERT INTO extractions (row_id, capture_id, field, alignment_status, extracted_at) "
            "VALUES (1, 1, 'party', ?, ?)",
            (status, STAMP),
        )
    _expect_rejected(
        conn,
        "INSERT INTO extractions (row_id, capture_id, field, alignment_status, extracted_at) "
        "VALUES (1, 1, 'party', 'close_enough', ?)",
        (STAMP,),
    )


def test_codes_evidence_tier_check(conn: sqlite3.Connection) -> None:
    insert_row(conn)
    tiers = (
        "stated_by_subject",
        "court_document",
        "news_reporting",
        "circumstantial",
        "none",
        None,
    )
    for i, tier in enumerate(tiers):
        conn.execute(
            "INSERT INTO codes (row_id, column_name, evidence_tier, codebook_version) "
            "VALUES (1, ?, ?, 'v1')",
            (f"col{i}", tier),
        )
    _expect_rejected(
        conn,
        "INSERT INTO codes (row_id, column_name, evidence_tier, codebook_version) "
        "VALUES (1, 'bad', 'rumour', 'v1')",
    )


def test_links_enums(conn: sqlite3.Connection) -> None:
    insert_row(conn)
    methods = (
        "live",
        "bitly_info",
        "wayback_short",
        "wayback_target",
        "commoncrawl",
        "unresolved",
        None,
    )
    for i, method in enumerate(methods):
        conn.execute(
            "INSERT INTO links (row_id, url_raw, expansion_method, status) "
            "VALUES (1, ?, ?, 'unresolved')",
            (f"https://example.test/{i}", method),
        )
    for status in ("live", "archived", "dead", "unresolved"):
        conn.execute(
            "INSERT INTO links (row_id, url_raw, status) VALUES (1, ?, ?)",
            (f"https://example.test/s-{status}", status),
        )
    _expect_rejected(
        conn,
        "INSERT INTO links (row_id, url_raw, expansion_method, status) "
        "VALUES (1, 'u', 'guessed', 'live')",
    )
    _expect_rejected(conn, "INSERT INTO links (row_id, url_raw, status) VALUES (1, 'u', 'maybe')")


def test_a_link_is_unique_per_row_and_url_and_carries_a_note(conn: sqlite3.Connection) -> None:
    insert_row(conn)
    conn.execute(
        "INSERT INTO links (row_id, url_raw, status, note) VALUES (1, 'https://e.test/a', 'dead', 'x')"
    )
    _expect_rejected(
        conn, "INSERT INTO links (row_id, url_raw, status) VALUES (1, 'https://e.test/a', 'live')"
    )
    insert_row(conn, 2)
    conn.execute(
        "INSERT INTO links (row_id, url_raw, status) VALUES (2, 'https://e.test/a', 'live')"
    )


def test_court_record_source_kind(conn: sqlite3.Connection) -> None:
    insert_row(conn)
    kinds = (
        "doj_release",
        "state_ag_release",
        "da_release",
        "federal_docket",
        "state_docket",
        "pin_report",
        "other",
    )
    for kind in kinds:
        conn.execute(
            "INSERT INTO court_records (row_id, source_kind, found_at) VALUES (1, ?, ?)",
            (kind, STAMP),
        )
    _expect_rejected(
        conn,
        "INSERT INTO court_records (row_id, source_kind, found_at) VALUES (1, 'rumor', ?)",
        (STAMP,),
    )


def test_articles_source_kind(conn: sqlite3.Connection) -> None:
    insert_row(conn)
    for kind in ("automated", "manual"):
        conn.execute("INSERT INTO articles (row_id, source_kind) VALUES (1, ?)", (kind,))
    _expect_rejected(conn, "INSERT INTO articles (row_id, source_kind) VALUES (1, 'scraped')")


def test_gold_set_split(conn: sqlite3.Connection) -> None:
    insert_row(conn)
    for split in ("dev", "heldout"):
        conn.execute(
            "INSERT INTO gold_set (row_id, field, split, set_version, set_sha256) "
            "VALUES (1, 'party', ?, 'v1', ?)",
            (split, ZERO),
        )
    _expect_rejected(
        conn,
        "INSERT INTO gold_set (row_id, field, split, set_version, set_sha256) "
        "VALUES (1, 'party', 'train', 'v1', ?)",
        (ZERO,),
    )


def test_state_adapters_mode_and_kind(conn: sqlite3.Connection) -> None:
    for i, mode in enumerate(("automated", "assisted", "manual", "none")):
        conn.execute(
            "INSERT INTO state_adapters (state, kind, mode) VALUES (?, 'ag', ?)", (f"S{i}", mode)
        )
    conn.execute("INSERT INTO state_adapters (state, kind, mode) VALUES ('S0', 'docket', 'none')")
    _expect_rejected(
        conn, "INSERT INTO state_adapters (state, kind, mode) VALUES ('S9', 'ag', 'semi')"
    )
    _expect_rejected(
        conn, "INSERT INTO state_adapters (state, kind, mode) VALUES ('S9', 'court', 'none')"
    )
    _expect_rejected(
        conn, "INSERT INTO state_adapters (state, kind, mode) VALUES ('S0', 'ag', 'none')"
    )


def test_documents_ocr_flag_is_boolean(conn: sqlite3.Connection) -> None:
    insert_row(conn)
    conn.execute("INSERT INTO documents (row_id, ocr_applied) VALUES (1, 1)")
    _expect_rejected(conn, "INSERT INTO documents (row_id, ocr_applied) VALUES (1, 2)")


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO case_stage (row_id, stage) VALUES (99, 'court')",
        "INSERT INTO identifiers (row_id, kind, value, capture_id) VALUES (99, 'city', 'x', 1)",
        "INSERT INTO identifiers (row_id, kind, value, capture_id) VALUES (1, 'city', 'x', 99)",
        "INSERT INTO captures (row_id, stage, url_requested, retrieved_at, content_sha256, body_path,"
        " tool, tool_version) VALUES (99, 'court', 'u', 't', 'h', 'b', 'x', '0')",
        "INSERT INTO links (row_id, url_raw, status) VALUES (99, 'u', 'live')",
        "INSERT INTO documents (row_id, record_id) VALUES (1, 99)",
        "INSERT INTO codes (row_id, column_name, extraction_id, codebook_version) VALUES (1, 'c', 99, 'v')",
    ],
)
def test_foreign_keys_reject_orphans(conn: sqlite3.Connection, sql: str) -> None:
    insert_row(conn)
    insert_capture(conn)
    _expect_rejected(conn, sql)


def test_a_capture_may_have_no_row(conn: sqlite3.Connection) -> None:
    insert_capture(conn, row_id=None, stage="reference")
    assert conn.execute("SELECT row_id FROM captures").fetchone() == (None,)


def test_every_foreign_key_column_is_indexed(conn: sqlite3.Connection) -> None:
    """Each foreign-key column is the leading column of some index or of the primary key."""
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    unindexed: list[str] = []
    for table in tables:
        leading: set[str] = set()
        for _seq, name, *_ in conn.execute(f"PRAGMA index_list({table})").fetchall():
            first = conn.execute(f"PRAGMA index_info({name})").fetchone()
            if first:
                leading.add(first[2])
        pk = sorted(
            (r for r in conn.execute(f"PRAGMA table_info({table})") if r[5]), key=lambda r: r[5]
        )
        if pk:
            leading.add(pk[0][1])
        for fk in conn.execute(f"PRAGMA foreign_key_list({table})"):
            if fk[3] not in leading:
                unindexed.append(f"{table}.{fk[3]}")
    assert unindexed == []


def test_required_indexes_exist(conn: sqlite3.Connection) -> None:
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {
        "idx_case_stage_stage_status",
        "idx_captures_content_sha256",
        "idx_captures_row_stage",
        "idx_identifiers_row_kind",
        "idx_codes_column_name",
    } <= names


def test_reference_tables_are_not_created_here(conn: sqlite3.Connection) -> None:
    assert not [
        r
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'ref\\_%' ESCAPE '\\'"
        )
    ]


def test_decision_helper_round_trip(conn: sqlite3.Connection) -> None:
    insert_row(conn)
    insert_decision(conn)
    assert conn.execute("SELECT outcome FROM decisions").fetchone() == ("confirmed",)


def test_migrations_readme_documents_postgres_and_ref_names() -> None:
    text = Path(REPO / "migrations" / "README.md").read_text(encoding="utf-8")
    for needle in ("TIMESTAMPTZ", "JSONB", "BIGSERIAL", "ref_dime", "ref_population", "codes"):
        assert needle in text

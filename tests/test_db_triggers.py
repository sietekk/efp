from __future__ import annotations

import sqlite3

import pytest

from efp import db as database
from efp.config import Config
from tests.dbhelpers import STAMP, ZERO, insert_audit, insert_capture, insert_decision, insert_row

APPEND_ONLY = "append-only"


@pytest.fixture
def seeded(conn: sqlite3.Connection) -> sqlite3.Connection:
    insert_row(conn)
    insert_capture(conn)
    insert_decision(conn)
    insert_audit(conn)
    conn.commit()
    return conn


def _refused(conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=APPEND_ONLY):
        conn.execute(sql, params)


@pytest.mark.parametrize(
    ("table", "key", "update"),
    [
        ("rows", "row_id", "name = 'changed'"),
        ("captures", "capture_id", "stage = 'changed'"),
        ("decisions", "decision_id", "outcome = 'changed'"),
        ("audit_log", "audit_id", "command = 'changed'"),
    ],
)
def test_update_and_delete_are_rejected(
    seeded: sqlite3.Connection, table: str, key: str, update: str
) -> None:
    _refused(seeded, f"UPDATE {table} SET {update} WHERE {key} = 1")
    _refused(seeded, f"DELETE FROM {table} WHERE {key} = 1")
    _refused(seeded, f"UPDATE {table} SET {update}")  # unqualified
    _refused(seeded, f"DELETE FROM {table}")  # unqualified


def test_insert_or_replace_is_rejected_on_rows(seeded: sqlite3.Connection) -> None:
    _refused(
        seeded,
        "INSERT OR REPLACE INTO rows (row_id, year, state, name, in_scope, row_sha256, ingested_at) "
        "VALUES (1, 2011, 'NY', 'Replaced Person', 1, ?, ?)",
        (ZERO, STAMP),
    )
    assert seeded.execute("SELECT name FROM rows WHERE row_id = 1").fetchone() == (
        "Synthetic Person",
    )


def test_replace_and_upsert_are_rejected_on_every_append_only_table(
    seeded: sqlite3.Connection,
) -> None:
    _refused(
        seeded,
        "REPLACE INTO captures (capture_id, row_id, stage, url_requested, retrieved_at, "
        "content_sha256, body_path, tool, tool_version) "
        "VALUES (1, 1, 'x', 'u', ?, ?, 'b', 't', '0')",
        (STAMP, ZERO),
    )
    _refused(
        seeded,
        "INSERT INTO decisions (decision_id, row_id, kind, outcome, decided_by, decided_at) "
        "VALUES (1, 1, 'review', 'flipped', 'XX', ?) "
        "ON CONFLICT(decision_id) DO UPDATE SET outcome = excluded.outcome",
        (STAMP,),
    )
    _refused(
        seeded,
        "INSERT OR REPLACE INTO audit_log (audit_id, invoked_at, command, args_json) "
        "VALUES (1, ?, 'x', '[]')",
        (STAMP,),
    )


@pytest.mark.parametrize("recursive", ["ON", "OFF"])
def test_replace_is_rejected_whatever_the_recursive_triggers_setting(
    config: Config, recursive: str
) -> None:
    """The insert triggers close the REPLACE hole independently of PRAGMA recursive_triggers."""
    conn = database.connect(config)
    insert_row(conn)
    conn.commit()
    conn.execute(f"PRAGMA recursive_triggers = {recursive}")
    _refused(
        conn,
        "INSERT OR REPLACE INTO rows (row_id, year, state, name, in_scope, row_sha256, ingested_at) "
        "VALUES (1, 2011, 'NY', 'Replaced Person', 1, ?, ?)",
        (ZERO, STAMP),
    )
    assert conn.execute("SELECT name FROM rows").fetchone() == ("Synthetic Person",)
    conn.close()


def _raw(config: Config) -> sqlite3.Connection:
    """A connection that did NOT go through efp.db.connect(): default pragmas, nothing set."""
    return sqlite3.connect(config.paths.database)


def test_a_raw_connection_cannot_replace_protected_rows(config: Config) -> None:
    conn = database.connect(config)
    insert_row(conn)
    insert_capture(conn)
    insert_decision(conn)
    insert_audit(conn)
    conn.commit()
    conn.close()
    raw = _raw(config)
    assert raw.execute("PRAGMA recursive_triggers").fetchone()[0] == 0  # the premise
    for sql in (
        "REPLACE INTO rows (row_id, year, state, name, in_scope, row_sha256, ingested_at) "
        f"VALUES (1, 1999, 'XX', 'CLIREPLACED', 1, '{ZERO}', '{STAMP}')",
        "INSERT OR REPLACE INTO captures (capture_id, stage, url_requested, retrieved_at, "
        f"content_sha256, body_path, tool, tool_version) VALUES (1, 'x', 'u', '{STAMP}', '{ZERO}', "
        "'b', 't', '0')",
        "INSERT OR REPLACE INTO decisions (decision_id, row_id, kind, outcome, decided_by, "
        f"decided_at) VALUES (1, 1, 'review', 'REVERSED-IN-PLACE', 'XX', '{STAMP}')",
        f"REPLACE INTO audit_log (audit_id, invoked_at, command, args_json) VALUES (1, '{STAMP}', "
        "'x', '[]')",
        "INSERT INTO rows (row_id, year, state, name, in_scope, row_sha256, ingested_at) "
        f"VALUES (1, 1999, 'XX', 'DUPLICATE', 1, '{ZERO}', '{STAMP}')",
    ):
        with pytest.raises(sqlite3.IntegrityError, match=APPEND_ONLY):
            raw.execute(sql)
    assert raw.execute("SELECT name FROM rows").fetchone() == ("Synthetic Person",)
    assert raw.execute("SELECT outcome FROM decisions").fetchone() == ("confirmed",)
    raw.close()


def test_a_raw_connection_can_still_append_new_rows(config: Config) -> None:
    conn = database.connect(config)
    insert_row(conn)
    conn.commit()
    conn.close()
    raw = _raw(config)
    raw.execute(  # NULL key: SQLite assigns the next id, which must not trip the trigger
        "INSERT INTO rows (year, state, name, in_scope, row_sha256, ingested_at) "
        f"VALUES (2012, 'NY', 'Another Synthetic', 1, '{ZERO}', '{STAMP}')"
    )
    raw.commit()
    assert raw.execute("SELECT count(*) FROM rows").fetchone() == (2,)
    raw.close()


def test_inserts_still_work(seeded: sqlite3.Connection) -> None:
    insert_row(seeded, 2)
    insert_capture(seeded, 2, 2)
    insert_decision(seeded, 2, 2)
    insert_audit(seeded, 2)
    assert seeded.execute("SELECT count(*) FROM rows").fetchone() == (2,)


def test_audit_completion_is_allowed_exactly_once(seeded: sqlite3.Connection) -> None:
    seeded.execute(
        "UPDATE audit_log SET finished_at = ?, exit_code = 0 WHERE audit_id = 1", (STAMP,)
    )
    assert seeded.execute("SELECT exit_code, finished_at FROM audit_log").fetchone() == (0, STAMP)
    _refused(
        seeded, "UPDATE audit_log SET finished_at = ?, exit_code = 1 WHERE audit_id = 1", (STAMP,)
    )
    _refused(seeded, "UPDATE audit_log SET exit_code = 9 WHERE audit_id = 1")


@pytest.mark.parametrize(
    "update",
    [
        "args_json = '[\"tampered\"]', finished_at = '2026-01-02', exit_code = 0",
        "git_sha = 'deadbee', finished_at = '2026-01-02', exit_code = 0",
        "invoked_at = '2020-01-01', finished_at = '2026-01-02', exit_code = 0",
        "audit_id = 7, finished_at = '2026-01-02', exit_code = 0",
        "finished_at = '2026-01-02'",  # exit_code left NULL
        "exit_code = 0",  # finished_at left NULL
        "finished_at = NULL, exit_code = NULL",
    ],
)
def test_audit_completion_cannot_change_anything_else(
    seeded: sqlite3.Connection, update: str
) -> None:
    _refused(seeded, f"UPDATE audit_log SET {update} WHERE audit_id = 1")


def test_codes_and_case_stage_remain_writable(seeded: sqlite3.Connection) -> None:
    seeded.execute("INSERT INTO case_stage (row_id, stage) VALUES (1, 'court')")
    seeded.execute("UPDATE case_stage SET status = 'running', attempts = 1 WHERE row_id = 1")
    seeded.execute(
        "INSERT INTO codes (row_id, column_name, value, codebook_version) VALUES (1, 'motive', 'a', 'v1')"
    )
    seeded.execute("UPDATE codes SET value = 'b', codebook_version = 'v2' WHERE row_id = 1")
    seeded.execute("DELETE FROM codes WHERE row_id = 1")
    seeded.execute("DELETE FROM case_stage WHERE row_id = 1")
    assert seeded.execute("SELECT count(*) FROM codes").fetchone() == (0,)


def test_other_tables_in_the_core_set_are_unprotected_by_design(seeded: sqlite3.Connection) -> None:
    """identifiers and extractions are not on the spec's append-only list; this pins that."""
    seeded.execute(
        "INSERT INTO identifiers (row_id, kind, value, capture_id) VALUES (1, 'city', 'x', 1)"
    )
    seeded.execute("UPDATE identifiers SET value = 'y' WHERE row_id = 1")


WORKBOOK_ROW = (
    "INSERT INTO source_workbook (workbook_id, sha256, filename, sheet_name, header_row, "
    "data_rows, year_from, year_to, ingested_at) VALUES (?, ?, 'w.xlsx', 'Cases', 4, 20, 2000, 2025, ?)"
)


def test_source_workbook_is_one_immutable_row(conn: sqlite3.Connection) -> None:
    conn.execute(WORKBOOK_ROW, (1, ZERO, STAMP))
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):  # a second workbook is not allowed at all
        conn.execute(WORKBOOK_ROW, (2, ZERO, STAMP))
    for sql in (
        "UPDATE source_workbook SET sha256 = 'x'",
        "DELETE FROM source_workbook",
        "INSERT OR REPLACE INTO source_workbook (workbook_id, sha256, filename, sheet_name, "
        "header_row, data_rows, year_from, year_to, ingested_at) "
        "VALUES (1, 'x', 'x', 'x', 1, 1, 2000, 2025, 'x')",
    ):
        _refused(conn, sql)
    assert conn.execute("SELECT sha256 FROM source_workbook").fetchone() == (ZERO,)


def test_a_raw_connection_cannot_replace_the_workbook_record(config: Config) -> None:
    conn = database.connect(config)
    conn.execute(WORKBOOK_ROW, (1, ZERO, STAMP))
    conn.commit()
    conn.close()
    raw = sqlite3.connect(config.paths.database)
    assert raw.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
    with pytest.raises(sqlite3.IntegrityError, match=APPEND_ONLY):
        raw.execute(
            "REPLACE INTO source_workbook (workbook_id, sha256, filename, sheet_name, header_row, "
            f"data_rows, year_from, year_to, ingested_at) VALUES (1, 'x', 'x', 'x', 1, 1, 2000, 2025, '{STAMP}')"
        )
    raw.close()

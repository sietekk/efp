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


def test_recursive_triggers_is_what_closes_the_replace_hole(config: Config) -> None:
    """Documents why db.connect() sets the pragma: with it off, REPLACE bypasses DELETE triggers."""
    conn = database.connect(config)
    insert_row(conn)
    conn.commit()
    conn.execute("PRAGMA recursive_triggers = OFF")
    conn.execute(
        "INSERT OR REPLACE INTO rows (row_id, year, state, name, in_scope, row_sha256, ingested_at) "
        "VALUES (1, 2011, 'NY', 'Replaced Person', 1, ?, ?)",
        (ZERO, STAMP),
    )
    assert conn.execute("SELECT name FROM rows").fetchone() == ("Replaced Person",)
    conn.close()
    fresh = database.connect(config)
    assert fresh.execute("PRAGMA recursive_triggers").fetchone()[0] == 1
    fresh.close()


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

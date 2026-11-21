"""Synthetic inserts for database tests. No real subjects: every value is made up."""

from __future__ import annotations

import sqlite3
from pathlib import Path

ZERO = "0" * 64
STAMP = "2026-01-01T00:00:00+00:00"
REPO = Path(__file__).resolve().parents[1]


def insert_row(
    conn: sqlite3.Connection,
    row_id: int = 1,
    *,
    year: int = 2010,
    state: str = "CT",
    sha: str = ZERO,
) -> None:
    conn.execute(
        "INSERT INTO rows (row_id, year, state, name, in_scope, row_sha256, ingested_at) "
        "VALUES (?, ?, ?, 'Synthetic Person', 1, ?, ?)",
        (row_id, year, state, sha, STAMP),
    )


def insert_capture(
    conn: sqlite3.Connection,
    capture_id: int = 1,
    row_id: int | None = 1,
    *,
    stage: str = "court",
    content_sha256: str = ZERO,
    body_path: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO captures (capture_id, row_id, stage, url_requested, retrieved_at, "
        "content_sha256, body_path, tool, tool_version) "
        "VALUES (?, ?, ?, 'https://example.test/x', ?, ?, ?, 'test', '0')",
        (
            capture_id,
            row_id,
            stage,
            STAMP,
            content_sha256,
            body_path or f"store/{content_sha256[:2]}/{content_sha256}",
        ),
    )


def insert_decision(conn: sqlite3.Connection, decision_id: int = 1, row_id: int = 1) -> None:
    conn.execute(
        "INSERT INTO decisions (decision_id, row_id, kind, outcome, decided_by, decided_at) "
        "VALUES (?, ?, 'review', 'confirmed', 'XX', ?)",
        (decision_id, row_id, STAMP),
    )


def insert_audit(conn: sqlite3.Connection, audit_id: int = 1) -> None:
    conn.execute(
        "INSERT INTO audit_log (audit_id, invoked_at, command, args_json, git_sha) "
        "VALUES (?, ?, 'status', '[]', 'abc1234')",
        (audit_id, STAMP),
    )

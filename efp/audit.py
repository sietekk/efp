"""Audit-log hook: every CLI invocation leaves an ``audit_log`` row.

``start()`` inserts the row when the invocation begins (so a crashed run still leaves a trace);
``finish()`` fills ``finished_at`` and ``exit_code`` at exit. The table is append-only by
trigger, which permits exactly that one completion write and nothing else (migration 0003).
Arguments are redacted before they are stored. Both functions are silent no-ops when the
database or the table does not exist, never create either, and never raise.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from efp.config import Config
from efp.db import DatabaseError, connect
from efp.redact import redact_args
from efp.version import git_sha, lockfile_sha256

log = logging.getLogger("efp.audit")

INSERT_SQL = (
    "INSERT INTO audit_log "
    "(invoked_at, command, args_json, git_sha, lockfile_sha256, config_sha256) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)
# The one UPDATE the code base is allowed to issue against audit_log (see tests/test_db_guard.py).
COMPLETE_SQL = "UPDATE audit_log SET finished_at = ?, exit_code = ? WHERE audit_id = ?"


def command_of(redacted_args: Sequence[str]) -> str:
    """The subcommand words: the leading tokens up to the first flag."""
    words: list[str] = []
    for token in redacted_args:
        if token.startswith("-"):
            break
        words.append(token)
    return " ".join(words)[:200]


def _open(config: Config) -> sqlite3.Connection | None:
    try:
        conn = connect(config, migrate=False, create=False)
    except (DatabaseError, sqlite3.Error):
        log.debug("audit skipped: no database")
        return None
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'audit_log'"
    ).fetchone()
    if not has_table:
        conn.close()
        log.debug("audit skipped: audit_log table does not exist yet")
        return None
    return conn


def start(
    config: Config,
    argv: Sequence[str],
    started: datetime,
    *,
    config_sha256: str | None = None,
    secret_values: Iterable[str] = (),
) -> int | None:
    """Insert the invocation's row; return its ``audit_id``, or None if nothing was written."""
    try:
        conn = _open(config)
        if conn is None:
            return None
        try:
            safe = redact_args(argv, secret_values)
            cursor = conn.execute(
                INSERT_SQL,
                (
                    started.isoformat(),
                    command_of(safe),
                    json.dumps(safe),
                    git_sha(),
                    lockfile_sha256(),
                    config_sha256,
                ),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.warning("audit row not written: %s", type(exc).__name__)
        return None


def finish(config: Config, audit_id: int | None, exit_code: int) -> bool:
    """Complete the row opened by :func:`start`. Returns True if it was updated."""
    if audit_id is None:
        return False
    try:
        conn = _open(config)
        if conn is None:
            return False
        try:
            conn.execute(COMPLETE_SQL, (datetime.now(UTC).isoformat(), exit_code, audit_id))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.warning("audit row not completed: %s", type(exc).__name__)
        return False
    return True

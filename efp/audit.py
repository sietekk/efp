"""Audit-log hook: every CLI invocation writes one ``audit_log`` row.

Build 02 creates the table; until then (or when the database does not exist) recording is a
no-op. Expected columns: ``args_json, git_sha, lockfile_sha256, started_at, ended_at,
exit_code``. Build 02 must match these names or adjust this module. No CLI flag takes a secret,
so argument values never hold one.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime

from efp.config import Config
from efp.version import git_sha, lockfile_sha256

log = logging.getLogger("efp.audit")


def record(
    config: Config,
    argv: Sequence[str],
    started: datetime,
    exit_code: int,
) -> bool:
    """Insert one audit row. Returns True if written, False if skipped. Never raises."""
    db_path = config.paths.database
    if not db_path.is_file():
        log.debug("audit skipped: no database at configured path")
        return False
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        try:
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'audit_log'"
            ).fetchone()
            if not has_table:
                log.debug("audit skipped: audit_log table not created yet (build 02)")
                return False
            conn.execute(
                "INSERT INTO audit_log "
                "(args_json, git_sha, lockfile_sha256, started_at, ended_at, exit_code) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    json.dumps(list(argv)),
                    git_sha(),
                    lockfile_sha256(),
                    started.isoformat(),
                    datetime.now(UTC).isoformat(),
                    exit_code,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.warning("audit row not written: %s", type(exc).__name__)
        return False
    return True

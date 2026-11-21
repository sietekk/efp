"""Database: connection setup, numbered migrations and the append-only guarantees.

The database lives only under the data directory named by ``paths.database``; it is never
created inside the repository. Migrations are ``migrations/NNNN_name.sql``, applied in order,
each inside one transaction, and recorded in ``schema_migrations`` with the file's SHA-256 so a
later edit of an applied migration is detected.

Every connection turns on ``foreign_keys`` and ``recursive_triggers``. The second one matters:
``INSERT OR REPLACE`` deletes the conflicting row, and SQLite only fires DELETE triggers for that
when recursive triggers are on, so without it the append-only triggers could be bypassed.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from efp.config import Config
from efp.version import repo_root

_MIGRATION_FILE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")

SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL,
  sha256     TEXT NOT NULL,
  applied_at TEXT NOT NULL
)
"""


class DatabaseError(Exception):
    """The database cannot be opened or created where it was asked to be."""


class MigrationError(Exception):
    """A migration is malformed, was changed after being applied, or failed."""


@dataclass(frozen=True)
class Migration:
    id: int
    name: str
    path: Path
    sha256: str
    sql: str


@dataclass(frozen=True)
class MigrationStatus:
    applied: tuple[int, ...]
    pending: tuple[Migration, ...]
    changed: tuple[str, ...]  # applied, but the file's hash no longer matches
    missing: tuple[str, ...]  # recorded as applied, but there is no file


def migrations_dir() -> Path:
    root = repo_root()
    if root is None:
        raise MigrationError("migrations/ not found: efp must run from a repository checkout")
    return root / "migrations"


def discover_migrations(directory: Path | None = None) -> list[Migration]:
    """Read ``NNNN_name.sql`` files in order. Numbering must start at 1 with no gaps."""
    directory = directory if directory is not None else migrations_dir()
    found: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_FILE.match(path.name)
        if match is None:
            raise MigrationError(f"{path.name}: migration files are named NNNN_name.sql")
        raw = path.read_bytes()
        found.append(
            Migration(
                id=int(match.group(1)),
                name=match.group(2),
                path=path,
                sha256=hashlib.sha256(raw).hexdigest(),
                sql=raw.decode("utf-8"),
            )
        )
    for expected, migration in enumerate(found, start=1):
        if migration.id != expected:
            raise MigrationError(
                f"migration numbering must be 1..N without gaps or repeats; "
                f"expected {expected:04d}, found {migration.path.name}"
            )
    return found


def _check_location(db_path: Path) -> None:
    root = repo_root()
    if root is not None and db_path.resolve().is_relative_to(root.resolve()):
        raise DatabaseError(
            "refusing to place the database inside the repository; "
            "set paths.database in config.toml to a location under the data directory"
        )


def _configure(conn: sqlite3.Connection, busy_timeout_ms: int) -> None:
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA recursive_triggers = ON")
    conn.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)


def migration_status(conn: sqlite3.Connection, migrations: list[Migration]) -> MigrationStatus:
    """Compare the migrations recorded in the database with the files on disk."""
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    recorded: dict[int, tuple[str, str]] = {}
    if has_table:
        recorded = {
            int(r[0]): (str(r[1]), str(r[2]))
            for r in conn.execute("SELECT id, name, sha256 FROM schema_migrations")
        }
    by_id = {m.id: m for m in migrations}
    changed = tuple(
        f"{m.path.name}" for m in migrations if m.id in recorded and recorded[m.id][1] != m.sha256
    )
    missing = tuple(
        f"{i:04d}_{name}" for i, (name, _sha) in sorted(recorded.items()) if i not in by_id
    )
    pending = tuple(m for m in migrations if m.id not in recorded)
    return MigrationStatus(
        applied=tuple(sorted(recorded)), pending=pending, changed=changed, missing=missing
    )


def apply_migration(conn: sqlite3.Connection, migration: Migration) -> None:
    """Apply one migration atomically: the file's SQL and its schema_migrations row, or neither."""
    # name and sha256 are constrained (regex / hex), so embedding them is safe; executescript
    # cannot take parameters, and it must run the whole transaction because it would otherwise
    # COMMIT any transaction opened outside it.
    stamp = datetime.now(UTC).isoformat()
    script = (
        "BEGIN IMMEDIATE;\n"
        f"{migration.sql}\n"
        "INSERT INTO schema_migrations (id, name, sha256, applied_at) "
        f"VALUES ({migration.id}, '{migration.name}', '{migration.sha256}', '{stamp}');\n"
        "COMMIT;\n"
    )
    try:
        conn.executescript(script)
    except sqlite3.Error as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise MigrationError(f"{migration.path.name} failed and was rolled back: {exc}") from exc


def connect(config: Config, *, migrate: bool = True, create: bool = True) -> sqlite3.Connection:
    """Open the pipeline database.

    ``create=False`` never creates the file or its directory (read paths: selftest, audit).
    ``migrate=True`` applies unapplied migrations in order and raises if an applied migration
    changed or its file is gone; ``migrate=False`` leaves the schema alone.
    """
    db_path = config.paths.database
    _check_location(db_path)
    if create:
        parent = db_path.parent
        if not parent.exists():
            parent.mkdir(parents=True, mode=0o700)
            os.chmod(parent, 0o700)
        conn = sqlite3.connect(db_path, timeout=config.database.busy_timeout_ms / 1000)
    else:
        if not db_path.is_file():
            raise DatabaseError(f"database not found: {db_path}")
        conn = sqlite3.connect(
            f"file:{db_path}?mode=rw", uri=True, timeout=config.database.busy_timeout_ms / 1000
        )
    try:
        _configure(conn, config.database.busy_timeout_ms)
        if migrate:
            migrate_connection(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def migrate_connection(conn: sqlite3.Connection) -> list[Migration]:
    """Apply every unapplied migration in order; return the ones newly applied.

    Raises :class:`MigrationError` if an applied migration changed on disk or its file is gone.
    """
    conn.execute(SCHEMA_MIGRATIONS_DDL)
    conn.commit()
    migrations = discover_migrations()
    status = migration_status(conn, migrations)
    if status.changed:
        raise MigrationError(
            "applied migration(s) changed on disk (migrations are append-only; "
            f"add a new numbered file instead): {', '.join(status.changed)}"
        )
    if status.missing:
        raise MigrationError(
            f"database records migration(s) with no file: {', '.join(status.missing)}"
        )
    for pending in status.pending:
        apply_migration(conn, pending)
    return list(status.pending)


def migrate(config: Config) -> list[Migration]:
    """Create the database if needed and apply pending migrations (``efp db migrate``)."""
    conn = connect(config, migrate=False, create=True)
    try:
        return migrate_connection(conn)
    finally:
        conn.close()

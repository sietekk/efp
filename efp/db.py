"""Database: connection setup, numbered migrations and the append-only guarantees.

The database lives only under the data directory (``paths.data_dir``); it is never created inside
the repository or outside the data directory. Migrations are ``migrations/NNNN_name.sql``,
applied in order, each in one transaction that this module controls, and recorded in
``schema_migrations`` with the file's SHA-256 so a later edit of an applied migration is detected.

The append-only triggers do not depend on how a connection is configured: a BEFORE INSERT trigger
refuses REPLACE-style inserts, so a plain ``sqlite3`` shell cannot overwrite a protected row. What
no trigger can stop is schema change (``DROP TRIGGER``, ``ALTER TABLE``) from a raw connection; see
``migrations/README.md``. Every connection from :func:`connect` also turns on ``foreign_keys`` and
``recursive_triggers`` (defence in depth for any REPLACE path the insert triggers miss).
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from efp.config import Config
from efp.version import repo_root

_MIGRATION_FILE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
_LEADING_COMMENTS = re.compile(r"\A(\s|--[^\n]*\n?|/\*.*?\*/)*", re.S)
# Statements a migration may not contain: this module owns the transaction.
_TRANSACTION_CONTROL = {"begin", "commit", "end", "rollback", "savepoint", "release"}

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


def split_statements(sql: str, *, source: str = "migration") -> list[str]:
    """Split SQL into complete statements (trigger bodies stay whole).

    Raises :class:`MigrationError` for an unterminated statement or comment, which would
    otherwise swallow everything after it.
    """
    statements: list[str] = []
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer)
            buffer = ""
    if _LEADING_COMMENTS.sub("", buffer).strip():
        raise MigrationError(f"{source}: unterminated statement or comment at end of file")
    return statements


def _check_no_transaction_control(statements: list[str], source: str) -> None:
    for statement in statements:
        body = _LEADING_COMMENTS.sub("", statement).lstrip()
        first = re.match(r"[A-Za-z]+", body)
        if first and first.group(0).lower() in _TRANSACTION_CONTROL:
            raise MigrationError(
                f"{source}: migrations must not contain {first.group(0).upper()}; "
                "the migration runner owns the transaction"
            )


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


def _check_location(config: Config) -> None:
    db_path = config.paths.database.resolve()
    root = repo_root()
    if root is not None and db_path.is_relative_to(root.resolve()):
        raise DatabaseError(
            "refusing to place the database inside the repository; "
            "set paths.database in config.toml to a location under the data directory"
        )
    if not db_path.is_relative_to(config.paths.data_dir.resolve()):
        raise DatabaseError(
            "paths.database must be inside paths.data_dir: the database lives only under the "
            "data directory"
        )


def _enable_wal(conn: sqlite3.Connection, busy_timeout_ms: int) -> None:
    """Put the database in WAL mode, tolerating other connections doing the same.

    WAL is stored in the file, so once any connection has switched it later ones only read it.
    The switch itself needs a lock that SQLite refuses at once with "database is locked" when
    another connection is mid-switch (the busy timeout is not consulted), which is what happens
    when several processes open a brand-new database together. Retry until the timeout instead.
    """
    deadline = time.monotonic() + busy_timeout_ms / 1000
    delay = 0.005
    while True:
        if str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal":
            return
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.1)


def _configure(conn: sqlite3.Connection, busy_timeout_ms: int) -> None:
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    _enable_wal(conn, busy_timeout_ms)
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
        m.path.name for m in migrations if m.id in recorded and recorded[m.id][1] != m.sha256
    )
    missing = tuple(
        f"{i:04d}_{name}" for i, (name, _sha) in sorted(recorded.items()) if i not in by_id
    )
    pending = tuple(m for m in migrations if m.id not in recorded)
    return MigrationStatus(
        applied=tuple(sorted(recorded)), pending=pending, changed=changed, missing=missing
    )


def apply_migration(conn: sqlite3.Connection, migration: Migration) -> bool:
    """Apply one migration atomically; return False if another process already applied it.

    The statements and the ``schema_migrations`` row run in one explicit transaction taken with
    ``BEGIN IMMEDIATE``; the applied state is re-read under that lock so two concurrent runs
    cannot both apply it. Any error rolls everything back.
    """
    statements = split_statements(migration.sql, source=migration.path.name)
    _check_no_transaction_control(statements, migration.path.name)
    previous = conn.isolation_level
    conn.isolation_level = None  # this function issues BEGIN/COMMIT itself
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise MigrationError(
                f"{migration.path.name}: could not take the database write lock ({exc}); "
                "is another efp process running?"
            ) from exc
        try:
            row = conn.execute(
                "SELECT sha256 FROM schema_migrations WHERE id = ?", (migration.id,)
            ).fetchone()
            if row is not None:
                conn.execute("ROLLBACK")
                if row[0] != migration.sha256:
                    raise MigrationError(
                        f"{migration.path.name}: already applied with different content"
                    )
                return False
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (id, name, sha256, applied_at) VALUES (?, ?, ?, ?)",
                (migration.id, migration.name, migration.sha256, datetime.now(UTC).isoformat()),
            )
            conn.execute("COMMIT")
        except MigrationError:
            raise
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise MigrationError(
                f"{migration.path.name} failed and was rolled back: {exc}"
            ) from exc
        finally:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
    finally:
        conn.isolation_level = previous
    return True


def _iter_database_files(db_path: Path) -> Iterator[Path]:
    for suffix in ("", "-wal", "-shm"):
        candidate = db_path.with_name(db_path.name + suffix)
        if candidate.exists():
            yield candidate


def connect(config: Config, *, migrate: bool = True, create: bool = True) -> sqlite3.Connection:
    """Open the pipeline database.

    ``create=False`` never creates the file or its directory (read paths: selftest, audit).
    ``migrate=True`` applies unapplied migrations in order and raises if an applied migration
    changed or its file is gone; ``migrate=False`` leaves the schema alone. A database file this
    call creates is mode 0600.
    """
    _check_location(config)
    db_path = config.paths.database
    timeout = config.database.busy_timeout_ms / 1000
    created = False
    if create:
        parent = db_path.parent
        if not parent.exists():
            # exist_ok: another process may create the directory between the check and here
            parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            os.chmod(parent, 0o700)
        created = not db_path.exists()
        conn = sqlite3.connect(db_path, timeout=timeout)
    else:
        if not db_path.is_file():
            raise DatabaseError(f"database not found: {db_path}")
        # as_uri() percent-encodes '?', '#' and '%' so odd directory names cannot alter the URI.
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=rw", uri=True, timeout=timeout)
    try:
        _configure(conn, config.database.busy_timeout_ms)
        if created:
            for path in _iter_database_files(db_path):
                os.chmod(path, 0o600)
        if migrate:
            migrate_connection(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def migrate_connection(conn: sqlite3.Connection) -> list[Migration]:
    """Apply every unapplied migration in order; return the ones this call applied.

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
    applied: list[Migration] = []
    for pending in status.pending:
        if apply_migration(conn, pending):
            applied.append(pending)
    return applied


def migrate(config: Config) -> list[Migration]:
    """Create the database if needed and apply pending migrations (``efp db migrate``)."""
    conn = connect(config, migrate=False, create=True)
    try:
        return migrate_connection(conn)
    finally:
        conn.close()

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

from efp import db as database
from efp.config import Config, load_config
from tests.conftest import REPO


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_fresh_database_applies_all_migrations(config: Config) -> None:
    applied = database.migrate(config)
    assert [m.id for m in applied] == [1, 2, 3, 4]
    conn = database.connect(config, migrate=False, create=False)
    try:
        assert {"rows", "case_stage", "captures", "audit_log", "schema_migrations"} <= _tables(conn)
        recorded = conn.execute(
            "SELECT id, name, sha256 FROM schema_migrations ORDER BY id"
        ).fetchall()
        assert [r[1] for r in recorded] == ["core", "supporting", "triggers", "source_workbook"]
        assert all(len(r[2]) == 64 for r in recorded)
    finally:
        conn.close()


def test_connection_pragmas(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_second_run_is_a_no_op(config: Config) -> None:
    database.migrate(config)
    conn = database.connect(config, migrate=False, create=False)
    before = conn.execute(
        "SELECT id, sha256, applied_at FROM schema_migrations ORDER BY id"
    ).fetchall()
    conn.close()
    assert database.migrate(config) == []
    conn = database.connect(config)  # connect(migrate=True) is also a no-op
    after = conn.execute(
        "SELECT id, sha256, applied_at FROM schema_migrations ORDER BY id"
    ).fetchall()
    conn.close()
    assert before == after


def test_modified_migration_is_detected(config: Config, migrations_copy: Path) -> None:
    database.migrate(config)
    target = migrations_copy / "0001_core.sql"
    target.write_text(target.read_text(encoding="utf-8") + "\n-- sneaky edit\n", encoding="utf-8")
    with pytest.raises(database.MigrationError, match=r"0001_core\.sql"):
        database.connect(config)
    with pytest.raises(database.MigrationError, match="append-only"):
        database.migrate(config)


def test_deleted_migration_is_detected(config: Config, migrations_copy: Path) -> None:
    database.migrate(config)
    (migrations_copy / "0004_source_workbook.sql").unlink()
    with pytest.raises(database.MigrationError, match="0004_source_workbook"):
        database.connect(config)


def test_new_migration_is_applied_after_the_old_ones(config: Config, migrations_copy: Path) -> None:
    database.migrate(config)
    (migrations_copy / "0005_extra.sql").write_text(
        "CREATE TABLE extra (x INTEGER);", encoding="utf-8"
    )
    assert [m.name for m in database.migrate(config)] == ["extra"]


@pytest.mark.parametrize(
    "bad_name", ["0004_Extra.sql", "core.sql", "01_core.sql", "0004-extra.sql"]
)
def test_bad_migration_names_are_rejected(migrations_copy: Path, bad_name: str) -> None:
    (migrations_copy / bad_name).write_text("", encoding="utf-8")
    with pytest.raises(database.MigrationError, match="NNNN_name"):
        database.discover_migrations()


def test_numbering_gap_is_rejected(migrations_copy: Path) -> None:
    (migrations_copy / "0002_supporting.sql").unlink()
    with pytest.raises(database.MigrationError, match="without gaps"):
        database.discover_migrations()


def test_failed_migration_rolls_back_completely(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "0001_ok.sql").write_text("CREATE TABLE fine (x INTEGER);", encoding="utf-8")
    (broken / "0002_bad.sql").write_text(
        "CREATE TABLE half (x INTEGER); INSERT INTO nowhere VALUES (1);", encoding="utf-8"
    )
    monkeypatch.setattr(database, "migrations_dir", lambda: broken)
    with pytest.raises(database.MigrationError, match=r"0002_bad\.sql failed and was rolled back"):
        database.migrate(config)
    conn = database.connect(config, migrate=False, create=False)
    try:
        assert "fine" in _tables(conn)
        assert "half" not in _tables(conn)
        assert [r[0] for r in conn.execute("SELECT id FROM schema_migrations")] == [1]
        assert not conn.in_transaction
    finally:
        conn.close()


def test_database_inside_the_repository_is_refused(
    write_config: Callable[..., Path], data_dir: Path
) -> None:
    inside = REPO / "never_created.db"
    config = load_config(
        write_config({f'database = "{data_dir}/pipeline.db"': f'database = "{inside}"'})
    )
    with pytest.raises(database.DatabaseError, match="inside the repository"):
        database.connect(config)
    assert not inside.exists()


def test_parent_directory_is_created_with_mode_700(config: Config, data_dir: Path) -> None:
    assert not data_dir.exists()
    database.connect(config).close()
    assert stat.S_IMODE(os.stat(data_dir).st_mode) == 0o700


def test_read_only_open_never_creates_anything(config: Config, data_dir: Path) -> None:
    with pytest.raises(database.DatabaseError, match="not found"):
        database.connect(config, migrate=False, create=False)
    assert not data_dir.exists()


@pytest.mark.parametrize(
    "bad_sql",
    [
        "CREATE TABLE t (x INTEGER);\nCOMMIT;\nCREATE TABLE u (x INTEGER);",
        "BEGIN;\nCREATE TABLE t (x INTEGER);",
        "CREATE TABLE t (x INTEGER);\nEND;",
        "SAVEPOINT s;\nCREATE TABLE t (x INTEGER);",
        "CREATE TABLE t (x INTEGER);\nROLLBACK;",
        "-- comment first\nCOMMIT;",
    ],
)
def test_migrations_may_not_control_the_transaction(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_sql: str
) -> None:
    folder = tmp_path / "txn"
    folder.mkdir()
    (folder / "0001_bad.sql").write_text(bad_sql, encoding="utf-8")
    monkeypatch.setattr(database, "migrations_dir", lambda: folder)
    with pytest.raises(database.MigrationError, match="must not contain"):
        database.migrate(config)
    conn = database.connect(config, migrate=False, create=False)
    assert "t" not in _tables(conn)  # nothing leaked out of the rejected migration
    conn.close()


def test_an_unterminated_comment_is_rejected_not_swallowed(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "unterminated"
    folder.mkdir()
    (folder / "0001_bad.sql").write_text(
        "CREATE TABLE t (x INTEGER);\n/* never closed\nCREATE TABLE u (x INTEGER);",
        encoding="utf-8",
    )
    monkeypatch.setattr(database, "migrations_dir", lambda: folder)
    with pytest.raises(database.MigrationError, match="unterminated"):
        database.migrate(config)
    conn = database.connect(config, migrate=False, create=False)
    assert "t" not in _tables(conn)
    assert not conn.in_transaction
    conn.close()


def test_trigger_bodies_with_begin_end_are_allowed(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "trig"
    folder.mkdir()
    (folder / "0001_ok.sql").write_text(
        "CREATE TABLE t (x INTEGER);\n"
        "CREATE TRIGGER t_guard BEFORE DELETE ON t BEGIN SELECT RAISE(ABORT, 'no'); END;\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(database, "migrations_dir", lambda: folder)
    assert [m.name for m in database.migrate(config)] == ["ok"]


def test_a_migration_already_applied_by_another_process_is_not_applied_twice(
    config: Config,
) -> None:
    conn = database.connect(config)
    migration = database.discover_migrations()[0]
    assert database.apply_migration(conn, migration) is False  # re-read under the lock
    changed = database.Migration(
        migration.id, migration.name, migration.path, "f" * 64, migration.sql
    )
    with pytest.raises(database.MigrationError, match="different content"):
        database.apply_migration(conn, changed)
    conn.close()


def test_a_busy_database_is_a_clear_error(
    write_config: Callable[..., Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = load_config(write_config({"busy_timeout_ms = 5000": "busy_timeout_ms = 50"}))
    database.migrate(config)
    folder = tmp_path / "more"
    folder.mkdir()
    for m in database.discover_migrations():
        (folder / m.path.name).write_text(m.sql, encoding="utf-8")
    (folder / "0005_more.sql").write_text("CREATE TABLE more (x INTEGER);", encoding="utf-8")
    monkeypatch.setattr(database, "migrations_dir", lambda: folder)
    holder = sqlite3.connect(config.paths.database, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(database.MigrationError, match="write lock"):
            database.migrate(config)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert [m.name for m in database.migrate(config)] == ["more"]


def test_database_outside_the_data_directory_is_refused(
    write_config: Callable[..., Path], data_dir: Path, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere" / "pipeline.db"
    config = load_config(
        write_config({f'database = "{data_dir}/pipeline.db"': f'database = "{elsewhere}"'})
    )
    with pytest.raises(database.DatabaseError, match=r"inside paths\.data_dir"):
        database.connect(config)
    assert not elsewhere.parent.exists()


def test_a_created_database_is_private(config: Config) -> None:
    database.connect(config).close()
    for suffix in ("", "-wal", "-shm"):
        path = config.paths.database.with_name(config.paths.database.name + suffix)
        if path.exists():
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600, path.name


def test_a_path_with_uri_special_characters_does_not_create_stray_files(
    write_config: Callable[..., Path], data_dir: Path
) -> None:
    odd = data_dir / "we%ird?x#y" / "pipeline.db"
    config = load_config(
        write_config({f'database = "{data_dir}/pipeline.db"': f'database = "{odd}"'})
    )
    database.migrate(config)
    before = sorted(p.name for p in data_dir.rglob("*"))
    conn = database.connect(config, migrate=False, create=False)
    assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone() == (4,)
    conn.close()
    assert sorted(p.name for p in data_dir.rglob("*")) == before
    assert not (data_dir / "we").exists() and not (data_dir / "we%ird").is_file()

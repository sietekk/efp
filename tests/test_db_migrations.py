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
    assert [m.id for m in applied] == [1, 2, 3]
    conn = database.connect(config, migrate=False, create=False)
    try:
        assert {"rows", "case_stage", "captures", "audit_log", "schema_migrations"} <= _tables(conn)
        recorded = conn.execute(
            "SELECT id, name, sha256 FROM schema_migrations ORDER BY id"
        ).fetchall()
        assert [r[1] for r in recorded] == ["core", "supporting", "triggers"]
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
    (migrations_copy / "0003_triggers.sql").unlink()
    with pytest.raises(database.MigrationError, match="0003_triggers"):
        database.connect(config)


def test_new_migration_is_applied_after_the_old_ones(config: Config, migrations_copy: Path) -> None:
    database.migrate(config)
    (migrations_copy / "0004_extra.sql").write_text(
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

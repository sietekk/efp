from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from efp import db as database
from efp.cli import app
from efp.config import Config, load_config
from efp.dbcheck import (
    EXPECTED_INDEXES,
    EXPECTED_TABLES,
    EXPECTED_TRIGGERS,
    DbCheck,
    run_db_checks,
)
from efp.store import body_relpath, sha256_hex, store_path, write_body
from tests.dbhelpers import ZERO, insert_capture, insert_row

runner = CliRunner()

ALL_CHECKS = [
    "database file",
    "journal mode is WAL",
    "migrations match files",
    "tables present",
    "indexes present",
    "triggers present",
    "append-only triggers fire",
    "capture bodies exist and hash",
    "no orphan captures",
    "foreign keys hold",
    "rows.row_sha256 well-formed",
    "rows.row_sha256 matches the source workbook",
    "sqlite quick_check",
]


def _by_name(checks: list[DbCheck]) -> dict[str, DbCheck]:
    return {c.name: c for c in checks}


def _add_capture(conn: sqlite3.Connection, config: Config, body: bytes, capture_id: int = 1) -> str:
    digest = write_body(config.paths.store, body)
    insert_capture(conn, capture_id, 1, content_sha256=digest, body_path=body_relpath(digest))
    conn.commit()
    return digest


def test_fresh_database_passes_and_reports_every_check(
    conn: sqlite3.Connection, config: Config
) -> None:
    checks = run_db_checks(config)
    assert [c.name for c in checks] == ALL_CHECKS
    assert all(c.ok for c in checks), [c for c in checks if not c.ok]


def test_expected_lists_match_the_migrations(conn: sqlite3.Connection) -> None:
    def present(kind: str) -> set[str]:
        return {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,))
        }

    assert set(EXPECTED_TABLES) == present("table")
    assert set(EXPECTED_INDEXES) == {n for n in present("index") if not n.startswith("sqlite_")}
    assert set(EXPECTED_TRIGGERS) == present("trigger")


def test_a_missing_database_is_one_failing_check_and_nothing_is_created(
    config: Config, data_dir: Path
) -> None:
    checks = run_db_checks(config)
    assert [c.name for c in checks] == ["database file"]
    assert not checks[0].ok and "efp db migrate" in checks[0].detail
    assert not data_dir.exists()


def test_the_trigger_probe_leaves_nothing_behind(conn: sqlite3.Connection, config: Config) -> None:
    run_db_checks(config)
    for table in ("rows", "captures", "decisions", "audit_log"):
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)


def test_the_probe_does_not_disturb_real_data(conn: sqlite3.Connection, config: Config) -> None:
    insert_row(conn, 1)
    conn.commit()
    run_db_checks(config)
    assert conn.execute("SELECT row_id, name FROM rows").fetchall() == [(1, "Synthetic Person")]


def test_verified_bodies_are_counted(conn: sqlite3.Connection, config: Config) -> None:
    insert_row(conn)
    _add_capture(conn, config, b"one")
    _add_capture(conn, config, b"two", 2)
    check = _by_name(run_db_checks(config))["capture bodies exist and hash"]
    assert check.ok and "all 2 verified" in check.detail


def test_a_tampered_body_is_caught(conn: sqlite3.Connection, config: Config) -> None:
    insert_row(conn)
    digest = _add_capture(conn, config, b"original")
    path = store_path(digest, config.paths.store)
    path.chmod(0o600)
    path.write_bytes(b"edited")
    check = _by_name(run_db_checks(config))["capture bodies exist and hash"]
    assert not check.ok and "does not hash" in check.detail and "1:" in check.detail


def test_a_missing_body_is_caught(conn: sqlite3.Connection, config: Config) -> None:
    insert_row(conn)
    digest = _add_capture(conn, config, b"original")
    store_path(digest, config.paths.store).unlink()
    check = _by_name(run_db_checks(config))["capture bodies exist and hash"]
    assert not check.ok and "missing" in check.detail


def test_a_body_path_that_does_not_name_its_hash_is_caught(
    conn: sqlite3.Connection, config: Config
) -> None:
    insert_row(conn)
    digest = write_body(config.paths.store, b"x")
    other = sha256_hex(b"y")
    insert_capture(conn, 1, 1, content_sha256=other, body_path=body_relpath(digest))
    conn.commit()
    check = _by_name(run_db_checks(config))["capture bodies exist and hash"]
    assert not check.ok and "does not name content_sha256" in check.detail


def test_large_stores_are_sampled_and_the_count_is_reported(
    write_config: Callable[..., Path],
) -> None:
    config = load_config(
        write_config({"selftest_capture_sample = 200": "selftest_capture_sample = 2"})
    )
    conn = database.connect(config)
    insert_row(conn)
    for i in range(1, 5):
        _add_capture(conn, config, f"body {i}".encode(), i)
    conn.close()
    check = _by_name(run_db_checks(config))["capture bodies exist and hash"]
    assert check.ok and "sample of 2 of 4" in check.detail


def test_orphan_captures_are_caught_unless_reference_or_selftest(
    conn: sqlite3.Connection, config: Config
) -> None:
    insert_capture(conn, 1, None, stage="reference")
    insert_capture(conn, 2, None, stage="selftest", content_sha256="1" * 64)
    conn.commit()
    ok = _by_name(run_db_checks(config))["no orphan captures"]
    assert ok.ok
    insert_capture(conn, 3, None, stage="court", content_sha256="2" * 64)
    conn.commit()
    bad = _by_name(run_db_checks(config))["no orphan captures"]
    assert not bad.ok and "3" in bad.detail


def test_foreign_key_violations_are_caught(conn: sqlite3.Connection, config: Config) -> None:
    insert_row(conn)
    conn.commit()  # PRAGMA foreign_keys is ignored inside a transaction
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("INSERT INTO case_stage (row_id, stage) VALUES (99, 'court')")
    conn.commit()
    check = _by_name(run_db_checks(config))["foreign keys hold"]
    assert not check.ok and "case_stage" in check.detail


def test_a_malformed_row_hash_is_caught(conn: sqlite3.Connection, config: Config) -> None:
    insert_row(conn, 1)
    insert_row(conn, 2, sha="XYZ")
    conn.commit()
    check = _by_name(run_db_checks(config))["rows.row_sha256 well-formed"]
    assert not check.ok and "[2]" in check.detail


def test_a_changed_migration_file_is_caught(
    conn: sqlite3.Connection, config: Config, migrations_copy: Path
) -> None:
    conn.close()
    database.migrate(config)  # records the copy's hashes... they already match the originals
    target = migrations_copy / "0002_supporting.sql"
    target.write_text(target.read_text(encoding="utf-8") + "\n-- edited\n", encoding="utf-8")
    check = _by_name(run_db_checks(config))["migrations match files"]
    assert not check.ok and "0002_supporting.sql" in check.detail


def test_an_unapplied_migration_is_reported(
    conn: sqlite3.Connection, config: Config, migrations_copy: Path
) -> None:
    (migrations_copy / f"{len(database.discover_migrations()) + 1:04d}_new.sql").write_text(
        "CREATE TABLE later (x INTEGER);", encoding="utf-8"
    )
    check = _by_name(run_db_checks(config))["migrations match files"]
    assert not check.ok and "unapplied" in check.detail and "efp db migrate" in check.detail


def test_a_dropped_trigger_is_caught_and_data_checks_are_skipped(
    conn: sqlite3.Connection, config: Config
) -> None:
    conn.execute("DROP TRIGGER rows_no_delete")
    conn.commit()
    checks = _by_name(run_db_checks(config))
    assert (
        not checks["triggers present"].ok and "rows_no_delete" in checks["triggers present"].detail
    )
    assert not checks["data checks"].ok
    assert "append-only triggers fire" not in checks


def test_triggers_that_do_not_fire_are_caught(conn: sqlite3.Connection, config: Config) -> None:
    """A trigger that exists but does nothing (replaced body) must fail the 'fire' check."""
    conn.execute("DROP TRIGGER rows_no_update")
    conn.execute("CREATE TRIGGER rows_no_update BEFORE UPDATE ON rows BEGIN SELECT 1; END")
    conn.commit()
    check = _by_name(run_db_checks(config))["append-only triggers fire"]
    assert not check.ok and "rows UPDATE" in check.detail


def test_a_neutered_delete_trigger_is_caught_and_replace_stays_blocked(
    conn: sqlite3.Connection, config: Config
) -> None:
    conn.execute("DROP TRIGGER rows_no_delete")
    conn.execute("CREATE TRIGGER rows_no_delete BEFORE DELETE ON rows BEGIN SELECT 1; END")
    conn.commit()
    check = _by_name(run_db_checks(config))["append-only triggers fire"]
    assert not check.ok and "rows DELETE" in check.detail
    assert "REPLACE" not in check.detail, "the insert triggers still refuse REPLACE"


def test_a_dropped_replace_trigger_is_caught_by_name(
    conn: sqlite3.Connection, config: Config
) -> None:
    conn.execute("DROP TRIGGER captures_no_replace")
    conn.commit()
    checks = _by_name(run_db_checks(config))
    assert not checks["triggers present"].ok
    assert "captures_no_replace" in checks["triggers present"].detail


def test_a_corrupt_database_is_a_failed_check_not_a_traceback(
    config: Config, data_dir: Path
) -> None:
    data_dir.mkdir()
    config.paths.database.write_bytes(b"this is not a sqlite database" * 100)
    checks = run_db_checks(config)
    assert not all(c.ok for c in checks)


def test_empty_database_checks_say_there_was_nothing_to_verify(
    conn: sqlite3.Connection, config: Config
) -> None:
    checks = _by_name(run_db_checks(config))
    assert "nothing to verify" in checks["capture bodies exist and hash"].detail
    assert "no rows yet" in checks["rows.row_sha256 well-formed"].detail


def test_cli_selftest_db_passes_on_a_fresh_database(
    config: Config, write_config: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    database.migrate(config)
    monkeypatch.setenv("EFP_CONFIG", str(write_config()))
    result = runner.invoke(app, ["selftest", "--db"])
    assert result.exit_code == 0, result.output
    for name in ALL_CHECKS:
        assert name in result.output
    assert "13 check(s) run, 0 failed" in result.output


def test_cli_selftest_db_exits_1_when_the_database_is_missing(
    write_config: Callable[..., Path], data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EFP_CONFIG", str(write_config()))
    result = runner.invoke(app, ["selftest", "--db"])
    assert result.exit_code == 1
    assert "FAILED" in result.output and "efp db migrate" in result.output
    assert not data_dir.exists()


def test_cli_db_migrate_reports_up_to_date(
    config: Config, write_config: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    database.migrate(config)
    monkeypatch.setenv("EFP_CONFIG", str(write_config()))
    result = runner.invoke(app, ["db", "migrate"])
    assert result.exit_code == 0 and "up to date" in result.output


def test_cli_db_migrate_refuses_a_database_inside_the_repo(
    write_config: Callable[..., Path], data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.conftest import REPO

    cfg = write_config({f'database = "{data_dir}/pipeline.db"': f'database = "{REPO}/x.db"'})
    monkeypatch.setenv("EFP_CONFIG", str(cfg))
    result = runner.invoke(app, ["db", "migrate"])
    assert result.exit_code == 1 and "inside the repository" in result.output
    assert not (REPO / "x.db").exists()


def test_zero_hash_constant_is_a_valid_digest() -> None:
    assert len(ZERO) == 64


def test_a_dropped_workbook_trigger_is_caught(conn: sqlite3.Connection, config: Config) -> None:
    conn.execute("DROP TRIGGER source_workbook_no_delete")
    conn.commit()
    check = _by_name(run_db_checks(config))["triggers present"]
    assert not check.ok and "source_workbook_no_delete" in check.detail


def test_the_probe_works_on_a_database_that_already_has_a_workbook_record(
    conn: sqlite3.Connection, config: Config
) -> None:
    conn.execute(
        "INSERT INTO source_workbook (workbook_id, sha256, filename, sheet_name, header_row, "
        "data_rows, year_from, year_to, ingested_at) VALUES (1, ?, 'w.xlsx', 'Cases', 4, 20, 2000, 2025, 't')",
        (ZERO,),
    )
    conn.commit()
    check = _by_name(run_db_checks(config))["append-only triggers fire"]
    assert check.ok, check.detail
    assert conn.execute("SELECT count(*) FROM source_workbook").fetchone() == (1,)

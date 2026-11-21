from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from efp import audit
from efp import db as database
from efp.cli import app
from efp.config import Config
from tests.conftest import SENTINELS

runner = CliRunner()
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _rows(config: Config) -> list[sqlite3.Row]:
    conn = sqlite3.connect(config.paths.database)
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute("SELECT * FROM audit_log ORDER BY audit_id"))
    finally:
        conn.close()


def test_start_then_finish_completes_the_row(config: Config) -> None:
    database.migrate(config)
    audit_id = audit.start(config, ["run", "court", "--limit", "5"], NOW, config_sha256="c" * 64)
    assert audit_id is not None
    row = _rows(config)[0]
    assert (row["command"], row["finished_at"], row["exit_code"]) == ("run court", None, None)
    assert json.loads(row["args_json"]) == ["run", "court", "--limit", "5"]
    assert row["config_sha256"] == "c" * 64
    assert len(row["git_sha"]) == 40  # the full SHA, not the short one
    assert audit.finish(config, audit_id, 2)
    row = _rows(config)[0]
    assert row["exit_code"] == 2 and row["finished_at"]
    assert not audit.finish(config, audit_id, 3), "a second completion is refused by the trigger"
    assert _rows(config)[0]["exit_code"] == 2


def test_no_database_means_no_row_and_nothing_created(config: Config, data_dir: Path) -> None:
    assert audit.start(config, ["status"], NOW) is None
    assert not audit.finish(config, 5, 0)
    assert not data_dir.exists()


def test_missing_table_is_a_silent_no_op(config: Config, data_dir: Path) -> None:
    data_dir.mkdir()
    sqlite3.connect(config.paths.database).execute("CREATE TABLE unrelated (x)").connection.close()
    assert audit.start(config, ["status"], NOW) is None


def test_finish_without_start_does_nothing(config: Config) -> None:
    database.migrate(config)
    assert not audit.finish(config, None, 0)
    assert _rows(config) == []


def test_arguments_are_redacted_before_storage(config: Config) -> None:
    database.migrate(config)
    secret = SENTINELS["COURTLISTENER_TOKEN"]
    argv = [
        "export",
        "--api-key",
        "plain-looking-value",
        f"FEC_API_KEY={SENTINELS['FEC_API_KEY']}",
        secret,
        "sk-ant-api03-abcdefghijklmnop",  # pragma: allowlist secret
        "--limit",
        "5",
    ]
    audit.start(config, argv, NOW, secret_values=list(SENTINELS.values()))
    row = _rows(config)[0]
    stored = row["args_json"] + row["command"]
    for value in (
        *SENTINELS.values(),
        "plain-looking-value",
        "sk-ant-api03-abcdefghijklmnop",
    ):  # pragma: allowlist secret
        assert value not in stored
    assert json.loads(row["args_json"])[-2:] == ["--limit", "5"]


def test_every_cli_invocation_is_recorded(
    config: Config, write_config: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    database.migrate(config)
    monkeypatch.setenv("EFP_CONFIG", str(write_config()))
    for argv, code in ((["run", "court"], 2), (["status"], 2), (["run"], 2), (["selftest"], 0)):
        monkeypatch.setattr(sys, "argv", ["efp", *argv])
        assert runner.invoke(app, argv).exit_code == code
    rows = _rows(config)
    assert [(r["command"], r["exit_code"]) for r in rows] == [
        ("run court", 2),
        ("status", 2),
        ("run", 2),  # usage error: missing STAGE
        ("selftest", 0),
    ]
    assert all(
        r["finished_at"] and r["lockfile_sha256"] and len(r["config_sha256"]) == 64 for r in rows
    )


def test_the_invocation_that_creates_the_database_is_recorded(
    config: Config, write_config: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EFP_CONFIG", str(write_config()))
    monkeypatch.setattr(sys, "argv", ["efp", "db", "migrate"])
    result = runner.invoke(app, ["db", "migrate"])
    assert result.exit_code == 0, result.output
    assert "applied: 0001_core, 0002_supporting, 0003_triggers" in result.output
    rows = _rows(config)
    assert [(r["command"], r["exit_code"]) for r in rows] == [("db migrate", 0)]


def test_secrets_in_cli_arguments_never_reach_the_audit_log(
    config: Config, write_config: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    database.migrate(config)
    monkeypatch.setenv("EFP_CONFIG", str(write_config()))
    secret = SENTINELS["COURTLISTENER_TOKEN"]
    monkeypatch.setattr(sys, "argv", ["efp", "run", secret])
    runner.invoke(app, ["run", secret])
    row = _rows(config)[0]
    assert secret not in row["args_json"] + row["command"]

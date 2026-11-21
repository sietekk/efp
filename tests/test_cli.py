from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from efp.cli import app

runner = CliRunner()

# command -> build that will implement it
STUBS: dict[tuple[str, ...], str] = {
    ("ingest", "book.xlsx"): "03",
    ("run", "court"): "04",
    ("status",): "19",
    ("review", "court"): "04",
    ("import", "f.pdf", "--row", "1", "--source", "westlaw"): "14",
    ("identity", "explain", "1"): "12",
    ("audit", "1"): "19",
    ("export",): "19",
    ("manifest",): "20",
}


@pytest.fixture
def cfg_env(write_config: Callable[..., Path], monkeypatch: pytest.MonkeyPatch) -> Path:
    path = write_config()
    monkeypatch.setenv("EFP_CONFIG", str(path))
    return path


@pytest.mark.parametrize(
    "command",
    [
        [],
        ["ingest"],
        ["run"],
        ["status"],
        ["review"],
        ["import"],
        ["identity"],
        ["identity", "explain"],
        ["audit"],
        ["export"],
        ["manifest"],
        ["selftest"],
    ],
)
def test_every_command_has_working_help(command: list[str]) -> None:
    result = runner.invoke(app, [*command, "--help"])
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize(("args", "build"), STUBS.items())
def test_stub_exits_with_not_built(cfg_env: Path, args: tuple[str, ...], build: str) -> None:
    result = runner.invoke(app, list(args))
    assert result.exit_code == 2
    assert f"not built yet (build {build})" in result.output


def test_version_prints_version_and_sha() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.startswith("efp ")
    assert "(git " in result.output


def test_commands_work_without_any_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EFP_CONFIG", str(tmp_path / "missing.toml"))
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 2
    assert "not built yet" in result.output


def test_invocation_writes_an_audit_row_once_the_table_exists(
    cfg_env: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir.mkdir()
    db = data_dir / "pipeline.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE audit_log (audit_id INTEGER PRIMARY KEY, invoked_at TEXT, finished_at TEXT, "
        "command TEXT, args_json TEXT, git_sha TEXT, lockfile_sha256 TEXT, config_sha256 TEXT, "
        "exit_code INTEGER)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(sys, "argv", ["efp", "run", "court"])

    result = runner.invoke(app, ["run", "court"])
    assert result.exit_code == 2

    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT command, args_json, git_sha, config_sha256, exit_code FROM audit_log"
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    command, args_json, sha, config_sha, exit_code = rows[0]
    assert command == "run court"
    assert json.loads(args_json) == ["run", "court"]
    assert sha
    assert config_sha and len(config_sha) == 64
    assert exit_code == 2


def test_usage_error_is_audited_with_its_real_exit_code(
    cfg_env: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir.mkdir()
    db = data_dir / "pipeline.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE audit_log (audit_id INTEGER PRIMARY KEY, invoked_at TEXT, finished_at TEXT, "
        "command TEXT, args_json TEXT, git_sha TEXT, lockfile_sha256 TEXT, config_sha256 TEXT, "
        "exit_code INTEGER)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(sys, "argv", ["efp", "run"])
    result = runner.invoke(app, ["run"])  # missing the STAGE argument
    assert result.exit_code == 2
    conn = sqlite3.connect(db)
    codes = [r[0] for r in conn.execute("SELECT exit_code FROM audit_log")]
    conn.close()
    assert codes == [2]


def test_audit_is_a_no_op_without_a_database(cfg_env: Path, data_dir: Path) -> None:
    result = runner.invoke(app, ["run", "court"])
    assert result.exit_code == 2
    assert not data_dir.exists()

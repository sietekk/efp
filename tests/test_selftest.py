from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from efp.cli import app
from tests.conftest import SENTINELS

runner = CliRunner()


def test_selftest_with_no_data_dir_says_what_is_missing(
    write_config: Callable[..., Path],
    data_dir: Path,
    env_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file.unlink()
    monkeypatch.setenv("EFP_CONFIG", str(write_config()))
    result = runner.invoke(app, ["selftest"])
    assert result.exit_code == 0
    assert "data directory" in result.output
    assert "MISSING" in result.output
    assert "env file" in result.output
    assert "required item(s) missing" in result.output
    assert not data_dir.exists(), "selftest must never create the data directory"


def test_strict_selftest_fails_when_required_items_are_missing(
    write_config: Callable[..., Path], env_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file.unlink()
    monkeypatch.setenv("EFP_CONFIG", str(write_config()))
    assert runner.invoke(app, ["selftest", "--strict"]).exit_code == 1


def test_selftest_reports_secret_names_never_values(
    write_config: Callable[..., Path], data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir.mkdir()
    monkeypatch.setenv("EFP_CONFIG", str(write_config()))
    result = runner.invoke(app, ["selftest", "--strict"])
    assert result.exit_code == 0, result.output
    for name in SENTINELS:
        assert name in result.output
    for value in SENTINELS.values():
        assert value not in result.output


def test_selftest_flags_a_permissive_env_file(
    write_config: Callable[..., Path], env_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file.chmod(0o644)
    monkeypatch.setenv("EFP_CONFIG", str(write_config()))
    result = runner.invoke(app, ["selftest"])
    assert "0644" in result.output
    for value in SENTINELS.values():
        assert value not in result.output


def test_selftest_with_unreadable_config_reports_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EFP_CONFIG", str(tmp_path / "missing.toml"))
    result = runner.invoke(app, ["selftest"])
    assert result.exit_code == 0
    assert "config" in result.output
    assert "missing.toml" in result.output


def test_selftest_lists_extras(
    write_config: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EFP_CONFIG", str(write_config()))
    result = runner.invoke(app, ["selftest"])
    for extra in ("documents", "social", "extraction", "validation", "ocr-fallback"):
        assert f"extra {extra}" in result.output


def test_selftest_never_prints_a_bare_pasted_key(
    write_config: Callable[..., Path], env_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare = "sk-ant-api03-FAKEVALUE123"  # pragma: allowlist secret
    env_file.write_text(f"{bare}\nFEC_API_KEY=x\n", encoding="utf-8")
    env_file.chmod(0o600)
    monkeypatch.setenv("EFP_CONFIG", str(write_config()))
    result = runner.invoke(app, ["selftest"])
    assert bare not in result.output
    assert "not NAME=value" in result.output

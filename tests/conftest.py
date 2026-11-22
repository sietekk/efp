"""Shared fixtures. Everything here is synthetic; no test touches the network or a real home dir."""

from __future__ import annotations

import shutil
import socket
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from efp import db as database
from efp.config import Config, load_config

FIXTURES = Path(__file__).parent / "fixtures"
REPO = Path(__file__).resolve().parents[1]

# Injected into a temporary env file. The hygiene test asserts none of these ever surfaces.
SENTINELS: dict[str, str] = {
    "COURTLISTENER_TOKEN": "SENTINEL-courtlistener-5b1d9c",
    "PACER_USER": "SENTINEL-pacer-user-77aa31",
    "PACER_PASS": "SENTINEL-pacer-pass-c0ffee",
    "FEC_API_KEY": "SENTINEL-fec-1f2e3d",  # pragma: allowlist secret
    "ANTHROPIC_API_KEY": "SENTINEL-anthropic-9a8b7c",  # pragma: allowlist secret
    "GOOGLE_CSE_KEY": "SENTINEL-gcse-key-4d4d4d",
}


@pytest.fixture(autouse=True)
def _no_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """CI never reaches external services: socket connects fail unless the test is marked live."""
    if request.node.get_closest_marker("live"):
        return

    def _blocked(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("network access is blocked in the test suite (mark the test 'live')")

    monkeypatch.setattr(socket.socket, "connect", _blocked)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EFP_CONFIG", raising=False)


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    path = tmp_path / "secrets" / ".env"
    path.parent.mkdir()
    path.write_text("".join(f"{k}={v}\n" for k, v in SENTINELS.items()), encoding="utf-8")
    path.chmod(0o600)
    return path


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A path for the data directory. Not created: tests decide whether it exists."""
    return tmp_path / "data"


@pytest.fixture
def write_config(tmp_path: Path, data_dir: Path, env_file: Path) -> Callable[..., Path]:
    """Write the synthetic config with tmp paths; optional text replacements for failure cases."""

    def _write(replacements: dict[str, str] | None = None, append: str = "") -> Path:
        text = (FIXTURES / "config.valid.toml").read_text(encoding="utf-8")
        text = text.replace("__DATA__", str(data_dir)).replace("__ENV__", str(env_file))
        text = text.replace("__SAMPLE__", str(tmp_path / "sample_rows.json"))
        for old, new in (replacements or {}).items():
            assert old in text, f"fixture text not found: {old!r}"
            text = text.replace(old, new)
        path = tmp_path / "config.toml"
        path.write_text(text + append, encoding="utf-8")
        return path

    return _write


@pytest.fixture
def config(write_config: Callable[..., Path]) -> Config:
    return load_config(write_config())


@pytest.fixture
def full_config_path(write_config: Callable[..., Path]) -> Path:
    """Like ``config`` but with all eight stages, which the runner tests need."""
    old = 'stages = ["resolve_links", "court"]'
    new = (
        'stages = ["resolve_links", "court", "identity", "documents", "news", "social", '
        '"extract", "code"]'
    )
    return write_config({old: new})


@pytest.fixture
def full_config(full_config_path: Path) -> Config:
    return load_config(full_config_path)


@pytest.fixture
def conn(config: Config) -> Iterator[sqlite3.Connection]:
    """A migrated scratch database under tmp_path."""
    connection = database.connect(config)
    yield connection
    connection.close()


@pytest.fixture
def migrations_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private copy of migrations/ that db.py reads, so tests can edit or add files."""
    target = tmp_path / "migrations_copy"
    shutil.copytree(REPO / "migrations", target, ignore=shutil.ignore_patterns("*.md", ".*"))
    monkeypatch.setattr(database, "migrations_dir", lambda: target)
    return target


@pytest.fixture
def source_dir(tmp_path: Path) -> Path:
    """Where a synthetic workbook and its MANIFEST.txt live (outside the data directory)."""
    folder = tmp_path / "source"
    folder.mkdir()
    return folder


@pytest.fixture
def workbook(source_dir: Path) -> Path:
    """The 20-row synthetic workbook with a matching MANIFEST.txt."""
    from tests.fixtures.make_workbook import make_workbook, write_manifest

    path = source_dir / "heritage-synthetic.xlsx"
    make_workbook(path)
    write_manifest(path)
    return path


# Every captured output and failure text from the whole suite, for the hygiene test to scan.
COLLECTED: list[str] = []


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Any:
    outcome = yield
    report = outcome.get_result()
    if report.longrepr is not None:
        COLLECTED.append(f"[{item.nodeid} failure]\n{report.longreprtext}")
    COLLECTED.extend(f"[{item.nodeid} {name}]\n{content}" for name, content in report.sections)

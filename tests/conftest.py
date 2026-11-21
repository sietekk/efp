"""Shared fixtures. Everything here is synthetic; no test touches the network or a real home dir."""

from __future__ import annotations

import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"

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
        for old, new in (replacements or {}).items():
            assert old in text, f"fixture text not found: {old!r}"
            text = text.replace(old, new)
        path = tmp_path / "config.toml"
        path.write_text(text + append, encoding="utf-8")
        return path

    return _write


# Every captured output and failure text from the whole suite, for the hygiene test to scan.
COLLECTED: list[str] = []


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Any:
    outcome = yield
    report = outcome.get_result()
    if report.longrepr is not None:
        COLLECTED.append(f"[{item.nodeid} failure]\n{report.longreprtext}")
    COLLECTED.extend(f"[{item.nodeid} {name}]\n{content}" for name, content in report.sections)

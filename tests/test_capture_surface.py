"""The edges of the capture layer: who may touch the network, the CLI, selftest and modules."""

from __future__ import annotations

import ast
import hashlib
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from efp import db as database
from efp import instruments as ins
from efp import manifest as mf
from efp.builtin_modules import register_builtin
from efp.capture import Capture, LazyCapture
from efp.cli import app
from efp.config import Config, load_config
from efp.modules import Registry
from efp.runner import make_context
from tests.capturehelpers import make_capture
from tests.conftest import REPO
from tests.dbhelpers import insert_row
from tests.fakets import make_tsr
from tests.runnerhelpers import ALL_STAGES, ALL_STAGES_LINE

runner = CliRunner()

# Nothing but efp/capture.py may speak HTTP. (urllib.robotparser parses text and never fetches.)
FORBIDDEN_ROOTS = {
    "requests",
    "requests_cache",
    "urllib3",
    "httpx",
    "aiohttp",
    "ftplib",
    "smtplib",
    # packages that make their own HTTP calls and would bypass the capture layer
    "wayback",
    "cdx_toolkit",
    "savepagenow",
}
FORBIDDEN_MODULES = {"urllib.request", "http.client", "socket"}
ALLOWED_FILES = {"capture.py"}


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def test_only_the_capture_layer_imports_an_http_library() -> None:
    offenders: list[str] = []
    for path in sorted((REPO / "efp").rglob("*.py")):
        if path.name in ALLOWED_FILES and path.parent.name == "efp":
            continue
        for name in _imports(path):
            if name.split(".")[0] in FORBIDDEN_ROOTS or name in FORBIDDEN_MODULES:
                offenders.append(f"{path.relative_to(REPO)}: {name}")
    assert offenders == []


def test_the_scan_would_catch_a_stray_import(tmp_path: Path) -> None:
    sample = tmp_path / "bad.py"
    sample.write_text("import requests\nfrom urllib.request import urlopen\n")
    found = _imports(sample)
    assert "requests" in found and "urllib.request" in found


# --- instruments ------------------------------------------------------------------------------


def test_an_instrument_hash_is_the_file_hash_and_changes_with_the_file(tmp_path: Path) -> None:
    (tmp_path / "allowlist.json").write_text('{"sites": []}')
    first = ins.pin("allowlist", base=tmp_path)
    assert first.sha256 == hashlib.sha256(b'{"sites": []}').hexdigest()
    (tmp_path / "allowlist.json").write_text('{"sites": ["x"]}')
    assert ins.pin("allowlist", base=tmp_path).sha256 != first.sha256


def test_a_directory_instrument_covers_every_file_by_name_and_content(tmp_path: Path) -> None:
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "a.txt").write_text("one")
    (prompts / "b.txt").write_text("two")
    before = ins.pin("prompts", base=tmp_path)
    assert before.files == ("prompts/a.txt", "prompts/b.txt")
    (prompts / "b.txt").write_text("changed")
    assert ins.pin("prompts", base=tmp_path).sha256 != before.sha256
    (prompts / "b.txt").write_text("two")
    (prompts / "c.txt").write_text("new")
    assert ins.pin("prompts", base=tmp_path).sha256 != before.sha256


def test_a_missing_instrument_is_named_not_guessed(tmp_path: Path) -> None:
    with pytest.raises(ins.InstrumentMissing, match="allowlist"):
        ins.pin("allowlist", base=tmp_path)
    (tmp_path / "gold.csv").write_text("x")
    assert ins.pinned(["allowlist", "gold_set"], base=tmp_path) == {}  # nothing registered exists
    ins.register("gold_set_test", "gold.csv")
    assert set(ins.pinned(["gold_set_test"], base=tmp_path)) == {"gold_set_test"}
    with pytest.raises(ValueError, match="already registered"):
        ins.register("gold_set_test", "elsewhere.csv")


# --- modules and the context ------------------------------------------------------------------


def test_the_archive_chain_reports_availability_and_reasons(
    full_config: Config, env_file: Path
) -> None:
    registry = Registry()
    register_builtin(registry)
    register_builtin(registry)  # idempotent
    ctx = make_context(full_config, None)
    spn_module = registry.get("archive.spn")
    assert spn_module is not None
    off = spn_module.available(ctx)
    assert not off.ok and "IA_ACCESS" in off.reason
    perma_module = registry.get("archive.perma")
    assert perma_module is not None and not perma_module.available(ctx).ok
    with env_file.open("a") as handle:
        handle.write("IA_ACCESS=a\nIA_SECRET=b\nPERMA_API_KEY=c\n")
    assert spn_module.available(ctx).ok and perma_module.available(ctx).ok
    auto = registry.get("archive.autoarchiver")
    assert auto is not None and not auto.available(ctx).ok


def test_efp_modules_lists_the_archive_chain(
    full_config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EFP_CONFIG", str(full_config_path))
    result = runner.invoke(app, ["modules", "--stage", "archive"])
    assert result.exit_code == 0
    assert (
        "archive.spn" in result.output and "IA_ACCESS" in result.output
    )  # the test config's chain


def test_the_shipped_config_lists_all_three_archivers_with_reasons() -> None:
    from efp import modules as chain

    registry = Registry()
    register_builtin(registry)
    shipped = load_config(REPO / "config.example.toml")
    rows = {
        r.name: r for r in chain.describe(shipped, make_context(shipped, None), registry, "archive")
    }
    assert set(rows) == {"archive.spn", "archive.perma", "archive.autoarchiver"}
    assert all(r.registered and r.reason for r in rows.values() if not r.ok)


def test_the_lazy_capture_builds_nothing_until_used(
    config: Config, conn: sqlite3.Connection
) -> None:
    lazy = LazyCapture(config, conn)
    assert not config.paths.cache.exists() and not config.paths.warc.exists()
    assert callable(lazy.fetch)  # first use builds the real client
    assert config.paths.cache.exists()
    lazy.close()
    ctx = make_context(config, None)
    assert ctx.capture is None


# --- selftest ---------------------------------------------------------------------------------


def test_selftest_reports_the_capture_layer_budgets_and_which_keys_exist(
    write_config: Callable[..., Path], env_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EFP_CONFIG", str(write_config()))
    result = runner.invoke(app, ["selftest"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "capture layer" in out and "libraries importable" in out
    assert "budgets" in out and "example.test 1s" in out
    assert "save page now" in out and "IA_ACCESS/IA_SECRET absent" in out
    assert "perma.cc" in out and "PERMA_API_KEY absent" in out
    assert "tsa trust anchors" in out
    with env_file.open("a") as handle:
        handle.write("IA_ACCESS=ACCESSVALUE1\nIA_SECRET=SECRETVALUE2\nPERMA_API_KEY=PERMAVALUE3\n")
    again = runner.invoke(app, ["selftest"]).output
    assert "keys present" in again and "key present (cited URLs only)" in again
    assert "ACCESSVALUE1" not in again and "SECRETVALUE2" not in again  # names only, never values


# --- the CLI: manifest, bag and the Save Page Now commands -------------------------------------


class _LatestManifestTsa:
    """Stamps whichever manifest file is newest (the CLI builds it just before asking)."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def post(self, url: str, request_der: bytes) -> bytes:
        newest = max(self.config.paths.manifests.glob("*.txt"))
        return make_tsr(hashlib.sha256(newest.read_bytes()).digest())


@pytest.fixture
def populated(
    write_config: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> tuple[Config, Path]:
    path = write_config(
        {
            ALL_STAGES_LINE: ALL_STAGES,
            'tsa_urls = ["http://tsa.example.test"]': (
                'tsa_urls = ["http://tsa-a.example.test", "http://tsa-b.example.test"]'
            ),
        }
    )
    config = load_config(path)
    conn = database.connect(config)
    insert_row(conn, 1)
    conn.commit()
    client: Capture = make_capture(config, conn)
    for i in range(3):
        client.store_bytes(
            f"body {i}".encode(),
            row_id=1,
            stage="resolve_links",
            url_requested=f"https://e.test/{i}",
            tool="t",
            http_status=200,
        )
    client.close()
    conn.close()
    monkeypatch.setenv("EFP_CONFIG", str(path))
    return config, path


def test_manifest_timestamp_verify_and_bag_through_the_cli(
    populated: tuple[Config, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _ = populated
    built = runner.invoke(app, ["manifest"])
    assert built.exit_code == 0, built.output
    assert "manifest " in built.output and "3 bodies" in built.output
    assert "nothing new" in runner.invoke(app, ["manifest"]).output
    monkeypatch.setattr(mf, "CaptureTsa", lambda _client: _LatestManifestTsa(config))
    stamped = runner.invoke(app, ["manifest", "--timestamp"])
    assert stamped.exit_code == 0, stamped.output
    assert stamped.output.count("token ") == 2
    checked = runner.invoke(app, ["manifest", "--verify"])
    assert checked.exit_code == 0, checked.output
    assert checked.output.count("[ok") == 2
    bag = runner.invoke(app, ["manifest", "--bag"])
    assert bag.exit_code == 0 and "bag is valid" in bag.output


def test_verify_fails_with_exit_1_when_the_manifest_changed(
    populated: tuple[Config, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _ = populated
    runner.invoke(app, ["manifest"])
    monkeypatch.setattr(mf, "CaptureTsa", lambda _client: _LatestManifestTsa(config))
    runner.invoke(app, ["manifest", "--timestamp"])
    target = max(config.paths.manifests.glob("*.txt"))
    target.chmod(0o644)
    target.write_text(target.read_text() + "tampered\n")
    result = runner.invoke(app, ["manifest", "--verify"])
    assert result.exit_code == 1 and "FAILED" in result.output


def test_spn_commands_without_keys(populated: tuple[Config, Path]) -> None:
    status = runner.invoke(app, ["spn", "status"])
    assert status.exit_code == 0 and "pending" in status.output
    drained = runner.invoke(app, ["spn", "drain"])
    assert drained.exit_code == 0 and "IA_ACCESS" in drained.output

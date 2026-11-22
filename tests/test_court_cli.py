from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import responses
import stamina
from typer.testing import CliRunner

from efp.cli import app
from efp.config import Config
from efp.court import bulk
from tests.courthelpers import HEADERS, court_config, s3_listing, write_bulk_files

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_retries() -> Iterator[None]:
    stamina.set_testing(True, attempts=1, cap=False)
    yield
    stamina.set_testing(False)


@pytest.fixture
def rsps() -> Iterator[responses.RequestsMock]:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield mock


@pytest.fixture
def config(write_config: Callable[..., Path], monkeypatch: pytest.MonkeyPatch) -> Config:
    cfg = court_config(
        write_config,
        **{'"example.test" = 1.0': '"example.test" = 1.0\n"bulk.example.test" = 100.0'},
    )
    # write_config puts config.toml beside the data directory
    monkeypatch.setenv("EFP_CONFIG", str(Path(cfg.paths.data_dir).parent / "config.toml"))
    return cfg


def _listing(rsps: responses.RequestsMock, sizes: dict[str, int]) -> None:
    entries = [(f"bulk-data/{kind}-2026-09-30.csv", size) for kind, size in sizes.items()]
    rsps.get(
        "https://bulk.example.test/",
        body=s3_listing(entries),
        match=[responses.matchers.query_param_matcher({"list-type": "2"}, strict_match=False)],
    )


def test_fetch_without_yes_lists_sizes_and_downloads_nothing(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    _listing(
        rsps,
        {"dockets": 4_000_000_000, "courts": 1_000, "parties": 2_000_000, "party-types": 3_000_000},
    )
    result = runner.invoke(app, ["court", "bulk", "fetch"])
    assert result.exit_code == 0, result.output
    assert "dockets" in result.output and "4.00 GB" in result.output
    assert "total 4.0 GB (limit court.bulk_max_gb = 1.0)" in result.output
    assert "nothing downloaded" in result.output
    assert not (config.paths.bulk / "courtlistener").exists()


def test_fetch_refuses_over_the_size_limit_even_with_yes(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    _listing(rsps, {"dockets": 4_000_000_000, "courts": 1_000, "parties": 1, "party-types": 1})
    result = runner.invoke(app, ["court", "bulk", "fetch", "--yes"])
    assert result.exit_code == 1 and "court.bulk_max_gb" in result.output
    assert not [c for c in rsps.calls if c.request.url.endswith(".csv")]  # nothing was downloaded


def test_fetch_with_yes_downloads_each_file_and_load_builds_the_index(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock, tmp_path: Path
) -> None:
    staged = write_bulk_files(tmp_path / "staged", compress=False)
    _listing(rsps, {kind: path.stat().st_size for kind, path in staged.items()})
    for kind, path in staged.items():
        rsps.get(
            f"https://bulk.example.test/bulk-data/{kind}-2026-09-30.csv", body=path.read_bytes()
        )
    fetched = runner.invoke(app, ["court", "bulk", "fetch", "--yes"])
    assert fetched.exit_code == 0, fetched.output
    assert fetched.output.count("downloaded") == 4 and "efp court bulk load" in fetched.output
    loaded = runner.invoke(app, ["court", "bulk", "load"])
    assert loaded.exit_code == 0, loaded.output
    assert "index loaded (dataset 2026-09-30)" in loaded.output
    # the manifest captures are recorded in the index, so a result can name its source file
    info = bulk.index_info(config)
    assert info is not None
    assert set(info.manifest_captures) == {"dockets", "courts", "parties", "party-types"}
    status = runner.invoke(app, ["court", "bulk", "status"])
    assert "dataset 2026-09-30" in status.output and f"{info.dockets:,} dockets" in status.output


def test_status_says_so_when_there_is_no_index(config: Config, conn: sqlite3.Connection) -> None:
    result = runner.invoke(app, ["court", "bulk", "status"])
    assert result.exit_code == 0 and "not loaded" in result.output


def test_load_before_download_says_what_is_missing(
    config: Config, conn: sqlite3.Connection
) -> None:
    result = runner.invoke(app, ["court", "bulk", "load"])
    assert result.exit_code == 1 and "no single dataset date has all of" in result.output


def test_load_reports_a_layout_mismatch_instead_of_guessing(
    config: Config, conn: sqlite3.Connection
) -> None:
    headers = {k: list(v) for k, v in HEADERS.items()}
    headers["parties"] = ["id", "label"]  # no name column
    write_bulk_files(config.paths.bulk / "courtlistener", compress=False, headers=headers)
    result = runner.invoke(app, ["court", "bulk", "load"])
    assert result.exit_code == 1 and "layout error" in result.output
    assert "'label'" in result.output and not bulk.index_path(config).exists()


def test_selftest_reports_the_bulk_index_and_the_court_sources(
    config: Config, conn: sqlite3.Connection, env_file: Path
) -> None:
    before = runner.invoke(app, ["selftest"]).output
    assert "courtlistener bulk index" in before and "not loaded" in before
    assert "pin reports" in before and "court.pin_report_urls is empty" in before
    assert "recap fetch" in before and "never fetches from PACER" in before
    folder = config.paths.bulk / "courtlistener"
    write_bulk_files(folder)
    bulk.load_index(
        config,
        {k: next(folder.glob(f"{k}-*")) for k in bulk.KINDS},
        dataset_date="2026-09-30",
        manifest_captures={},
    )
    after = runner.invoke(app, ["selftest"]).output
    assert "dataset 2026-09-30" in after and "dockets" in after and "courtlistener api" in after
    assert "SENTINEL" not in after  # names only, never values

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from efp.cli import app
from efp.config import Config
from efp.ingest.loader import sha256_file
from tests.fixtures.make_workbook import ROWS_20, make_workbook, write_manifest

runner = CliRunner()


@pytest.fixture
def cfg_env(write_config: Callable[..., Path], monkeypatch: pytest.MonkeyPatch) -> Path:
    path = write_config()
    monkeypatch.setenv("EFP_CONFIG", str(path))
    return path


def _ingest(workbook: Path, monkeypatch: pytest.MonkeyPatch, *extra: str):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(sys, "argv", ["efp", "ingest", str(workbook), *extra])
    return runner.invoke(app, ["ingest", str(workbook), *extra])


def _audit(config: Config) -> list[sqlite3.Row]:
    conn = sqlite3.connect(config.paths.database)
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute("SELECT * FROM audit_log ORDER BY audit_id"))
    finally:
        conn.close()


def test_ingest_reports_counts_and_never_names(
    cfg_env: Path, workbook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _ingest(workbook, monkeypatch)
    assert result.exit_code == 0, result.output
    for expected in (
        "INGESTED",
        "rows loaded     : 20",
        "in the window   : 19",
        "non-empty Source: 18",
        "frozen sample: 19 of 50",
        "Criminal Conviction",
    ):
        assert expected in result.output
    assert sha256_file(workbook) in result.output
    for private in ("Alex Example", "example.test", "Synthetic note"):
        assert private not in result.output


def test_a_second_ingest_is_a_no_op(
    cfg_env: Path, workbook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _ingest(workbook, monkeypatch).exit_code == 0
    again = _ingest(workbook, monkeypatch)
    assert again.exit_code == 0 and "NO-OP" in again.output


def test_a_modified_workbook_is_refused_with_exit_1(
    cfg_env: Path, workbook: Path, source_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _ingest(workbook, monkeypatch).exit_code == 0
    changed = source_dir / "changed.xlsx"
    rows = [list(r) for r in ROWS_20]
    rows[0][2] = "Edited Person"
    make_workbook(changed, rows=[tuple(r) for r in rows])
    write_manifest(changed)
    result = _ingest(changed, monkeypatch)
    assert result.exit_code == 1
    assert "ingest failed" in result.output and "rows is immutable" in result.output


def test_a_manifest_mismatch_prints_both_hashes_and_creates_nothing(
    cfg_env: Path, workbook: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (workbook.parent / "MANIFEST.txt").write_text(
        f"{'0' * 64}  {workbook.name}\n", encoding="utf-8"
    )
    result = _ingest(workbook, monkeypatch)
    assert result.exit_code == 1
    assert "0" * 64 in result.output and sha256_file(workbook) in result.output
    assert not data_dir.exists()


def test_check_mode_writes_nothing(
    cfg_env: Path, workbook: Path, data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _ingest(workbook, monkeypatch, "--check")
    assert result.exit_code == 0 and "CHECK ONLY" in result.output
    assert not data_dir.exists() and not (tmp_path / "sample_rows.json").exists()


def test_a_workbook_problem_is_listed_and_exits_1(
    cfg_env: Path, source_dir: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = source_dir / "bad.xlsx"
    make_workbook(path, drop_header="Source")
    write_manifest(path)
    result = _ingest(path, monkeypatch)
    assert result.exit_code == 1 and "missing expected column(s): Source" in result.output
    assert not data_dir.exists()


def test_the_audit_row_carries_the_workbook_facts_and_population(
    cfg_env: Path, workbook: Path, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _ingest(workbook, monkeypatch).exit_code == 0
    row = _audit(config)[0]
    args = json.loads(row["args_json"])
    assert row["command"].split()[0] == "ingest" and row["exit_code"] == 0 and row["finished_at"]
    assert args["argv"] == ["ingest", str(workbook)]
    assert args["population"] == "all"
    assert args["workbook_sha256"] == sha256_file(workbook)
    assert (args["sheet"], args["header_row"]) == ("Cases", 4)


def test_every_run_records_the_population_rule_in_force(
    write_config: Callable[..., Path], workbook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config({'population = "all"': 'population = "convictions"'})
    monkeypatch.setenv("EFP_CONFIG", str(path))
    from efp.config import load_config

    config = load_config(path)
    assert _ingest(workbook, monkeypatch).exit_code == 0
    monkeypatch.setattr(sys, "argv", ["efp", "status"])
    assert runner.invoke(app, ["status"]).exit_code == 0
    populations = [json.loads(r["args_json"])["population"] for r in _audit(config)]
    assert populations == ["convictions", "convictions"]


def test_a_failed_ingest_is_still_audited_once_a_database_exists(
    cfg_env: Path, workbook: Path, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _ingest(workbook, monkeypatch).exit_code == 0
    (workbook.parent / "MANIFEST.txt").write_text(
        f"{'0' * 64}  {workbook.name}\n", encoding="utf-8"
    )
    assert _ingest(workbook, monkeypatch).exit_code == 1
    first, second = _audit(config)
    assert second["exit_code"] == 1
    assert "workbook_sha256" not in json.loads(second["args_json"])  # it never got that far
    assert first["exit_code"] == 0


# ---------------------------------------------------------------- status


def test_status_without_a_database_says_so(cfg_env: Path, data_dir: Path) -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 1 and "efp ingest" in result.output
    assert not data_dir.exists()


def test_status_shows_every_stage_pending(
    cfg_env: Path, workbook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ingest(workbook, monkeypatch)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "population: all (19 rows)" in result.output
    lines = {line.split()[0]: line.split()[1:] for line in result.output.splitlines()[2:]}
    assert set(lines) == {"resolve_links", "court"}
    for counts in lines.values():
        assert counts == [
            "19",
            "0",
            "0",
            "0",
            "0",
            "0",
        ]  # pending, running, done, failed, needs_review, skipped


def test_status_filters(cfg_env: Path, workbook: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ingest(workbook, monkeypatch)
    only_court = runner.invoke(app, ["status", "--stage", "court"])
    assert "court" in only_court.output and "resolve_links" not in only_court.output
    ohio = runner.invoke(app, ["status", "--state", "oh"])
    assert "population: all (2 rows)" in ohio.output  # 2004 and 2019; the 1998 row is out of scope


def test_status_applies_the_population_rule_at_query_time(
    write_config: Callable[..., Path], workbook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config({'population = "all"': 'population = "convictions"'})
    monkeypatch.setenv("EFP_CONFIG", str(path))
    _ingest(workbook, monkeypatch)
    result = runner.invoke(app, ["status"])
    expected = sum(
        1
        for r in ROWS_20
        if 2000 <= int(r[0]) <= 2025 and str(r[3]).startswith("Criminal Conviction")
    )
    assert f"population: convictions ({expected} rows)" in result.output


# ---------------------------------------------------------------- export --dry-run


def test_export_dry_run_passes_and_reports_counts(
    cfg_env: Path, workbook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ingest(workbook, monkeypatch)
    result = runner.invoke(app, ["export", "--dry-run", "--workbook", str(workbook)])
    assert result.exit_code == 0, result.output
    assert "F1 round trip: PASS" in result.output
    assert "rows verified against rows.row_sha256 : 20" in result.output
    assert "rows in the year window (in_scope)    : 19" in result.output
    assert (
        "Source non-empty                      : 18 of all rows, 17 in the window" in result.output
    )
    assert (
        "Source containing an http(s) URL      : 18 of all rows, 17 in the window" in result.output
    )
    assert "data validations in the openpyxl copy : 1 standard, 0 extension (x14)" in result.output
    assert "WARNING" not in result.output


def test_export_dry_run_defaults_to_paths_source_workbook(
    cfg_env: Path, workbook: Path, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ingest(workbook, monkeypatch)
    config.paths.source_workbook.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(workbook, config.paths.source_workbook)
    assert runner.invoke(app, ["export", "--dry-run"]).exit_code == 0


def test_export_dry_run_fails_on_a_changed_workbook(
    cfg_env: Path, workbook: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ingest(workbook, monkeypatch)
    other = tmp_path / "other.xlsx"
    shutil.copy(workbook, other)
    with other.open("ab") as handle:
        handle.write(b"x")
    result = runner.invoke(app, ["export", "--dry-run", "--workbook", str(other)])
    assert result.exit_code == 1 and "F1 round trip failed" in result.output


def test_export_dry_run_needs_a_database(cfg_env: Path, workbook: Path) -> None:
    result = runner.invoke(app, ["export", "--dry-run", "--workbook", str(workbook)])
    assert result.exit_code == 1 and "efp ingest" in result.output


def test_export_without_dry_run_is_still_build_19(cfg_env: Path) -> None:
    result = runner.invoke(app, ["export"])
    assert result.exit_code == 2 and "not built yet (build 19)" in result.output


# ---------------------------------------------------------------- selftest --db


def test_selftest_db_recomputes_hashes_when_the_workbook_is_available(
    cfg_env: Path, workbook: Path, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ingest(workbook, monkeypatch)
    skipped = runner.invoke(app, ["selftest", "--db"])
    assert skipped.exit_code == 0, skipped.output
    assert (
        "rows.row_sha256 matches the source workbook" in skipped.output
        and "skipped" in skipped.output
    )
    config.paths.source_workbook.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(workbook, config.paths.source_workbook)
    verified = runner.invoke(app, ["selftest", "--db"])
    assert (
        verified.exit_code == 0
        and "all 20 rows recomputed from the workbook and match" in verified.output
    )


def test_selftest_db_fails_when_the_workbook_at_the_configured_path_is_a_different_file(
    cfg_env: Path, workbook: Path, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ingest(workbook, monkeypatch)
    config.paths.source_workbook.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(workbook, config.paths.source_workbook)
    with config.paths.source_workbook.open("ab") as handle:
        handle.write(b"x")
    result = runner.invoke(app, ["selftest", "--db"])
    assert result.exit_code == 1 and "not the workbook that was ingested" in result.output


@pytest.mark.filterwarnings("ignore:Data Validation extension:UserWarning")
def test_export_dry_run_warns_when_openpyxl_loses_the_dropdown(
    cfg_env: Path, source_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = source_dir / "x14.xlsx"
    make_workbook(path, dropdown="x14")
    write_manifest(path)
    assert _ingest(path, monkeypatch).exit_code == 0
    result = runner.invoke(app, ["export", "--dry-run", "--workbook", str(path)])
    assert result.exit_code == 0, result.output  # F1 holds
    assert "F1 round trip: PASS" in result.output
    assert "0 standard, 1 extension (x14)" in result.output
    assert "WARNING: openpyxl dropped a data validation" in result.output

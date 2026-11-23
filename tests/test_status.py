from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from efp import db as database
from efp import status as view
from efp.cli import app
from efp.config import Config
from tests.runnerhelpers import FakeComponent, run, seed, set_stage

runner = CliRunner()


@pytest.fixture
def db(full_config: Config) -> Iterator[sqlite3.Connection]:
    connection = database.connect(full_config)
    seed(connection)
    yield connection
    connection.close()


def test_the_matrix_sums_to_the_population_for_every_stage(full_config: Config, db) -> None:  # type: ignore[no-untyped-def]
    run(
        db, full_config, "resolve_links", FakeComponent("resolve_links", fail={2}, needs_review={3})
    )
    set_stage(db, 4, "court", "skipped", gate_reason="x")
    report = view.build_report(db, full_config)
    assert report.population == 6
    for stage, counts in report.matrix.items():
        assert sum(counts.values()) == 6, stage
    assert report.matrix["resolve_links"]["done"] == 4
    assert report.matrix["resolve_links"]["failed"] == 1
    assert report.matrix["resolve_links"]["needs_review"] == 1


def test_gate_reasons_attempts_and_errors_are_summarised(full_config: Config, db) -> None:  # type: ignore[no-untyped-def]
    set_stage(db, 1, "social", "skipped", gate_reason="no_handle")
    set_stage(db, 2, "social", "skipped", gate_reason="no_handle")
    set_stage(db, 3, "social", "skipped", gate_reason="social_disabled")
    run(db, full_config, "resolve_links", FakeComponent("resolve_links", fail={1, 2}))
    report = view.build_report(db, full_config)
    assert report.gate_reasons["social"] == {"no_handle": 2, "social_disabled": 1}
    assert report.attempts["resolve_links"] == {
        "rows_attempted": 6,
        "max_attempts": 1,
        "failed_at_limit": 0,
    }
    assert report.errors["resolve_links"] == [("boom", 2)]
    text = view.render_md(report)
    assert "| social | no_handle | 2 |" in text and "2 x boom" in text


def test_the_frozen_sample_gets_its_own_matrix(full_config: Config, db) -> None:  # type: ignore[no-untyped-def]
    Path(full_config.scope.sample_file).write_text(
        json.dumps({"rows": [{"row_id": 1, "row_sha256": "0"}, {"row_id": 2, "row_sha256": "0"}]})
    )
    run(db, full_config, "resolve_links", FakeComponent("resolve_links"), limit=1)
    report = view.build_report(db, full_config)
    assert report.sample_size == 2
    assert report.sample_matrix["resolve_links"]["done"] == 1  # row 1 is first in the order
    assert report.sample_matrix["resolve_links"]["pending"] == 1
    assert "### Frozen sample (2 rows in scope)" in view.render_md(report)


def test_filters_and_json(full_config: Config, db) -> None:  # type: ignore[no-untyped-def]
    only = view.build_report(db, full_config, stage="court", state="oh")
    assert list(only.matrix) == ["court"] and only.population == 3
    document = json.loads(view.render_json(view.build_report(db, full_config)))
    assert document["population"] == 6 and "resolve_links" in document["matrix"]


def test_the_empty_matrix_for_a_fresh_ingest(
    full_config_path: Path, workbook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EFP_CONFIG", str(full_config_path))
    assert runner.invoke(app, ["ingest", str(workbook)]).exit_code == 0
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "None: no row has stopped at a gate." in result.output
    assert "No stage has been attempted yet." in result.output
    document = json.loads(runner.invoke(app, ["status", "--format", "json"]).output)
    in_scope = 19  # the 20-row synthetic workbook has one row outside 2000 to 2025
    assert document["population"] == in_scope
    assert all(c["pending"] == in_scope for c in document["matrix"].values())


def test_run_dry_run_after_ingest_and_every_other_stage_names_its_predecessor(
    full_config_path: Path, workbook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EFP_CONFIG", str(full_config_path))
    runner.invoke(app, ["ingest", str(workbook)])
    first = runner.invoke(app, ["run", "resolve_links", "--dry-run"])
    assert first.exit_code == 0 and "eligible of" in first.output
    assert "gate: pass" in first.output and " 0 eligible" not in first.output
    court = runner.invoke(app, ["run", "court", "--dry-run"])
    assert "0 eligible" in court.output and "predecessor resolve_links is not done" in court.output
    extract = runner.invoke(app, ["run", "extract", "--dry-run"])
    assert "predecessor documents" in extract.output and "predecessor social" in extract.output
    assert runner.invoke(app, ["run", "nope", "--dry-run"]).exit_code == 2


def test_run_without_a_component_says_so(
    full_config_path: Path, workbook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EFP_CONFIG", str(full_config_path))
    runner.invoke(app, ["ingest", str(workbook)])
    result = runner.invoke(app, ["run", "news"])  # news has no component yet
    assert result.exit_code == 0
    assert "no component registered yet; nothing was run" in result.output

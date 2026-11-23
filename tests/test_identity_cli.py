"""The identity commands, ``efp review identity``, ``efp status`` and ``efp selftest --identity``."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from efp import identity
from efp.cli import app
from efp.config import Config
from efp.identity import params
from efp.identity.checks import identity_checks, name_only_posteriors
from efp.modules import Registry
from efp.status import identity_summary
from efp.types import Context, Row
from tests.identity_helpers import seed_candidate, seed_reference, seed_row

runner = CliRunner()


@pytest.fixture
def cli(
    config: Config, conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Context:
    monkeypatch.setenv("EFP_CONFIG", str(tmp_path / "config.toml"))
    seed_reference(conn)
    for i in range(1, 5):
        surname = "Surname" + chr(96 + i)
        seed_row(conn, i, f"Jane Marie {surname}")
        seed_candidate(
            conn, "fec", f"F{i}", surname.upper(), "JANE", middle="MARIE" if i < 3 else "ADAMS"
        )
    registry = Registry()
    identity.register_modules(registry)
    ctx = Context(config=config, db=conn, log=logging.getLogger("t"))
    rows = [
        Row(
            row_id=i,
            year=2018,
            state="CT",
            source={"name": f"Jane Marie Surname{chr(96 + i)}", "state": "CT"},
        )
        for i in range(1, 5)
    ]
    for result in identity.process_batch(rows, ctx, registry):
        conn.execute(
            "UPDATE case_stage SET status = ? WHERE row_id = ? AND stage = 'identity'",
            (result.status, result.row_id),
        )
    conn.commit()
    return ctx


def test_explain_prints_the_decision(cli: Context) -> None:
    result = runner.invoke(app, ["identity", "explain", "1"])
    assert result.exit_code == 0, result.output
    assert "Population prior" in result.output and "posterior" in result.output
    assert (
        runner.invoke(app, ["identity", "explain", "999"]).output.strip() == "row 999: no such row"
    )


def test_labels_build_and_status(cli: Context) -> None:
    built = runner.invoke(app, ["identity", "labels", "build"])
    assert built.exit_code == 0, built.output
    assert "queue: 4 pairs" in built.output and "only 4 of 150 pairs exist so far" in built.output
    status = runner.invoke(app, ["identity", "labels", "status"])
    assert (
        "queued 4" in status.output
        and "waiting 4" in status.output
        and "needs 150" in status.output
    )


def test_refit_is_refused_below_the_required_number_of_labels(cli: Context) -> None:
    result = runner.invoke(app, ["identity", "refit"])
    assert result.exit_code == 1
    assert "0 of 150 labeled pairs" in result.output


def test_rescore_with_the_published_priors_changes_nothing(cli: Context) -> None:
    result = runner.invoke(app, ["identity", "rescore"])
    assert result.exit_code == 0, result.output
    assert "0 written, 4 unchanged" in result.output
    strict = runner.invoke(app, ["identity", "rescore", "--confirm-threshold", "0.995"])
    assert "batch sensitivity" in strict.output


def test_review_identity_walks_the_band_with_the_typed_answers(cli: Context) -> None:
    result = runner.invoke(app, ["review", "identity"], input="MC\nc\nsame middle\n" + "q\n")
    assert result.exit_code == 0, result.output
    assert "answered 1" in result.output and "confirmed" in result.output
    conn = cli.db
    assert conn is not None
    assert (
        conn.execute(
            "SELECT count(*) FROM decisions WHERE kind = 'review' AND outcome = 'confirmed'"
        ).fetchone()[0]
        == 1
    )


def test_audit_build_rate_and_label_modes(cli: Context, tmp_path: Path) -> None:
    conn = cli.db
    assert conn is not None
    for row_id in (1, 2):  # a later model decision per row: this tests the audit only
        conn.execute(
            "INSERT INTO decisions (row_id, kind, outcome, decided_by, decided_at, stage, batch) "
            "VALUES (?, 'identity', 'confirmed', 'x', 's', 'identity', 'main')",
            (row_id,),
        )
    conn.commit()
    built = runner.invoke(
        app, ["identity", "audit", "build", "--out", str(tmp_path / "f3_audit.md")]
    )
    assert built.exit_code == 0, built.output
    assert "drew 2 confirmed rows" in built.output
    rate = runner.invoke(app, ["identity", "audit", "rate"])
    assert "no verdicts yet" in rate.output and "stratum" in rate.output
    verdicts = runner.invoke(
        app, ["review", "identity", "--audit"], input="MC\nc\nfine\nw\ndifferent person\n"
    )
    assert verdicts.exit_code == 0, verdicts.output
    rate = runner.invoke(app, ["identity", "audit", "rate"])
    assert "0.500" in rate.output  # one wrong verdict of two judged (both rows are rare)


def test_status_carries_the_identity_table(cli: Context) -> None:
    conn = cli.db
    assert conn is not None
    summary = identity_summary(conn, cli.config)
    assert summary["outcomes"]["all"]["needs_review"] == 2 and summary["decisions"] == 4
    assert summary["provisional_parameters"] == 4
    out = runner.invoke(app, ["status"])
    assert "### Identity (as the gates read it)" in out.output and "review band" in out.output


def test_selftest_identity_reports_the_guarantee(cli: Context) -> None:
    result = runner.invoke(app, ["selftest", "--identity"])
    assert result.exit_code == 0, result.output
    assert "identity.guarantee" in result.output and "highest posterior" in result.output
    assert "0 failed" in result.output


def test_the_guarantee_check_fails_if_a_name_only_candidate_could_pass(
    cli: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = cli.db
    assert conn is not None
    results = name_only_posteriors(conn, cli.config, params.load_published(), top=3)
    assert len(results) == 6 and all(p < 0.9 and bits <= 0 for _, _, bits, p in results)
    checks = {c.name: c for c in identity_checks(cli.config, conn)}
    assert checks["identity.guarantee"].ok and checks["identity.settings"].ok
    monkeypatch.setattr(
        "efp.identity.checks.name_only_posteriors", lambda *a, **k: [(1, "census", 0.0, 0.95)]
    )
    assert not {c.name: c for c in identity_checks(cli.config, conn)}["identity.guarantee"].ok

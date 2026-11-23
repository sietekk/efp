from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from efp import db as database
from efp import runner as rn
from efp.config import Config, load_config
from tests.conftest import REPO
from tests.dbhelpers import insert_capture
from tests.runnerhelpers import (
    FakeComponent,
    add_identifier,
    decide,
    run,
    seed,
    set_stage,
    stage_of,
    statuses,
)


@pytest.fixture
def db(full_config: Config) -> Iterator[sqlite3.Connection]:
    connection = database.connect(full_config)
    seed(connection)
    yield connection
    connection.close()


def _done(conn: sqlite3.Connection, row_id: int, *stages: str) -> None:
    for stage in stages:
        set_stage(conn, row_id, stage, "done")


# --- eligibility ---------------------------------------------------------------------------


def test_only_the_first_stage_is_eligible_after_ingest(full_config: Config, db) -> None:  # type: ignore[no-untyped-def]
    ctx = rn.make_context(full_config, db)
    first = rn.dry_run(db, full_config, rn.RunOptions("resolve_links"), ctx)
    assert first.eligible == 6
    assert [r.row_id for r in first.rows] == [1, 2, 3, 4, 5, 6]  # year DESC, row_id
    for stage in ("court", "identity", "news", "documents", "social", "extract", "code"):
        report = rn.dry_run(db, full_config, rn.RunOptions(stage), ctx)
        assert report.eligible == 0, stage
    court = rn.dry_run(db, full_config, rn.RunOptions("court"), ctx)
    assert court.blockers == {"resolve_links": 6}
    assert "predecessor resolve_links is not done for 6 rows" in rn.render_dry_run(court)


def test_a_failed_predecessor_blocks_and_a_skipped_one_satisfies(full_config: Config, db) -> None:  # type: ignore[no-untyped-def]
    set_stage(db, 1, "resolve_links", "done")
    set_stage(db, 2, "resolve_links", "skipped", gate_reason="x")
    set_stage(db, 3, "resolve_links", "failed")
    set_stage(db, 4, "resolve_links", "needs_review")
    component = FakeComponent("court")
    run(db, full_config, "court", component)
    assert sorted(component.seen) == [1, 2]


def test_extract_needs_all_three_predecessors(full_config: Config, db) -> None:  # type: ignore[no-untyped-def]
    _done(db, 1, "documents", "news")
    set_stage(db, 1, "social", "pending")
    component = FakeComponent("extract")
    run(db, full_config, "extract", component)
    assert component.seen == []
    set_stage(db, 1, "social", "skipped", gate_reason="no_handle")
    run(db, full_config, "extract", component)
    assert component.seen == [1]


def test_batches_are_bounded_and_ordered(write_config: Callable[..., Path]) -> None:
    from tests.runnerhelpers import ALL_STAGES, ALL_STAGES_LINE

    config = load_config(
        write_config({ALL_STAGES_LINE: ALL_STAGES, "batch_size = 50": "batch_size = 2"})
    )
    conn = database.connect(config)
    seed(conn)
    component = FakeComponent("resolve_links")
    summary = run(conn, config, "resolve_links", component)
    assert component.batches == [[1, 2], [3, 4], [5, 6]]
    assert (summary.claimed, summary.done) == (6, 6)
    assert set(statuses(conn, "resolve_links").values()) == {"done"}
    row = stage_of(conn, 1, "resolve_links")
    assert (row["attempts"], row["tool_version"], row["started_at"] is not None) == (
        1,
        "fake-1",
        True,
    )
    assert json.loads(row["summary_json"]) == {"ok": True}
    conn.close()


def test_filters(full_config: Config, db) -> None:  # type: ignore[no-untyped-def]
    component = FakeComponent("resolve_links")
    run(db, full_config, "resolve_links", component, state="oh")
    assert sorted(component.seen) == [2, 4, 6]
    component = FakeComponent("resolve_links")
    run(db, full_config, "resolve_links", component, year_from=2019, limit=2)
    assert component.seen == [1, 3]  # CT 2020 and CT 2019 (OH 2019 was done by the first run)


def test_sample_restricts_to_the_frozen_rows(full_config: Config, db) -> None:  # type: ignore[no-untyped-def]
    Path(full_config.scope.sample_file).write_text(
        json.dumps({"rows": [{"row_id": 3, "row_sha256": "0"}, {"row_id": 5, "row_sha256": "0"}]})
    )
    component = FakeComponent("resolve_links")
    run(db, full_config, "resolve_links", component, sample=True)
    assert sorted(component.seen) == [3, 5]


def test_a_stage_with_no_component_runs_nothing(full_config: Config, db) -> None:  # type: ignore[no-untyped-def]
    _done(db, 1, "resolve_links", "court")
    summary = run(db, full_config, "news", None)  # no component is built for news yet
    assert summary.no_component and summary.claimed == 0
    assert set(statuses(db, "news").values()) == {"pending"}


def test_stage_validation(full_config: Config, write_config: Callable[..., Path]) -> None:
    rn.validate_stages(full_config)
    bad = load_config(write_config({'stages = ["resolve_links", "court"]': 'stages = ["court"]'}))
    with pytest.raises(rn.RunnerError, match="missing a predecessor"):
        rn.validate_stages(bad)


# --- gates ----------------------------------------------------------------------------------


def test_identity_gate_needs_court_done(full_config: Config, db) -> None:  # type: ignore[no-untyped-def]
    _done(db, 1, "resolve_links", "court")
    _done(db, 2, "resolve_links")
    set_stage(db, 2, "court", "skipped", gate_reason="x")  # satisfies the query, fails the gate
    component = FakeComponent("identity")
    summary = run(db, full_config, "identity", component)
    assert component.seen == [1]
    assert summary.gate_skipped == {"court_not_done": 1}
    skipped = stage_of(db, 2, "identity")
    assert (skipped["status"], skipped["gate_reason"]) == ("skipped", "court_not_done")


def test_a_failed_identity_gate_cascades_to_news_and_social_only(full_config: Config, db) -> None:  # type: ignore[no-untyped-def]
    _done(db, 2, "resolve_links")
    set_stage(db, 2, "court", "skipped", gate_reason="x")
    run(db, full_config, "identity", FakeComponent("identity"))
    got = {
        s: stage_of(db, 2, s)["status"] for s in ("news", "social", "documents", "extract", "code")
    }
    assert got == {
        "news": "skipped",
        "social": "skipped",
        "documents": "pending",
        "extract": "pending",
        "code": "pending",
    }
    assert stage_of(db, 2, "social")["gate_reason"] == "court_not_done"


def test_news_gate_both_outcomes(full_config: Config, db) -> None:  # type: ignore[no-untyped-def]
    for row_id in (1, 2, 3):
        _done(db, row_id, "resolve_links", "court", "identity")
    decide(db, 1, "confirmed")
    decide(db, 2, "unresolved")
    # row 3 has no identity decision at all: treated as unresolved, never guessed
    component = FakeComponent("news")
    summary = run(db, full_config, "news", component)
    assert component.seen == [1]
    assert summary.gate_skipped == {"identity_unresolved": 2}
    assert stage_of(db, 2, "news")["gate_reason"] == "identity_unresolved"


def test_a_reversal_is_the_latest_identity_decision(full_config: Config, db) -> None:  # type: ignore[no-untyped-def]
    _done(db, 1, "resolve_links", "court", "identity")
    decide(db, 1, "unresolved")
    decide(db, 1, "confirmed")  # a reversal is a new row
    component = FakeComponent("news")
    run(db, full_config, "news", component)
    assert component.seen == [1]


def _ready_for_social(conn: sqlite3.Connection, row_id: int) -> None:
    _done(conn, row_id, "resolve_links", "court", "identity", "news")


def _social_config(write_config: Callable[..., Path], *, enabled: bool) -> Config:
    from tests.runnerhelpers import ALL_STAGES, ALL_STAGES_LINE

    flag = "true" if enabled else "false"
    return load_config(
        write_config(
            {ALL_STAGES_LINE: ALL_STAGES, "social_enabled = false": f"social_enabled = {flag}"}
        )
    )


def test_social_gate_names_each_reason(write_config: Callable[..., Path]) -> None:
    config = _social_config(write_config, enabled=True)
    conn = database.connect(config)
    seed(conn)
    for row_id in (1, 2, 3, 4):
        _ready_for_social(conn, row_id)
    decide(conn, 1, "confirmed")
    add_identifier(conn, 1, "handle")  # passes
    decide(conn, 2, "confirmed")  # confirmed, but no handle or email
    decide(conn, 3, "probable")
    add_identifier(conn, 3, "email")  # an email exists but identity is not confirmed
    set_stage(conn, 4, "news", "skipped", gate_reason="x")  # news not done
    component = FakeComponent("social")
    summary = run(conn, config, "social", component)
    assert component.seen == [1]
    assert summary.gate_skipped == {
        "no_handle": 1,
        "identity_not_confirmed": 1,
        "news_not_done": 1,
    }
    # a social failure never cascades: extract still sees the row
    assert stage_of(conn, 2, "extract")["status"] == "pending"
    conn.close()


def test_social_email_alone_satisfies_the_identifier_requirement(
    write_config: Callable[..., Path],
) -> None:
    config = _social_config(write_config, enabled=True)
    conn = database.connect(config)
    seed(conn)
    _ready_for_social(conn, 1)
    decide(conn, 1, "confirmed")
    add_identifier(conn, 1, "email")
    component = FakeComponent("social")
    run(conn, config, "social", component)
    assert component.seen == [1]
    conn.close()


def test_social_disabled_is_a_reason_not_a_failure(write_config: Callable[..., Path]) -> None:
    config = _social_config(write_config, enabled=False)
    conn = database.connect(config)
    seed(conn)
    _ready_for_social(conn, 1)
    decide(conn, 1, "confirmed")
    add_identifier(conn, 1, "handle")
    component = FakeComponent("social")
    summary = run(conn, config, "social", component)
    assert component.seen == [] and summary.gate_skipped == {"social_disabled": 1}
    conn.close()


def test_code_gate_accepts_done_or_skipped_extract_and_names_the_reason(
    full_config: Config, db
) -> None:  # type: ignore[no-untyped-def]
    assert rn.gate_code(db, full_config, 1) == "extract_not_done"
    set_stage(db, 1, "extract", "done")
    assert rn.gate_code(db, full_config, 1) is None
    set_stage(db, 2, "extract", "skipped", gate_reason="no_handle")
    assert rn.gate_code(db, full_config, 2) is None
    component = FakeComponent("code")
    run(db, full_config, "code", component)
    assert sorted(component.seen) == [1, 2]  # a row stopped at a gate still reaches `code`


def test_a_component_gate_can_skip_a_row_with_its_own_reason(full_config: Config, db) -> None:  # type: ignore[no-untyped-def]
    component = FakeComponent("resolve_links", gate_reasons={2: "no_urls"})
    summary = run(db, full_config, "resolve_links", component)
    assert 2 not in component.seen and summary.gate_skipped == {"no_urls": 1}
    assert stage_of(db, 2, "resolve_links")["gate_reason"] == "no_urls"


def test_needs_review_is_recorded_and_blocks_downstream(full_config: Config, db) -> None:  # type: ignore[no-untyped-def]
    run(db, full_config, "resolve_links", FakeComponent("resolve_links", needs_review={1}))
    assert stage_of(db, 1, "resolve_links")["status"] == "needs_review"
    component = FakeComponent("court")
    run(db, full_config, "court", component)
    assert 1 not in component.seen and sorted(component.seen) == [2, 3, 4, 5, 6]


# --- idempotence ----------------------------------------------------------------------------


def test_same_input_hash_is_a_no_op_and_force_reruns(full_config: Config, db) -> None:  # type: ignore[no-untyped-def]
    component = FakeComponent("resolve_links")
    run(db, full_config, "resolve_links", component)
    assert len(component.seen) == 6
    summary = run(db, full_config, "resolve_links", component, recheck=True)
    assert summary.noop == 6 and len(component.seen) == 6  # nothing was called
    component.hash_value = "h2"
    summary = run(db, full_config, "resolve_links", component, recheck=True)
    assert summary.claimed == 6 and len(component.seen) == 12
    component.hash_value = "h2"
    run(db, full_config, "resolve_links", component, force=True)
    assert len(component.seen) == 18
    assert stage_of(db, 1, "resolve_links")["input_hash"] == "h2"


def test_a_plain_run_never_touches_done_rows(full_config: Config, db) -> None:  # type: ignore[no-untyped-def]
    component = FakeComponent("resolve_links")
    run(db, full_config, "resolve_links", component)
    run(db, full_config, "resolve_links", component)
    assert len(component.seen) == 6


def test_reset_repends_without_touching_captures(full_config: Config, db) -> None:  # type: ignore[no-untyped-def]
    run(db, full_config, "resolve_links", FakeComponent("resolve_links"))
    insert_capture(db, 1, 1, stage="resolve_links")
    db.commit()
    set_stage(db, 1, "identity", "skipped", gate_reason="court_not_done")
    set_stage(db, 1, "news", "skipped", gate_reason="court_not_done")
    set_stage(db, 2, "identity", "done")
    summary = run(db, full_config, "news", None, reset=True)  # (no news component yet)
    assert summary.reset == 6
    assert stage_of(db, 1, "identity")["status"] == "skipped"  # only the selected stage is reset
    assert stage_of(db, 2, "identity")["status"] == "done"
    assert stage_of(db, 1, "news")["status"] == "pending"  # a stranded skip is re-opened
    assert db.execute("SELECT count(*) FROM captures").fetchone()[0] == 1
    assert stage_of(db, 2, "resolve_links")["status"] == "done"


def test_input_hash_helper_is_stable() -> None:
    assert rn.input_hash("a", 1, {"b": 2, "a": 1}) == rn.input_hash("a", 1, {"a": 1, "b": 2})
    assert rn.input_hash("a") != rn.input_hash("b")


# --- attempts -------------------------------------------------------------------------------


def test_third_failure_leaves_failed_and_retry_failed_reruns_once(full_config: Config, db) -> None:  # type: ignore[no-untyped-def]
    component = FakeComponent("resolve_links", fail={1})
    for expected in (1, 2, 3):
        run(db, full_config, "resolve_links", component)
        assert stage_of(db, 1, "resolve_links")["attempts"] == expected
    assert component.seen.count(1) == 3  # each row once per run, never looped within a run
    row = stage_of(db, 1, "resolve_links")
    assert (row["status"], row["last_error"]) == ("failed", "boom")
    run(db, full_config, "resolve_links", component)
    assert component.seen.count(1) == 3  # the fourth attempt needs --retry-failed
    run(db, full_config, "resolve_links", component, retry_failed=True)
    assert component.seen.count(1) == 4
    assert stage_of(db, 1, "resolve_links")["attempts"] == 4


def test_a_component_exception_fails_the_batch_not_the_run(full_config: Config, db) -> None:  # type: ignore[no-untyped-def]
    summary = run(
        db, full_config, "resolve_links", FakeComponent("resolve_links", raise_error=True)
    )
    assert summary.failed == 6
    assert "component exploded" in stage_of(db, 1, "resolve_links")["last_error"]


# --- recovery -------------------------------------------------------------------------------


def _ago(minutes: int) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat(timespec="seconds")


def test_stale_running_rows_are_recovered_without_counting_the_attempt(
    full_config: Config, db
) -> None:  # type: ignore[no-untyped-def]
    set_stage(db, 1, "resolve_links", "running", attempts=1, started_at=_ago(180))
    set_stage(db, 2, "resolve_links", "running", attempts=1, started_at=_ago(5))
    component = FakeComponent("resolve_links")
    summary = run(db, full_config, "resolve_links", component)
    assert summary.recovered == 1
    assert 1 in component.seen and 2 not in component.seen  # a fresh claim is left alone
    assert stage_of(db, 1, "resolve_links")["attempts"] == 1  # 1 - 1 + 1: the crash did not count
    assert stage_of(db, 2, "resolve_links")["status"] == "running"


def test_recover_flag_resets_every_running_row(full_config: Config, db) -> None:  # type: ignore[no-untyped-def]
    set_stage(db, 2, "resolve_links", "running", attempts=1, started_at=_ago(1))
    component = FakeComponent("resolve_links")
    summary = run(db, full_config, "resolve_links", component, recover=True)
    assert summary.recovered == 1 and 2 in component.seen


# --- dry run --------------------------------------------------------------------------------


def _dump(conn: sqlite3.Connection) -> list[tuple[object, ...]]:
    return conn.execute("SELECT * FROM case_stage ORDER BY row_id, stage").fetchall()


def test_dry_run_changes_nothing_and_shows_gates(full_config: Config, db) -> None:  # type: ignore[no-untyped-def]
    _done(db, 1, "resolve_links")
    set_stage(db, 1, "court", "skipped", gate_reason="x")
    before = _dump(db)
    ctx = rn.make_context(full_config, db)
    report = rn.dry_run(db, full_config, rn.RunOptions("identity", dry_run=True), ctx)
    assert _dump(db) == before
    assert report.eligible == 1 and report.rows[0].gate == "court_not_done"
    assert report.rows[0].predecessors == {"court": "skipped"}
    assert "gate: skip (court_not_done)" in rn.render_dry_run(report)


# --- concurrency and interruption (real processes) ------------------------------------------


def _worker(config_path: Path, stage: str, out: Path, *extra: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: S603  (our own interpreter, our own module)
        [sys.executable, "-m", "tests.runner_worker", str(config_path), stage, str(out), *extra],
        cwd=REPO,
    )


@pytest.fixture
def shared(write_config: Callable[..., Path], tmp_path: Path) -> tuple[Path, Config]:
    from tests.runnerhelpers import ALL_STAGES, ALL_STAGES_LINE

    path = write_config({ALL_STAGES_LINE: ALL_STAGES, "batch_size = 50": "batch_size = 2"})
    config = load_config(path)
    conn = database.connect(config)
    seed(conn, [(i, 2020 - i, "CT") for i in range(1, 13)])
    conn.close()
    return path, config


def test_two_processes_never_claim_the_same_row(
    shared: tuple[Path, Config], tmp_path: Path
) -> None:
    path, _config = shared
    outs = [tmp_path / "a.txt", tmp_path / "b.txt"]
    go = tmp_path / "go"
    procs = [_worker(path, "resolve_links", out, "--go", str(go)) for out in outs]
    go.touch()  # both are past their imports and waiting: release them together
    assert [p.wait(timeout=60) for p in procs] == [0, 0]
    per_worker = [[int(x) for x in out.read_text().split()] if out.exists() else [] for out in outs]
    assert all(per_worker), "both workers must have claimed rows for this test to prove anything"
    assert sorted(per_worker[0] + per_worker[1]) == list(range(1, 13))  # every row once, none twice


def test_a_killed_run_resumes_without_duplicate_work(
    shared: tuple[Path, Config], tmp_path: Path
) -> None:
    path, config = shared
    out = tmp_path / "done.txt"
    first = _worker(path, "resolve_links", out, "--die-after", "2")
    assert first.wait(timeout=60) == 9  # killed mid-batch
    conn = database.connect(config, migrate=False, create=False)
    states = statuses(conn, "resolve_links")
    assert sorted(r for r, s in states.items() if s == "done") == [1, 2]
    assert sorted(r for r, s in states.items() if s == "running") == [3, 4]
    conn.close()
    second = _worker(path, "resolve_links", out, "--recover")
    assert second.wait(timeout=60) == 0
    seen = [int(x) for x in out.read_text().split()]
    assert sorted(seen) == list(range(1, 13))  # the two finished rows were not redone
    conn = database.connect(config, migrate=False, create=False)
    assert set(statuses(conn, "resolve_links").values()) == {"done"}
    assert stage_of(conn, 3, "resolve_links")["attempts"] == 1  # the interrupted claim not counted
    conn.close()


# --- review follow-ups ------------------------------------------------------------------------


def test_a_component_that_skips_a_row_cascades_like_a_gate(full_config: Config, db) -> None:  # type: ignore[no-untyped-def]
    _done(db, 1, "resolve_links", "court")
    summary = run(db, full_config, "identity", FakeComponent("identity", skip={1: "no_name_match"}))
    assert summary.skipped == 1 and summary.gate_skipped == {"no_name_match": 1}
    assert stage_of(db, 1, "identity")["gate_reason"] == "no_name_match"
    assert stage_of(db, 1, "news")["status"] == "skipped"
    assert stage_of(db, 1, "documents")["status"] == "pending"


def test_a_row_the_component_does_not_answer_is_failed_not_stuck_running(
    full_config: Config, db
) -> None:  # type: ignore[no-untyped-def]
    summary = run(db, full_config, "resolve_links", FakeComponent("resolve_links", omit={2}))
    row = stage_of(db, 2, "resolve_links")
    assert (row["status"], summary.failed) == ("failed", 1)
    assert "no result" in row["last_error"]


def test_error_text_is_redacted_before_it_reaches_the_database(full_config: Config, db) -> None:  # type: ignore[no-untyped-def]
    from efp.logging import register_secret_values

    register_secret_values(["runner-test-token-0d9f3a"])
    component = FakeComponent(
        "resolve_links", fail={1}, error_text="403 for https://x.test/?key=runner-test-token-0d9f3a"
    )
    run(db, full_config, "resolve_links", component)
    stored = stage_of(db, 1, "resolve_links")["last_error"]
    assert "runner-test-token-0d9f3a" not in stored and "403" in stored
    run(
        db,
        full_config,
        "resolve_links",
        FakeComponent("resolve_links", raise_error=True),
        retry_failed=True,
    )
    assert "runner-test-token-0d9f3a" not in stage_of(db, 1, "resolve_links")["last_error"]


def test_reset_cannot_be_combined_with_limit_and_respects_state(full_config: Config, db) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(rn.RunnerError, match="--limit"):
        run(db, full_config, "resolve_links", FakeComponent("resolve_links"), reset=True, limit=2)
    for row_id in range(1, 7):
        set_stage(db, row_id, "identity", "done")
    summary = run(
        db, full_config, "identity", None, reset=True, state="CT"
    )  # (no identity component)
    assert summary.reset == 3
    assert statuses(db, "identity") == {
        1: "pending",
        2: "done",
        3: "pending",
        4: "done",
        5: "pending",
        6: "done",
    }


def test_a_done_row_whose_gate_now_fails_is_left_alone_on_recheck(full_config: Config, db) -> None:  # type: ignore[no-untyped-def]
    run(db, full_config, "resolve_links", FakeComponent("resolve_links"))
    component = FakeComponent("resolve_links", gate_reasons={1: "withdrawn"}, hash_value="h2")
    summary = run(db, full_config, "resolve_links", component, recheck=True)
    assert 1 not in component.seen and summary.gate_skipped == {}
    assert stage_of(db, 1, "resolve_links")["status"] == "done"


def test_dry_run_with_reset_says_how_many_rows_it_would_reset(full_config: Config, db) -> None:  # type: ignore[no-untyped-def]
    ctx = rn.make_context(full_config, db)
    report = rn.dry_run(db, full_config, rn.RunOptions("resolve_links", reset=True), ctx)
    assert report.would_reset == 6 and "would set 6 rows back to pending" in rn.render_dry_run(
        report
    )


def test_cli_dry_run_and_status_change_nothing_on_a_database_one_migration_behind(
    full_config_path: Path, migrations_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from efp.cli import app

    newest = database.discover_migrations()[-1]
    (migrations_copy / newest.path.name).unlink()  # the operator's database lacks the newest
    config = load_config(full_config_path)
    conn = database.connect(config)
    seed(conn)
    before = (_dump(conn), conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0])
    conn.close()
    monkeypatch.setenv("EFP_CONFIG", str(full_config_path))
    cli = CliRunner()
    for argv in (["run", "resolve_links", "--dry-run"], ["run", "court", "--dry-run"], ["status"]):
        result = cli.invoke(app, argv)
        assert result.exit_code == 0, result.output
    conn = database.connect(config, migrate=False, create=False)
    assert (
        _dump(conn),
        conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0],
    ) == before
    assert before[1] == newest.id - 1  # the dry run did not apply the newest migration
    conn.close()


def test_recheck_also_revisits_rows_waiting_in_the_review_band(full_config: Config, db) -> None:  # type: ignore[no-untyped-def]
    """A needs_review row is settled like a done one: new data revisits it, an unchanged hash does not."""
    component = FakeComponent("resolve_links", needs_review={1})
    run(db, full_config, "resolve_links", component)
    assert stage_of(db, 1, "resolve_links")["status"] == "needs_review"
    component.batches.clear()
    unchanged = run(db, full_config, "resolve_links", component, recheck=True)
    assert 1 not in component.seen and unchanged.noop >= 1  # the same inputs: nothing to do
    component.hash_value = "h2"  # a reference load changes every row's input hash
    component.needs_review = set()
    component.batches.clear()
    run(db, full_config, "resolve_links", component, recheck=True)
    assert 1 in component.seen
    assert stage_of(db, 1, "resolve_links")["input_hash"] == "h2"
    assert stage_of(db, 1, "resolve_links")["status"] == "done"


def test_a_plain_run_still_leaves_review_band_rows_alone(full_config: Config, db) -> None:  # type: ignore[no-untyped-def]
    component = FakeComponent("resolve_links", needs_review={1})
    run(db, full_config, "resolve_links", component)
    component.batches.clear()
    run(db, full_config, "resolve_links", component)
    assert 1 not in component.seen


def test_a_crash_during_a_recheck_returns_a_review_band_row_to_needs_review(
    full_config: Config, db
) -> None:  # type: ignore[no-untyped-def]
    """A claimed row is `running`; recovery must put a settled row back as it was, not as pending,
    or a review-band row drops out of `efp review` until the stage is run again."""
    from efp.runner import recover

    component = FakeComponent("resolve_links", needs_review={1})
    run(db, full_config, "resolve_links", component)
    assert stage_of(db, 1, "resolve_links")["status"] == "needs_review"

    class Crash(FakeComponent):
        def run(self, batch, ctx):  # type: ignore[no-untyped-def]
            raise KeyboardInterrupt  # the process dies: nothing records an outcome

    crashing = Crash("resolve_links", hash_value="h2")
    with pytest.raises(KeyboardInterrupt):
        run(db, full_config, "resolve_links", crashing, recheck=True)
    assert stage_of(db, 1, "resolve_links")["status"] == "running"
    recover(db, "resolve_links", stale_after_minutes=None, force=True)
    db.commit()
    row = stage_of(db, 1, "resolve_links")
    assert (
        row["status"] == "needs_review" and row["attempts"] == 1
    )  # as before; the attempt is not counted
    # a done row is put back as done (its last result stands), a pending one as pending
    assert stage_of(db, 3, "resolve_links")["status"] in ("done", "pending")


def test_recovery_returns_a_pending_row_to_pending(full_config: Config, db) -> None:  # type: ignore[no-untyped-def]
    from efp.runner import recover

    class Crash(FakeComponent):
        def run(self, batch, ctx):  # type: ignore[no-untyped-def]
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run(db, full_config, "resolve_links", Crash("resolve_links"), limit=1)
    running = [r for r in range(1, 7) if stage_of(db, r, "resolve_links")["status"] == "running"]
    assert running
    recover(db, "resolve_links", stale_after_minutes=None, force=True)
    db.commit()
    assert all(stage_of(db, r, "resolve_links")["status"] == "pending" for r in running)

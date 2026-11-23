"""The identity stage end to end on synthetic rows: candidates, prior, evidence, thresholds."""

from __future__ import annotations

import json
import logging
import sqlite3

import pytest

from efp import identity
from efp.config import Config
from efp.modules import Registry
from efp.types import Context, Row
from tests.identity_helpers import (
    seed_candidate,
    seed_court,
    seed_link,
    seed_reference,
    seed_row,
)

COURT_TEXT = (
    "John Quincy Raresurname, 52, of Hartford, Connecticut, was sentenced today. "
    "Case No. 3:19-cr-123."
)
COURT_IDS = [
    ("age", "52", "Raresurname, 52, of Hartford"),
    ("city", "Hartford", "52, of Hartford"),
    ("case_number", "3:19-cr-123", "Case No. 3:19-cr-123"),
]


@pytest.fixture
def ctx(config: Config, conn: sqlite3.Connection) -> Context:
    seed_reference(conn)
    return Context(config=config, db=conn, log=logging.getLogger("test"))


@pytest.fixture
def registry() -> Registry:
    reg = Registry()
    identity.register_modules(reg)
    return reg


def run_rows(
    conn: sqlite3.Connection, ctx: Context, registry: Registry, *ids: int
) -> dict[int, dict]:
    rows = []
    for row_id in ids:
        r = conn.execute(
            "SELECT year, state, name, details, source_raw FROM rows WHERE row_id = ?", (row_id,)
        ).fetchone()
        rows.append(
            Row(
                row_id=row_id, year=r[0], state=r[1],
                source={"name": r[2], "details": r[3], "source_raw": r[4], "state": r[1]},
            )
        )  # fmt: skip
    results = identity.process_batch(rows, ctx, registry)
    return {r.row_id: r.summary_json for r in results}


def test_rare_name_with_independent_corroboration_is_confirmed(
    conn: sqlite3.Connection, ctx: Context, registry: Registry, config: Config
) -> None:
    seed_row(
        conn,
        1,
        "John Q. Raresurname",
        details="John Q. Raresurname, 52, of Hartford, was convicted.",
    )
    seed_court(
        conn,
        config,
        1,
        text=COURT_TEXT,
        case_number="3:19-cr-123",
        url="https://example.test/release",
        identifiers=COURT_IDS,
    )
    summary = run_rows(conn, ctx, registry, 1)[1]
    assert summary["outcome"] == "confirmed", summary
    row = conn.execute(
        "SELECT outcome, decided_by, stage, batch FROM decisions WHERE row_id = 1 AND kind = 'identity'"
    ).fetchone()
    assert row[0] == "confirmed" and row[2:] == ("identity", "main")
    assert "params=provisional" in row[1] and "splink:" in row[1] and "prior=census" in row[1]
    cand = conn.execute(
        "SELECT waterfall_json, fields_json, prior_namesakes FROM identity_candidates "
        "WHERE row_id = 1 AND source = 'court'"
    ).fetchone()
    steps = {s["comparison"]: s["level"] for s in json.loads(cand[0])["steps"]}
    assert steps["middle"] == "initial" and steps["dob"] == "within_1" and steps["city"] == "exact"
    assert cand[2] == 1.0  # a namesake count below one is floored at one
    fields = json.loads(cand[1])
    assert fields["prior"]["raw_namesakes"] < 1 and fields["prior"]["p_surname_how"] == "listed"


def test_the_same_evidence_on_a_common_name_stays_unresolved(
    conn: sqlite3.Connection, ctx: Context, registry: Registry, config: Config
) -> None:
    text = COURT_TEXT.replace("Raresurname", "Commonsurname")
    seed_row(
        conn,
        1,
        "John Q. Commonsurname",
        details="John Q. Commonsurname, 52, of Hartford, was convicted.",
    )
    seed_court(
        conn,
        config,
        1,
        text=text,
        case_number="3:19-cr-123",
        url="https://example.test/release",
        identifiers=COURT_IDS,
    )
    summary = run_rows(conn, ctx, registry, 1)[1]
    assert summary["outcome"] == "unresolved"
    assert summary["reason"] == "below_review_threshold"


def test_name_state_and_year_alone_is_never_enough(
    conn: sqlite3.Connection, ctx: Context, registry: Registry, config: Config
) -> None:
    seed_row(conn, 1, "John Raresurname")
    seed_court(
        conn,
        config,
        1,
        text="John Raresurname was sentenced.",
        case_number=None,
        url="https://example.test/a",
    )
    summary = run_rows(conn, ctx, registry, 1)[1]
    assert summary["outcome"] == "unresolved"
    cand = conn.execute(
        "SELECT posterior, match_weight FROM identity_candidates WHERE row_id = 1"
    ).fetchone()
    # evidence is zero, or a hair below it: the term-frequency adjustment may only lower a weight
    # (in a one-row workbook every name is "frequent"), never raise it
    assert -0.5 < cand[1] <= 0.0 and cand[0] <= 0.5


def test_a_document_read_by_both_sides_is_one_observation(
    conn: sqlite3.Connection, ctx: Context, registry: Registry, config: Config
) -> None:
    """The row's own link and the court record are the same press release: its age and city do not
    corroborate themselves."""
    url = "https://example.test/release"
    seed_row(conn, 1, "John Q. Raresurname")
    seed_link(conn, config, 1, f"<html><p>{COURT_TEXT}</p></html>", url)
    seed_court(
        conn, config, 1, text=COURT_TEXT, case_number="3:19-cr-123", url=url, identifiers=COURT_IDS
    )
    summary = run_rows(conn, ctx, registry, 1)[1]
    assert summary["outcome"] == "unresolved"
    fields = json.loads(
        conn.execute("SELECT fields_json FROM identity_candidates WHERE row_id = 1").fetchone()[0]
    )
    assert {m["kind"] for m in fields["masked"]} >= {"age", "city", "case_number"}


def test_an_independent_news_link_corroborates_the_court_record(
    conn: sqlite3.Connection, ctx: Context, registry: Registry, config: Config
) -> None:
    seed_row(conn, 1, "John Q. Raresurname")
    seed_link(
        conn,
        config,
        1,
        "<html><body><p>John Q. Raresurname, 52, of Hartford, voted twice, police said in a long enough article body.</p></body></html>",
        "https://news.example.test/story",
    )
    seed_court(
        conn,
        config,
        1,
        text=COURT_TEXT,
        case_number="3:19-cr-123",
        url="https://example.test/release",
        identifiers=COURT_IDS,
    )
    assert run_rows(conn, ctx, registry, 1)[1]["outcome"] == "confirmed"


def test_reference_candidate_full_middle_name_goes_to_review(
    conn: sqlite3.Connection, ctx: Context, registry: Registry
) -> None:
    seed_row(conn, 1, "Jane Marie Raresurname")
    seed_candidate(conn, "fec", "H0XX00001", "RARESURNAME", "JANE", middle="MARIE")
    summary = run_rows(conn, ctx, registry, 1)[1]
    assert summary["outcome"] == "needs_review"
    assert summary["reason"] == "in_review_band"
    status = conn.execute("SELECT 1").fetchone()
    assert status


def test_reference_candidate_without_evidence_is_unresolved(
    conn: sqlite3.Connection, ctx: Context, registry: Registry
) -> None:
    seed_row(conn, 1, "Jane Raresurname")
    seed_candidate(conn, "fec", "H0XX00001", "RARESURNAME", "JANE")
    assert run_rows(conn, ctx, registry, 1)[1]["outcome"] == "unresolved"


def test_an_alias_is_expanded_into_the_candidate_set(
    conn: sqlite3.Connection, ctx: Context, registry: Registry
) -> None:
    seed_row(conn, 1, "Jane Marie Smithson aka Jane Marie Raresurname")
    seed_candidate(conn, "fec", "H0XX00002", "RARESURNAME", "JANE", middle="MARIE")
    summary = run_rows(conn, ctx, registry, 1)[1]
    assert summary["candidates"] == 1 and summary["outcome"] == "needs_review"


def test_no_candidate_is_unresolved_with_the_reason(
    conn: sqlite3.Connection, ctx: Context, registry: Registry
) -> None:
    seed_row(conn, 1, "John Raresurname")
    summary = run_rows(conn, ctx, registry, 1)[1]
    assert summary["outcome"] == "unresolved" and summary["reason"] == "no_candidate"
    skipped = {m["name"]: m["reason"] for m in summary["modules"] if m["status"] == "skipped"}
    assert "voter_file" in " ".join(skipped) and "dime" in " ".join(skipped)


def test_a_weak_court_record_is_at_most_reviewed(
    conn: sqlite3.Connection, ctx: Context, registry: Registry, config: Config
) -> None:
    seed_row(
        conn,
        1,
        "John Q. Raresurname",
        details="John Q. Raresurname, 52, of Hartford, was convicted.",
    )
    seed_court(
        conn,
        config,
        1,
        text=COURT_TEXT,
        case_number="3:19-cr-123",
        url="https://example.test/release",
        identifiers=COURT_IDS,
        weak=1,
    )
    summary = run_rows(conn, ctx, registry, 1)[1]
    assert summary["outcome"] in ("needs_review", "unresolved")
    assert summary["outcome"] != "confirmed"


def test_empty_reference_tables_use_the_ceiling_and_say_so(
    config: Config, conn: sqlite3.Connection, registry: Registry
) -> None:
    ctx = Context(config=config, db=conn, log=logging.getLogger("test"))
    seed_row(
        conn,
        1,
        "John Q. Raresurname",
        details="John Q. Raresurname, 52, of Hartford, was convicted.",
    )
    seed_court(
        conn,
        config,
        1,
        text=COURT_TEXT,
        case_number="3:19-cr-123",
        url="https://example.test/release",
        identifiers=COURT_IDS,
    )
    summary = run_rows(conn, ctx, registry, 1)[1]
    assert summary["prior"] == "ceiling" and summary["outcome"] == "unresolved"
    by = conn.execute("SELECT decided_by FROM decisions WHERE row_id = 1").fetchone()[0]
    assert "prior=ceiling" in by


def test_only_the_best_candidates_of_a_row_are_stored(
    conn: sqlite3.Connection, ctx: Context, registry: Registry
) -> None:
    seed_row(conn, 1, "Jane Marie Commonsurname")
    for i in range(40):  # a common name matches many public figures: keep the 25 best
        seed_candidate(
            conn, "fec", f"F{i:02d}", "COMMONSURNAME", "JANE", middle="MARIE" if i < 3 else ""
        )
    summary = run_rows(conn, ctx, registry, 1)[1]
    assert (
        summary["candidates"] == 40
        and summary["candidates_stored"] == identity.MAX_CANDIDATES_STORED
    )
    stored = conn.execute(
        "SELECT count(*), max(posterior) FROM identity_candidates WHERE row_id = 1 AND current = 1"
    ).fetchone()
    assert stored[0] == identity.MAX_CANDIDATES_STORED
    best = conn.execute(
        "SELECT json_extract(fields_json, '$.person.middle') FROM identity_candidates "
        "WHERE row_id = 1 ORDER BY posterior DESC LIMIT 1"
    ).fetchone()[0]
    assert best == "MARIE"  # the ones with evidence are the ones kept


def test_a_filing_read_by_the_documents_stage_adds_its_identifiers_to_the_case(
    conn: sqlite3.Connection, ctx: Context, registry: Registry, config: Config
) -> None:
    """The press release names no middle name; the plea agreement the documents stage read does."""
    seed_row(
        conn,
        1,
        "John Q. Raresurname",
        details="John Q. Raresurname, 52, of Hartford, was convicted.",
    )
    release = "John Raresurname, of Hartford, was sentenced today. Case No. 3:19-cr-123."
    seed_court(
        conn, config, 1, text=release, case_number="3:19-cr-123", url="https://example.test/a"
    )
    assert run_rows(conn, ctx, registry, 1)[1]["outcome"] == "unresolved"
    plea = "John Quincy Raresurname, 52, of Hartford, pleaded guilty. Case No. 3:19-cr-123."
    _, text_capture = seed_court(
        conn, config, 1, text=plea, case_number="3:19-cr-123", url="https://example.test/plea",
        identifiers=[],
    )  # fmt: skip
    record = conn.execute("SELECT max(record_id) FROM court_records").fetchone()[0]
    conn.execute(
        "UPDATE court_records SET document_kind = 'filing', docket_id = 7 WHERE record_id = ?",
        (record,),
    )
    conn.execute(  # the docket the filing belongs to must be confirmed too
        "INSERT INTO court_records (row_id, source_kind, case_number, document_kind, strategy, "
        "docket_id, found_at, weak, current) VALUES (1, 'federal_docket', '3:19-cr-123', 'docket', "
        "'court.recap', 7, 's', 0, 1)"
    )
    task = Row(row_id=1, year=2018, state="CT", source={"name": "John Q. Raresurname"})
    before = identity.input_hash(task, ctx)
    conn.execute(
        "INSERT INTO documents (row_id, record_id, capture_id, text_capture_id, current) "
        "VALUES (1, ?, ?, ?, 1)",
        (record, text_capture, text_capture),
    )
    conn.commit()
    summary = run_rows(conn, ctx, registry, 1)[1]
    fields = conn.execute(
        "SELECT json_extract(fields_json, '$.person.middle') FROM identity_candidates "
        "WHERE row_id = 1 AND current = 1 AND source = 'court'"
    ).fetchall()
    assert any(f[0] == "Quincy" for f in fields), fields
    assert summary["outcome"] in ("confirmed", "needs_review")
    assert before != identity.input_hash(task, ctx)  # a documents run makes the row stale


def test_the_real_component_runs_through_the_runner_and_the_gates_follow_its_decisions(
    full_config: Config, conn: sqlite3.Connection
) -> None:
    """``efp run identity`` end to end: the gate needs court done, the outcome sets the stage's
    status, and the news gate reads the decision the stage wrote."""
    from efp import runner
    from efp.builtin_modules import register_builtin
    from efp.modules import REGISTRY
    from efp.runner import RunOptions, make_context, run_stage
    from tests.dbhelpers import STAMP

    register_builtin(REGISTRY)
    seed_reference(conn)
    seed_row(conn, 1, "Jane Marie Raresurname")  # a reference candidate with the full middle name
    seed_candidate(conn, "fec", "F1", "RARESURNAME", "JANE", middle="MARIE")
    seed_row(conn, 2, "Jane Raresurname")  # nobody to compare
    seed_row(conn, 3, "John Nocourt")  # its court stage has not run
    conn.execute("UPDATE case_stage SET status = 'pending' WHERE row_id = 3 AND stage = 'court'")
    conn.commit()
    ctx = make_context(full_config, conn)
    summary = run_stage(conn, full_config, RunOptions(stage="identity"), ctx)
    assert summary.claimed == 2 and summary.needs_review == 1 and summary.no_component is False
    status = dict(conn.execute("SELECT row_id, status FROM case_stage WHERE stage = 'identity'"))
    assert status == {1: "needs_review", 2: "done", 3: "pending"}  # court has not run for row 3
    # the review band blocks news until the operator decides; an unresolved row stops it
    assert runner.gate_news(conn, full_config, 1) == "identity_not_done"
    assert runner.gate_news(conn, full_config, 2) == "identity_unresolved"
    conn.execute(
        "UPDATE case_stage SET status = 'done' WHERE row_id = 1 AND stage = 'identity'"
    )  # what `efp review identity` does after a confirmation
    conn.execute(
        "INSERT INTO decisions (row_id, kind, outcome, decided_by, decided_at, stage) "
        "VALUES (1, 'review', 'confirmed', 'XX', ?, 'identity')",
        (STAMP,),
    )
    conn.commit()
    assert runner.gate_news(conn, full_config, 1) is None
    row = conn.execute(
        "SELECT input_hash, tool_version FROM case_stage WHERE row_id = 2 AND stage = 'identity'"
    ).fetchone()
    assert row[0] and row[1] == identity.TOOL_VERSION
    again = run_stage(conn, full_config, RunOptions(stage="identity", recheck=True), ctx)
    assert again.claimed == 0  # nothing changed: an unchanged input hash is a no-op


def test_a_row_with_no_candidate_is_tagged_with_the_prior_it_would_have_used(
    config: Config, conn: sqlite3.Connection, registry: Registry
) -> None:
    ctx = Context(config=config, db=conn, log=logging.getLogger("test"))
    seed_row(conn, 1, "John Raresurname")
    run_rows(conn, ctx, registry, 1)
    assert "prior=ceiling" in conn.execute("SELECT decided_by FROM decisions").fetchone()[0]

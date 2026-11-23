"""Threshold routing, competing candidates, weak records, batches and what the gates read."""

from __future__ import annotations

import json
import sqlite3

import pytest

from efp import runner
from efp.identity import decide as dec
from efp.identity.pairs import Pair
from efp.identity.prior import Prior
from efp.identity.profile import Person
from tests.dbhelpers import STAMP, insert_row


def scored(posterior_bits: float, *, source: str = "court", source_id: str = "a", weak: bool = False,
           agreeing: str = "middle:exact", prior_n: float = 1.0) -> dec.Scored:  # fmt: skip
    person = Person(source, source_id, "John", "", "Smith", extras={"weak": weak})
    water = {
        "evidence_bits": posterior_bits, "prior_odds": 1 / prior_n, "posterior": 0.0,
        "steps": [{"comparison": agreeing.split(":")[0], "level": agreeing.split(":")[1]}],
    }  # fmt: skip
    return dec.score_candidate(person, Pair(1, {}, {}, Prior(prior_n, {})), water)


def test_posterior_is_prior_odds_times_two_to_the_weight() -> None:
    assert dec.posterior(1 / 100, 0.0) == pytest.approx(1 / 101)
    assert dec.posterior(1 / 100, 10.0) == pytest.approx(10.24 / 11.24)


@pytest.mark.parametrize(
    ("bits", "outcome"),
    [(1.0, "unresolved"), (4.0, "needs_review"), (7.5, "confirmed"), (20.0, "confirmed")],
)
def test_the_three_outcomes(bits: float, outcome: str) -> None:
    # prior odds 1: posterior 0.67 / 0.94 / 0.995 / ~1
    assert dec.decide([scored(bits)], confirm=0.99, review=0.90).outcome == outcome


def test_the_band_is_closed_below_and_open_above() -> None:
    low = scored(0.0)
    low.posterior = 0.90
    assert dec.decide([low], confirm=0.99, review=0.90).outcome == "needs_review"
    high = scored(0.0)
    high.posterior = 0.99
    assert dec.decide([high], confirm=0.99, review=0.90).outcome == "confirmed"
    under = scored(0.0)
    under.posterior = 0.8999
    assert dec.decide([under], confirm=0.99, review=0.90).outcome == "unresolved"


def test_no_candidate_is_unresolved() -> None:
    out = dec.decide([], confirm=0.99, review=0.90)
    assert (out.outcome, out.reason) == ("unresolved", "no_candidate")


def test_the_stricter_sensitivity_threshold_moves_a_marginal_confirmation_to_review() -> None:
    marginal = scored(7.0)  # about 0.992
    assert dec.decide([marginal], confirm=0.99, review=0.90).outcome == "confirmed"
    assert dec.decide([marginal], confirm=0.995, review=0.90).outcome == "needs_review"


def test_two_candidates_of_one_source_both_in_the_band_go_to_review() -> None:
    a, b = scored(9.0, source_id="a"), scored(9.0, source_id="b")
    out = dec.decide([a, b], confirm=0.99, review=0.90)
    assert (out.outcome, out.reason) == ("needs_review", "competing_candidate")


def test_candidates_from_different_sources_are_not_rivals() -> None:
    a, b = scored(9.0, source="court", source_id="a"), scored(9.0, source="fec", source_id="b")
    assert dec.decide([a, b], confirm=0.99, review=0.90).outcome == "confirmed"


def test_a_weak_court_record_is_never_confirmed_on_its_own() -> None:
    out = dec.decide([scored(20.0, weak=True)], confirm=0.99, review=0.90)
    assert (out.outcome, out.reason) == ("needs_review", "weak_record")


# ---- what the gates read ---------------------------------------------------------------------


def decision(conn: sqlite3.Connection, row_id: int, kind: str, outcome: str, *, stage: str | None = "identity",
             batch: str = "main") -> None:  # fmt: skip
    conn.execute(
        "INSERT INTO decisions (row_id, kind, outcome, decided_by, decided_at, stage, batch) "
        "VALUES (?,?,?,?,?,?,?)",
        (row_id, kind, outcome, "t", STAMP, stage, batch),
    )


def test_the_gate_reads_the_latest_main_batch_decision(conn: sqlite3.Connection) -> None:
    insert_row(conn)
    decision(conn, 1, "identity", "unresolved")
    assert runner._identity_outcome(conn, 1) == "unresolved"
    decision(conn, 1, "review", "confirmed")  # the operator's later decision wins
    assert runner._identity_outcome(conn, 1) == "confirmed"
    decision(conn, 1, "identity", "unresolved", batch="sensitivity")  # never read
    assert runner._identity_outcome(conn, 1) == "confirmed"
    decision(conn, 1, "review", "label=match")  # a label is not a decision
    decision(conn, 1, "review", "audit_correct")
    assert runner._identity_outcome(conn, 1) == "confirmed"
    decision(conn, 1, "review", "audit_wrong")  # a wrong audit verdict withdraws the confirmation
    assert runner._identity_outcome(conn, 1) == "unresolved"
    decision(conn, 1, "review", "confirmed", stage="code")  # another stage's review is not read
    assert runner._identity_outcome(conn, 1) == "unresolved"


def test_a_row_with_no_decision_has_no_outcome(conn: sqlite3.Connection) -> None:
    insert_row(conn)
    assert runner._identity_outcome(conn, 1) is None


def test_decision_rationale_is_json_with_the_reason(conn: sqlite3.Connection) -> None:
    from efp.identity import store

    insert_row(conn)
    best = scored(7.5)
    best.candidate_id = None
    out = dec.decide([best], confirm=0.99, review=0.90)
    store.write_decision(conn, 1, out, decided_by="x", thresholds=(0.99, 0.90))
    raw = conn.execute("SELECT rationale, stage, batch FROM decisions").fetchone()
    body = json.loads(raw[0])
    assert (
        body["reason"] == "at_or_above_confirm_threshold" and body["thresholds"]["review"] == 0.90
    )
    assert raw[1:] == ("identity", "main")

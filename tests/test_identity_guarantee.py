"""The property the design exists to guarantee (spec section 8): a candidate that agrees only on
name, state and year has evidence zero and a posterior equal to the prior, which for a common name
is far below the review threshold. Never a match on name, state and year alone.

The ten surnames are synthetic stand-ins for the ten most common US surnames, at proportions of the
same order as the Census 2010 file (the top one is Smith's published 828.19 per 100,000)."""

from __future__ import annotations

import sqlite3

import pytest

from efp.identity import decide as dec
from efp.identity import model, params
from efp.identity.pairs import PairBuilder
from efp.identity.prior import PriorModel
from efp.identity.profile import Person
from tests.identity_helpers import seed_ref_source, seed_reference

TOP_TEN = {
    "SURNAMEA": 828.19, "SURNAMEB": 655.0, "SURNAMEC": 566.0, "SURNAMED": 521.0, "SURNAMEE": 504.0,
    "SURNAMEF": 429.0, "SURNAMEG": 421.0, "SURNAMEH": 400.0, "SURNAMEI": 387.0, "SURNAMEJ": 373.0,
}  # fmt: skip
REVIEW = 0.90


def candidate_posterior(
    prior_model: PriorModel, conn: sqlite3.Connection, surname: str, given: str
) -> float:
    left = Person("row", "1", given, "", surname.title(), state="CT")
    right = Person("court", "x", given, "", surname.title(), state="CT")
    pair = PairBuilder(conn, prior_model).build(1, left, right, 2018)
    tf = {
        "surname": model.term_frequencies([surname] * 3 + ["OTHER"] * 97),
        "given_name": model.term_frequencies([given.upper()] * 3 + ["OTHER"] * 97),
    }
    water = model.score(
        [{"pair_id": 1, "left": pair.left, "right": pair.right}], params.load_published(), tf=tf
    )[1]
    assert water["evidence_bits"] <= 0.0  # nothing but the name: no positive evidence at all
    scored = dec.score_candidate(right, pair, water)
    assert dec.decide([scored], confirm=0.99, review=REVIEW).outcome == "unresolved"
    return scored.posterior


@pytest.mark.parametrize("given", ["John", "Mary", "Alex"])
def test_name_state_year_only_never_reaches_the_review_threshold_with_census_tables(
    conn: sqlite3.Connection, given: str
) -> None:
    seed_reference(conn)
    load = seed_ref_source(conn, "census_surnames", notes={"floor_prop100k": 0.25})
    for rank, (name, prop) in enumerate(TOP_TEN.items(), 1):
        conn.execute(
            "INSERT INTO ref_surname_freq VALUES (?,?,?,?,?)",
            (load, name, rank, int(prop * 1000), prop),
        )
    prior_model = PriorModel(conn)
    for surname in TOP_TEN:
        assert candidate_posterior(prior_model, conn, surname, given) < REVIEW / 100  # far below


def test_name_state_year_only_never_reaches_the_review_threshold_with_the_ceiling_prior(
    conn: sqlite3.Connection,
) -> None:
    prior_model = PriorModel(conn)  # no reference tables at all
    for surname in TOP_TEN:
        assert candidate_posterior(prior_model, conn, surname, "John") < 0.001


def test_even_a_unique_name_is_capped_by_the_namesake_floor(conn: sqlite3.Connection) -> None:
    """N_namesakes is at least one, so a prior never exceeds one half and a bare name cannot reach
    the review threshold however rare it is."""
    seed_reference(conn)
    posterior = candidate_posterior(PriorModel(conn), conn, "Qzxqzxqzx", "Zelda")
    assert posterior <= 0.5 < REVIEW


def test_the_structural_guard_stops_a_posterior_with_no_corroboration() -> None:
    """If the arithmetic ever said yes on a name alone, the rule still says no."""
    from efp.identity.pairs import Pair
    from efp.identity.prior import Prior

    person = Person("court", "x", "John", "", "Smith")
    pair = Pair(1, {}, {}, Prior(1.0001, {}))  # prior odds ~1 and a huge unexplained weight
    water = {
        "evidence_bits": 30.0, "prior_odds": 1.0, "posterior": 1.0,
        "steps": [{"comparison": "surname", "level": "exact"}, {"comparison": "middle", "level": "null"}],
    }  # fmt: skip
    scored = dec.score_candidate(person, pair, water)
    assert scored.posterior > 0.99 and scored.agreeing == []
    out = dec.decide([scored], confirm=0.99, review=0.90)
    assert (out.outcome, out.reason) == ("unresolved", "no_corroborating_evidence")

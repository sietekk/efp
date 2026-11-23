"""The population prior against hand-computed values (synthetic reference tables, see
``tests/identity_helpers.seed_reference`` for the arithmetic)."""

from __future__ import annotations

import sqlite3

import pytest

from efp.identity.prior import (
    CEILING_N_ADULTS,
    CEILING_P_GIVEN,
    CEILING_P_SURNAME,
    PriorModel,
)
from tests.identity_helpers import seed_reference


@pytest.fixture
def model(conn: sqlite3.Connection) -> PriorModel:
    seed_reference(conn)
    return PriorModel(conn)


def test_cohort_free_prior_over_all_adults(model: PriorModel) -> None:
    prior = model.prior(
        surname="Commonsurname", given="John", state="CT", birth_lo=None, birth_hi=None
    )
    # p(surname) 800/100k, p(John | male, all years) 5%, adults: 0.4M + 13M + 1M = 14.4M
    assert prior.n_namesakes == pytest.approx(0.008 * 0.05 * 14_400_000)
    assert prior.components["cohort_free"] is True
    assert prior.mode == "census"


def test_exact_birth_year_prior(model: PriorModel) -> None:
    prior = model.prior(
        surname="Commonsurname", given="John", state="CT", birth_lo=1970, birth_hi=1970
    )
    # aged 53 in the 2023 vintage: one age of the 50-54 band (200,000)
    assert prior.n_namesakes == pytest.approx(0.008 * 0.05 * 200_000)
    assert prior.components["cohort_free"] is False


def test_age_only_widens_the_cohort_by_five_years(model: PriorModel) -> None:
    # age 52 in 2018 -> born 1965-66, widened to 1960-71 (twelve birth years, ages 52-63 in 2023):
    # 3/5 of 50-54 + all of 55-59 + 4/5 of 60-64 = 600,000 + 1,000,000 + 800,000
    prior = model.prior(
        surname="Commonsurname", given="John", state="CT", birth_lo=1965, birth_hi=1966, widen=5
    )
    assert prior.components["p_given_info"]["cohort"] == [1960, 1971]
    assert prior.n_namesakes == pytest.approx(0.008 * 0.05 * 2_400_000)


def test_surname_absent_from_the_file_takes_the_recorded_floor(model: PriorModel) -> None:
    prior = model.prior(
        surname="Nosuchname", given="John", state="CT", birth_lo=None, birth_hi=None
    )
    assert prior.components["p_surname_how"] == "floor"
    assert prior.components["p_surname"] == pytest.approx(0.25 / 100_000)


def test_the_floor_hits_are_visible(model: PriorModel) -> None:
    listed = model.prior(
        surname="Rare Surname", given="John", state="CT", birth_lo=None, birth_hi=None
    )
    assert listed.components["p_surname_how"] == "listed"


def test_ambiguous_given_name_pools_both_sexes(model: PriorModel) -> None:
    prior = model.prior(
        surname="Commonsurname", given="Alex", state="CT", birth_lo=None, birth_hi=None
    )
    info = prior.components["p_given_info"]
    assert info["ambiguous"] is True and info["sex"] == "F/M"
    # (10,000 + 10,000) / (1,000,000 + 1,000,000)
    assert info["how"] == "national"
    assert prior.components["p_given"] == pytest.approx(0.01)


def test_sex_is_the_names_majority_sex(model: PriorModel) -> None:
    mary = model.prior(
        surname="Commonsurname", given="Mary", state="CT", birth_lo=None, birth_hi=None
    )
    assert mary.components["p_given_info"]["sex"] == "F"
    assert mary.components["p_given"] == pytest.approx(0.04)


def test_n_namesakes_is_never_below_one(model: PriorModel) -> None:
    prior = model.prior(
        surname="Nosuchname", given="Zelda", state="CT", birth_lo=1970, birth_hi=1970
    )
    assert prior.components["raw_namesakes"] < 1
    assert prior.components["floored_at_one"] is True
    assert prior.n_namesakes == 1.0
    assert prior.odds == 1.0


def test_by_state_file_is_used_only_above_the_birth_threshold(conn: sqlite3.Connection) -> None:
    seed_reference(conn)
    ssa = conn.execute("SELECT load_id FROM ref_sources WHERE dataset = 'ssa_names'").fetchone()[0]
    for year in range(1900, 2011):
        for sex, count in (("M", 100), ("F", 100)):
            conn.execute(
                "INSERT INTO ref_given_freq VALUES (?,?,?,?,?,?)",
                (ssa, "Filler", sex, year, "CT", 1000),
            )
            conn.execute(
                "INSERT INTO ref_given_freq VALUES (?,?,?,?,?,?)",
                (ssa, "John", sex, year, "CT", count),
            )
    low = PriorModel(conn, ssa_state_min_births=10**9).p_given("John", "CT", 1970, 1970)
    assert low[1]["how"] == "national"
    high = PriorModel(conn, ssa_state_min_births=1).p_given("John", "CT", 1970, 1970)
    assert high[1]["how"] == "state"
    assert high[0] == pytest.approx(100 / 1100)


def test_empty_reference_tables_give_the_ceiling_prior(conn: sqlite3.Connection) -> None:
    prior = PriorModel(conn).prior(
        surname="Anything", given="Anyone", state="CT", birth_lo=None, birth_hi=None
    )
    assert prior.mode == "ceiling"
    assert prior.components["ceiling"] is True
    assert prior.components["raw_namesakes"] == pytest.approx(
        CEILING_P_SURNAME * CEILING_P_GIVEN * CEILING_N_ADULTS
    )
    assert prior.n_namesakes > 10_000  # a ceiling: no name is treated as rarer than that


def test_an_initial_stands_for_every_name_that_starts_with_it(model: PriorModel) -> None:
    """'J. Commonsurname' must not look like a unique person: the initial J covers John."""
    initial = model.prior(
        surname="Commonsurname", given="J", state="CT", birth_lo=None, birth_hi=None
    )
    assert initial.components["p_given_info"]["how"] == "initial"
    # John is 50,000 of 2,000,000 births a year in the synthetic table: J is at least that share
    assert initial.components["p_given"] >= 0.025
    assert initial.n_namesakes >= 0.008 * 0.025 * 14_400_000


def test_no_given_name_says_nothing(model: PriorModel) -> None:
    prior = model.prior(surname="Commonsurname", given="", state="CT", birth_lo=None, birth_hi=None)
    assert prior.components["p_given"] == 1.0
    assert prior.n_namesakes == pytest.approx(0.008 * 14_400_000)


def test_an_unlisted_name_is_bounded_by_the_cohorts_births_not_all_years(model: PriorModel) -> None:
    prior = model.prior(
        surname="Commonsurname", given="Xqzyv", state="CT", birth_lo=1970, birth_hi=1970
    )
    info = prior.components["p_given_info"]
    assert info["how"] == "unlisted" and info["cohort"] == [1970, 1970]
    assert prior.components["p_given"] == pytest.approx(4 * 2 * 1 / 2_000_000)

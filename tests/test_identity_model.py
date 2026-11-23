"""Each comparison's levels on synthetic pairs, through Splink with the published priors."""

from __future__ import annotations

from typing import Any

import pytest

from efp.identity import model, params
from efp.identity.normalize import load_nicknames, metaphone_codes


@pytest.fixture(scope="module")
def settings() -> model.Settings:
    return params.load_published()


def person(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "surname": "SMITH", "surname_dm": metaphone_codes("SMITH"), "given_name": "JOHN",
        "given_forms": load_nicknames().forms("JOHN"), "middle": None, "dob": None,
        "by_lo": None, "by_hi": None, "city": None, "counties": [], "state": "CT", "case_no": None,
    }  # fmt: skip
    base.update(overrides)
    return base


def weights(
    settings: model.Settings, left: dict[str, Any], right: dict[str, Any]
) -> dict[str, Any]:
    tf = {
        "surname": model.term_frequencies(["SMITH"] * 5 + ["JONES"] * 95),
        "given_name": model.term_frequencies(["JOHN"] * 5 + ["MARY"] * 95),
    }
    out = model.score([{"pair_id": 1, "left": left, "right": right}], settings, tf=tf)[1]
    return {s["comparison"]: s for s in out["steps"]} | {"_bits": out["evidence_bits"]}


def test_a_pair_that_agrees_only_on_the_name_has_no_evidence(settings: model.Settings) -> None:
    w = weights(settings, person(), person())
    assert w["surname"]["level"] == "exact" and w["given_name"]["level"] == "exact"
    assert w["surname"]["bayes_factor"] == pytest.approx(1.0)
    assert w["given_name"]["bayes_factor"] == pytest.approx(1.0)
    assert w["_bits"] == pytest.approx(0.0, abs=1e-9)
    assert all(w[c]["level"] == "null" for c in ("middle", "dob", "city", "case_number"))


@pytest.mark.parametrize(
    ("right", "level"),
    [
        (person(surname="SMYTH", surname_dm=metaphone_codes("SMYTH")), "double_metaphone"),
        (person(surname="SMITHE"), "jaro_winkler"),
        (person(surname="BROWN", surname_dm=metaphone_codes("BROWN")), "else"),
    ],
)
def test_surname_levels(settings: model.Settings, right: dict[str, Any], level: str) -> None:
    assert weights(settings, person(), right)["surname"]["level"] == level


@pytest.mark.parametrize(
    ("right", "level"),
    [
        (person(given_name="JOHN"), "exact"),
        (person(given_name="JACK", given_forms=load_nicknames().forms("JACK")), "nickname"),
        (person(given_name="JOHNN", given_forms=["JOHNN"]), "jaro_winkler"),
        (person(given_name="ZEBEDIAH", given_forms=["ZEBEDIAH"]), "else"),
    ],
)
def test_given_name_levels(settings: model.Settings, right: dict[str, Any], level: str) -> None:
    assert weights(settings, person(), right)["given_name"]["level"] == level


def test_approximate_name_agreement_is_weaker_than_exact(settings: model.Settings) -> None:
    nick = weights(settings, person(), person(given_name="JACK", given_forms=["JACK", "JOHN"]))
    assert nick["given_name"]["log2_weight"] < 0


@pytest.mark.parametrize(
    ("left", "right", "level"),
    [
        ("QUINCY", "QUINCY", "exact"),
        ("Q", "QUINCY", "initial"),
        ("QUINCY", "Q", "initial"),
        ("Q", "Q", "initial"),
        ("QUINCY", "ADAMS", "conflict"),
        ("Q", "ADAMS", "conflict"),
        (None, "ADAMS", "null"),
        ("QUINCY", None, "null"),
    ],
)
def test_middle_levels(
    settings: model.Settings, left: str | None, right: str | None, level: str
) -> None:
    assert weights(settings, person(middle=left), person(middle=right))["middle"]["level"] == level


def test_an_absent_middle_name_is_not_evidence_against(settings: model.Settings) -> None:
    w = weights(settings, person(middle="QUINCY"), person(middle=None))
    assert w["middle"]["bayes_factor"] == 1.0 and w["middle"]["log2_weight"] == 0.0


def test_a_recorded_middle_name_that_disagrees_is_negative(settings: model.Settings) -> None:
    assert (
        weights(settings, person(middle="QUINCY"), person(middle="ADAMS"))["middle"]["log2_weight"]
        < -2
    )


@pytest.mark.parametrize(
    ("left", "right", "level"),
    [
        (
            {"dob": "1970-05-05", "by_lo": 1970, "by_hi": 1970},
            {"dob": "1970-05-05", "by_lo": 1970, "by_hi": 1970},
            "exact",
        ),
        ({"by_lo": 1965, "by_hi": 1966}, {"by_lo": 1966, "by_hi": 1967}, "within_1"),
        ({"by_lo": 1965, "by_hi": 1966}, {"by_lo": 1969, "by_hi": 1970}, "within_3"),
        ({"by_lo": 1965, "by_hi": 1966}, {"by_lo": 1980, "by_hi": 1981}, "conflict"),
        (
            {"dob": "1970-05-05", "by_lo": 1970, "by_hi": 1970},
            {"dob": "1970-06-06", "by_lo": 1970, "by_hi": 1970},
            "conflict",
        ),
        (
            {"dob": "1970-05-05", "by_lo": 1970, "by_hi": 1970},
            {"by_lo": 1969, "by_hi": 1970},
            "within_1",
        ),
        ({}, {"by_lo": 1980, "by_hi": 1981}, "null"),
    ],
)
def test_dob_and_age_levels(settings: model.Settings, left: dict, right: dict, level: str) -> None:
    assert weights(settings, person(**left), person(**right))["dob"]["level"] == level


def test_a_date_of_birth_conflict_is_strongly_negative(settings: model.Settings) -> None:
    w = weights(settings, person(by_lo=1965, by_hi=1966), person(by_lo=1990, by_hi=1991))
    assert w["dob"]["log2_weight"] < -4
    assert w["_bits"] < -4


@pytest.mark.parametrize(
    ("left", "right", "level"),
    [
        ({"city": "HARTFORD"}, {"city": "HARTFORD"}, "exact"),
        (
            {"city": "HARTFORD", "counties": ["HARTFORD"]},
            {"city": "BLOOMFIELD", "counties": ["HARTFORD"]},
            "same_county",
        ),
        (
            {"city": "HARTFORD", "counties": ["HARTFORD"]},
            {"city": "STAMFORD", "counties": ["FAIRFIELD"]},
            "same_state",
        ),
        ({"city": "HARTFORD"}, {"city": "TAMPA", "state": "FL"}, "conflict"),
        ({"city": "HARTFORD"}, {}, "null"),
    ],
)
def test_city_levels(settings: model.Settings, left: dict, right: dict, level: str) -> None:
    assert weights(settings, person(**left), person(**right))["city"]["level"] == level


def test_case_number_exact_is_effectively_decisive(settings: model.Settings) -> None:
    w = weights(settings, person(case_no="2019-CR-123"), person(case_no="2019-CR-123"))
    assert w["case_number"]["level"] == "exact"
    assert w["case_number"]["log2_weight"] > 18  # a factor of about 600,000
    other = weights(settings, person(case_no="2019-CR-123"), person(case_no="2015-CR-9"))
    assert other["case_number"]["level"] == "else" and other["case_number"]["log2_weight"] < 0


def test_term_frequency_adjustment_never_raises_a_weight(settings: model.Settings) -> None:
    """A rare surname in the workbook would get a boost from Splink's adjustment: it is capped,
    because rarity is already in the population prior."""
    tf = {
        "surname": model.term_frequencies(["RARENAME"] + ["JONES"] * 999),
        "given_name": model.term_frequencies(["JOHN"] * 5 + ["MARY"] * 95),
    }
    pair = [{"pair_id": 1, "left": person(surname="RARENAME"), "right": person(surname="RARENAME")}]
    capped = model.score(pair, settings, tf=tf, cap=True)[1]["steps"][0]
    free = model.score(pair, settings, tf=tf, cap=False)[1]["steps"][0]
    assert free["tf_adjustment_raw"] > 1  # Splink would boost it
    assert capped["tf_adjustment_used"] == 1.0 and capped["log2_weight"] == pytest.approx(0.0)


def test_a_common_name_in_the_workbook_may_lower_the_weight(settings: model.Settings) -> None:
    tf = {
        "surname": model.term_frequencies(["SMITH"] * 99 + ["JONES"]),
        "given_name": model.term_frequencies(["JOHN"] * 5 + ["MARY"] * 95),
    }
    step = model.score([{"pair_id": 1, "left": person(), "right": person()}], settings, tf=tf)[1][
        "steps"
    ][0]
    assert step["tf_adjustment_raw"] < 1 and step["log2_weight"] <= 0

from __future__ import annotations

import pytest

from efp.court.classify import classify
from efp.court.identifiers import extract_identifiers
from efp.court.names import match_name, parse_name


def _kinds(text: str, name: str = "Alex Example") -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for item in extract_identifiers(text, parse_name(name)):
        assert item.span in text  # every span is verbatim text from the document
        found.setdefault(item.kind, []).append(item.value)
    return found


# --- names --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "given", "middle", "surname", "suffix"),
    [
        ("Alex Example", "Alex", "", "Example", ""),
        ("Alex Q. Example Jr.", "Alex", "Q", "Example", "Jr"),
        ("Example, Alex", "Alex", "", "Example", ""),
        ("Example, Alex Q.", "Alex", "Q", "Example", ""),
        ("Maria de la Cruz", "Maria", "", "de la Cruz", ""),
        ("Dr. Alex Example", "Alex", "", "Example", ""),
        ("Example", "", "", "Example", ""),
    ],
)
def test_names_are_parsed(raw: str, given: str, middle: str, surname: str, suffix: str) -> None:
    parts = parse_name(raw)
    assert (parts.given, parts.middle, parts.surname, parts.suffix) == (
        given,
        middle,
        surname,
        suffix,
    )


def test_aka_and_nicknames_become_aliases() -> None:
    parts = parse_name('Alexander "Al" Example aka Alex Sample')
    assert parts.nicknames == ("Al",) and [a.full for a in parts.aliases] == ["Alex Sample"]
    assert match_name(parts, "Al Example was charged") is not None
    assert match_name(parts, "Alex Sample was charged") is not None


@pytest.mark.parametrize(
    ("text", "full"),
    [
        ("Alex Example, 47, of Springfield", True),
        ("Alex Q. Example pleaded guilty", True),
        ("Example, Alex was sentenced", True),
        ("A. Example was charged", True),  # an initial and the surname
        ("Mr. Example said", False),  # a surname alone is not enough
        ("The Examples were present", False),  # a different word
        ("Alexa Example pleaded", False),  # a different given name
    ],
)
def test_a_name_match_needs_the_surname_and_a_given_name_or_initial(text: str, full: bool) -> None:
    found = match_name(parse_name("Alex Example"), text)
    assert bool(found and found.full) is full


# --- identifiers -------------------------------------------------------------------------------


def test_age_and_city_in_the_common_press_release_form() -> None:
    found = _kinds("Alex Example, 47, of Springfield, Ohio, pleaded guilty.")
    assert found["age"] == ["47"] and found["city"] == ["Springfield"]


def test_one_defendants_age_is_not_given_to_another() -> None:
    text = (
        "Alex Example, 47, of Springfield, pleaded guilty. Bob Other, 33, of Dayton, was sentenced."
    )
    found = _kinds(text)
    assert found["age"] == ["47"] and found["city"] == ["Springfield"]
    assert "33" not in found.get("age", [])


@pytest.mark.parametrize(
    "case_number",
    ["1:19-cr-00123", "3:13-cr-00045-JAM", "No. 19-CR-123", "Case No. 19-cr-00123-ABC"],
)
def test_case_number_formats(case_number: str) -> None:
    found = _kinds(
        f"Alex Example was indicted. The case is {case_number}, in the District of Ohio."
    )
    assert found.get("case_number"), case_number


def test_dob_office_employer_party_and_alias() -> None:
    text = (
        'Alex Example, a former county clerk employed by the Lake County Board of Elections, also known as "Lex", '
        "was a Republican candidate. Example was born on March 3, 1972."
    )
    found = _kinds(text)
    assert found["dob"] == ["March 3, 1972"] and found["office"] == ["county clerk"]
    assert found["employer"] == ["Lake County Board of Elections"]
    assert found["party_on_ballot"] == ["Republican"] and found["alias"] == ["Lex"]


def test_a_year_old_form_gives_an_age() -> None:
    assert _kinds("Alex Example, a 52-year-old election judge, admitted the scheme.")["age"] == [
        "52"
    ]


def test_nothing_is_invented_from_text_that_does_not_mention_the_subject() -> None:
    found = _kinds("Bob Other, 33, of Dayton, a former mayor, was sentenced.", name="Alex Example")
    assert "age" not in found and "city" not in found and "office" not in found


def test_a_text_with_no_name_is_read_whole() -> None:
    text = "Docket: 1:19-cr-00123. The defendant, 41, of Akron, pleaded."
    found = {i.kind for i in extract_identifiers(text, None)}
    assert {"case_number", "age"} <= found


# --- classification ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("details", "hosts", "kind"),
    [
        ("Charged by the U.S. Attorney's Office", [], "federal"),
        ("", ["www.justice.gov", "news.test"], "federal"),
        ("", ["courtlistener.com"], "federal"),
        ("Convicted in U.S. District Court", [], "federal"),
        ("Charged under 52 U.S.C. 20511", [], "federal"),
        ("Pleaded guilty in Hartford Superior Court", [], "state"),
        ("Prosecuted by the District Attorney", [], "state"),
        ("", ["ag.state.mn.us"], "state"),
        ("Voted twice", [], "unknown"),
        ("", ["news.test"], "unknown"),
    ],
)
def test_federal_state_or_unknown(details: str, hosts: list[str], kind: str) -> None:
    result = classify(details, None, hosts)
    assert result.kind == kind and result.basis
    assert result.federal_strategies_apply is (kind != "state")


def test_a_row_with_both_signals_is_federal_and_keeps_the_state_basis() -> None:
    result = classify("U.S. Attorney and the District Attorney both charged him", None, [])
    assert result.kind == "federal" and any("state" in b for b in result.basis)

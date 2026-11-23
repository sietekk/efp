"""Names, nicknames, dates, cities and case numbers, and the places loader."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from efp.config import Config
from efp.identity import normalize as n
from efp.reference import places
from efp.reference.base import LayoutError
from efp.runner import make_context


def test_the_nickname_table_is_versioned_and_hashed() -> None:
    table = n.load_nicknames()
    assert table.version == "1"
    assert table.sha256 == hashlib.sha256(n.NICKNAMES_PATH.read_bytes()).hexdigest()
    assert len(n.NICKNAMES_PATH.read_text().splitlines()) > 400  # curated, not a stub


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("Jack", "John", True), ("john", "JACK", True), ("Bill", "William", True),
        ("Liz", "Elizabeth", True), ("Peggy", "Margaret", True), ("Bob", "Robert", True),
        ("Al", "Fred", False),  # both are short for Alfred, and are not each other
        ("John", "James", False), ("Mary", "Marie", False), ("", "John", False),
    ],
)  # fmt: skip
def test_nickname_match(a: str, b: str, expected: bool) -> None:
    assert n.load_nicknames().match(a, b) is expected


def test_initials() -> None:
    assert (
        n.initial_match("Q", "Quincy")
        and n.initial_match("Quincy", "Q")
        and n.initial_match("Q", "Q")
    )
    assert not n.initial_match("Q", "Adams") and not n.initial_match("Quincy", "Quentin")
    assert not n.initial_match("", "Q")


def test_double_metaphone_codes() -> None:
    assert set(n.metaphone_codes("Smith")) & set(n.metaphone_codes("Smyth"))
    assert not set(n.metaphone_codes("Smith")) & set(n.metaphone_codes("Jones"))
    assert n.metaphone_codes("") == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("March 3, 1975", date(1975, 3, 3)), ("Mar. 3, 1975", date(1975, 3, 3)),
        ("3/3/1975", date(1975, 3, 3)), ("3/3/75", date(1975, 3, 3)), ("1/2/05", date(2005, 1, 2)),
        ("13/40/1975", None), ("Smarch 3, 1975", None), ("March 3, 1850", None), (None, None),
    ],
)  # fmt: skip
def test_parse_dob(text: str | None, expected: date | None) -> None:
    assert n.parse_dob(text, today=date(2026, 11, 23)) == expected


def test_birth_interval() -> None:
    assert n.birth_interval(dob=date(1970, 5, 5), age=None, ref_year=2018) == (1970, 1970)
    assert n.birth_interval(dob=None, age=52, ref_year=2018) == (1965, 1966)
    assert n.birth_interval(dob=None, age=None, ref_year=2018) == (None, None)
    assert n.birth_interval(dob=None, age=200, ref_year=2018) == (None, None)


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("1:19-cr-00123-ABC-1", "2019-CR-123"), ("19CR123", "2019-CR-123"),
        ("2:2019-cr-123", "2019-CR-123"), ("3:99-cr-45", "1999-CR-45"), ("", ""),
        ("Docket #A-77/b", "DOCKETAB"[:0] or "DOCKETA77B"),
    ],
)  # fmt: skip
def test_case_key(text: str, key: str) -> None:
    assert n.case_key(text) == key


@pytest.mark.parametrize(
    ("text", "key"),
    [("Springfield", "SPRINGFIELD"), ("Springfield, Ill.", "SPRINGFIELD"), ("Tampa city", "TAMPA"),
     ("123 Main St, Tampa, FL 33601", "TAMPA"), ("St. Louis", "STLOUIS"), ("", "")],
)  # fmt: skip
def test_place_key_uses_usaddress_for_address_strings(text: str, key: str) -> None:
    assert n.place_key(text) == key


@pytest.mark.parametrize(
    "name",
    [
        "Springfield city",
        "Hartford",
        "Nashville-Davidson metropolitan government (balance)",
        "West Hartford town",
    ],
)
def test_place_keys_agree_with_the_reference_loader(name: str) -> None:
    """Components do not import each other, so the two copies of the rule are tested together."""
    assert n.place_key(name) == places.place_key(name)


def test_places_loader_reads_the_codes_file_and_rejects_a_foreign_layout(
    config: Config, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    from efp.reference.files import Part

    ctx = make_context(config, conn)
    conn.execute(
        "INSERT INTO ref_sources (load_id, dataset, status, current, loaded_at) VALUES (1, 'census_place_county', 'loaded', 1, 's')"
    )
    good = tmp_path / "place_county.txt"
    good.write_text(
        "STATE|STATEFP|COUNTYFP|COUNTYNAME|PLACEFP|PLACENS|PLACENAME|TYPE|CLASSFP|FUNCSTAT\n"
        "CT|09|003|Hartford County|37070|1|Hartford city|city|C1|A\n"
        "CT|09|003|Hartford County|08000|2|Bloomfield town|town|C1|A\n"
        "CT|09|005|Litchfield County|08000|2|Bloomfield town|town|C1|A\n"
        "CT|09|003|Hartford County||0||county|||\n",
        encoding="utf-8",
    )
    dataset = places.CensusPlaceCounty()
    part = Part("", "u", "place_county.txt", "text")
    counter = dataset.read(ctx, part, good, 1)
    assert counter.rows == 3
    got = conn.execute(
        "SELECT state, place_key, county_key FROM ref_place_county ORDER BY 2, 3"
    ).fetchall()
    assert got == [
        ("CT", "BLOOMFIELD", "HARTFORD"),
        ("CT", "BLOOMFIELD", "LITCHFIELD"),
        ("CT", "HARTFORD", "HARTFORD"),
    ]
    bad = tmp_path / "other.txt"
    bad.write_text("a|b\n1|2\n")
    with pytest.raises(LayoutError):
        dataset.read(ctx, part, bad, 1)

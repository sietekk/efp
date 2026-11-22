from __future__ import annotations

import datetime as dt
import hashlib

import pytest

from efp.rowhash import CellError, canonical_cell, row_sha256

NINE = [2016, "OH", "Alex Example", "Criminal Conviction", "Duplicate Voting", "d", "s", None, None]


def test_the_rule_is_what_docs_row_hash_says() -> None:
    """Recompute by hand from the documented rule, independent of the implementation."""
    expected = "\x1f".join(
        [
            "\x1eint:2016",
            "OH",
            "Alex Example",
            "Criminal Conviction",
            "Duplicate Voting",
            "d",
            "s",
            "",
            "",
        ]
    )
    assert row_sha256(NINE) == hashlib.sha256(expected.encode("utf-8")).hexdigest()


def test_none_and_empty_string_hash_the_same() -> None:
    a = [*NINE[:7], None, None]
    b = [*NINE[:7], "", ""]
    assert row_sha256(a) == row_sha256(b)


def test_number_and_text_cells_hash_differently() -> None:
    as_number = row_sha256([2016, *NINE[1:]])
    as_text = row_sha256(["2016", *NINE[1:]])
    as_float = row_sha256([2016.0, *NINE[1:]])
    assert len({as_number, as_text, as_float}) == 3


@pytest.mark.parametrize(
    "changed",
    [
        "Alex Example ",
        " Alex Example",
        "Alex  Example",
        "alex example",
        "Alex" + chr(0xA0) + "Example",
    ],
)
def test_any_whitespace_or_case_change_changes_the_hash(changed: str) -> None:
    base = row_sha256(NINE)
    assert row_sha256([NINE[0], NINE[1], changed, *NINE[3:]]) != base


def test_the_hash_is_deterministic_and_position_sensitive() -> None:
    assert row_sha256(NINE) == row_sha256(list(NINE))
    swapped = [NINE[0], NINE[2], NINE[1], *NINE[3:]]
    assert row_sha256(swapped) != row_sha256(NINE)


def test_cells_cannot_smuggle_a_boundary() -> None:
    """Without the ban on separators, ('a\\x1fb', 'c') and ('a', 'b\\x1fc') would collide."""
    for bad in ("a\x1fb", "a\x1eb"):
        with pytest.raises(CellError, match="reserved"):
            canonical_cell(bad)


def test_other_cell_types() -> None:
    assert canonical_cell(True) == "\x1ebool:True"
    assert canonical_cell(3) != canonical_cell(True)
    assert canonical_cell(dt.datetime(2020, 1, 2, 3, 4, 5)) == "\x1edatetime:2020-01-02T03:04:05"
    assert canonical_cell(dt.date(2020, 1, 2)) == "\x1edate:2020-01-02"
    assert canonical_cell(dt.time(3, 4)) == "\x1etime:03:04:00"
    with pytest.raises(CellError, match="unsupported"):
        canonical_cell([1, 2])
    with pytest.raises(CellError, match="unsupported"):
        canonical_cell({"a": 1})


def test_a_row_needs_exactly_nine_cells() -> None:
    with pytest.raises(CellError, match="9 source cells"):
        row_sha256(NINE[:8])
    with pytest.raises(CellError, match="9 source cells"):
        row_sha256([*NINE, None])


def test_lone_surrogates_are_rejected() -> None:
    with pytest.raises(CellError, match="Unicode"):
        row_sha256(["\ud800", *NINE[1:]])

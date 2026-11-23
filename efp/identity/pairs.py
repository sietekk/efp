"""From two people to one comparison record for the model, plus the population prior.

The independence rule is applied here: a value one side read from a document the other side also
read from is dropped for this pair (and listed under ``masked``), because two readings of the same
document are one observation, not two.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from efp.identity.normalize import (
    birth_interval,
    case_key,
    letters,
    load_nicknames,
    metaphone_codes,
    parse_dob,
    place_key,
    place_keys,
)
from efp.identity.prior import Prior, PriorModel
from efp.identity.profile import Person, Value

AGE_WIDEN = 5  # spec section 8: +-5 birth years when only an age is known


@dataclass
class Pair:
    pair_id: int
    left: dict[str, Any]
    right: dict[str, Any]
    prior: Prior
    used: dict[str, dict[str, Any]] = field(default_factory=dict)
    masked: list[dict[str, Any]] = field(default_factory=list)
    prior_inputs: dict[str, Any] = field(default_factory=dict)  # to recompute the prior on a refit
    age_boost_bits: float = 0.0  # what the prior's cohort already took from the age evidence
    flags: list[str] = field(default_factory=list)  # e.g. suffix_conflict


class Counties:
    """Place -> counties from ``ref_place_county_current`` (empty until the file is loaded)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._cache: dict[tuple[str, str], list[str]] = {}
        self.loaded = (
            conn.execute("SELECT 1 FROM ref_place_county_current LIMIT 1").fetchone() is not None
        )

    def of(self, state: str | None, places: list[str]) -> list[str]:
        """The counties of the first key (``place_keys`` order) the table knows."""
        if not self.loaded or not state:
            return []
        for place in places:
            key = (state, place)
            if key not in self._cache:
                self._cache[key] = [
                    str(r[0])
                    for r in self.conn.execute(
                        "SELECT county_key FROM ref_place_county_current "
                        "WHERE state = ? AND place_key = ?",
                        key,
                    )
                ]
            if self._cache[key]:
                return self._cache[key]
        return []


def _middle(person: Person) -> str | None:
    word = (person.middle or "").split()
    return letters(word[0]) or None if word else None


def _int(value: Value | None) -> int | None:
    if value is None:
        return None
    digits = "".join(ch for ch in value.value if ch.isdigit())
    return int(digits) if digits else None


class PairBuilder:
    def __init__(self, conn: sqlite3.Connection, prior: PriorModel) -> None:
        self.conn = conn
        self.prior_model = prior
        self.nicknames = load_nicknames()
        self.counties = Counties(conn)

    def _side(
        self, person: Person, other: Person, ref_year: int | None, masked: list[dict[str, Any]],
        used: dict[str, Any], side: str,
    ) -> dict[str, Any]:  # fmt: skip
        exclude = other.docs
        for kind in ("dob", "age", "city", "case_number"):
            for seen in person.values.get(kind, []):
                if seen.docs & exclude:
                    masked.append(
                        {"side": side, "kind": kind, **seen.as_dict(), "reason": "same document"}
                    )
        # a row's own details are a document of the row, never of a candidate: the row side keeps
        # them, and masking only applies to documents both sides read
        dob_value = person.first("dob", exclude)
        dob = parse_dob(dob_value.value) if dob_value else None
        age_value = person.first("age", exclude)
        age = _int(age_value)
        lo, hi = birth_interval(dob=dob, age=age, ref_year=ref_year)
        city_value = person.first("city", exclude)
        city = place_key(city_value.value) if city_value else ""
        city_keys = place_keys(city_value.value) if city_value else []
        case_value = person.first("case_number", exclude)
        case = case_key(case_value.value) if case_value else ""
        picked: tuple[tuple[str, Value | None], ...] = (
            ("dob", dob_value if dob else None),
            ("age", age_value if dob is None and lo is not None else None),
            ("city", city_value if city else None),
            ("case_number", case_value if case else None),
        )
        for kind, value in picked:
            if value is not None:
                used.setdefault(kind, {})[side] = value.as_dict()
        surname = letters(person.surname)
        given = letters(person.given)
        counties = self.counties.of(person.state, city_keys)
        return {
            "surname": surname or None,
            "surname_dm": metaphone_codes(surname),
            "given_name": given or None,
            "given_forms": [
                *self.nicknames.forms(given),
                *(letters(n) for n in person.extras.get("nicknames", ()) if letters(n)),
            ],
            "middle": _middle(person),
            "dob": dob.isoformat() if dob else None,
            "by_lo": lo,
            "by_hi": hi,
            "city": city or None,
            "counties": counties,
            "state": person.state,
            "case_no": case or None,
            "_age_only": dob is None and lo is not None,
        }

    def build(self, pair_id: int, left: Person, right: Person, ref_year: int | None) -> Pair:
        masked: list[dict[str, Any]] = []
        used: dict[str, dict[str, Any]] = {}
        left_side = self._side(left, right, ref_year, masked, used, "left")
        right_side = self._side(right, left, ref_year, masked, used, "right")
        # the prior's cohort is the candidate's (the court record's age or date of birth); a
        # candidate with neither takes the row's own, and with neither anywhere it is cohort-free
        basis = right_side if right_side["by_lo"] is not None else left_side
        widen = AGE_WIDEN if basis["_age_only"] else 0
        prior = self.prior_model.prior(
            surname=left.surname,
            given=left.given,
            state=left.state,
            birth_lo=basis["by_lo"],
            birth_hi=basis["by_hi"],
            widen=widen,
        )
        prior.components["cohort_basis"] = (
            "candidate" if basis is right_side and basis["by_lo"] is not None
            else "row" if basis["by_lo"] is not None else "none"
        )  # fmt: skip
        prior.components["widen_years"] = widen
        for side in (left_side, right_side):
            side.pop("_age_only", None)
        inputs = {
            "surname": left.surname, "given": left.given, "state": left.state,
            "birth_lo": basis["by_lo"], "birth_hi": basis["by_hi"], "widen": widen,
        }  # fmt: skip
        boost = self.prior_model.age_boost_bits(inputs, prior)
        prior.components["age_boost_bits"] = boost
        flags = []
        a, b = letters(left.suffix), letters(right.suffix)
        if a and b and a != b:
            flags.append("suffix_conflict")  # a Sr. and a Jr. share a name, and often a case
        return Pair(pair_id, left_side, right_side, prior, used, masked, inputs, boost, flags)


def workbook_term_frequencies(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Surnames and given names of every workbook row, for the model's term-frequency lookups."""
    from efp.court.names import parse_name

    surnames: list[str] = []
    givens: list[str] = []
    for (name,) in conn.execute("SELECT name FROM rows WHERE in_scope = 1"):
        parts = parse_name(name)
        if parts.surname:
            surnames.append(letters(parts.surname))
        if parts.given:
            givens.append(letters(parts.given))
    return {"surname": surnames, "given_name": givens}


def today() -> date:
    return date.today()

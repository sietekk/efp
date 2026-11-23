"""Normalisation for the identity comparisons: names, nicknames, dates, cities, case numbers.

Everything here is deterministic and local. The nickname table is a curated, versioned CSV beside
this file (``nicknames.csv``); its version and SHA-256 are written into every decision's inputs
(spec section 8: "Nickname table curated and versioned").

The place and county keys are kept in step with ``efp.reference.places`` (the loader writes them,
this reads them); components do not import each other, so ``tests/test_identity_normalize.py``
checks the two agree.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

from doublemetaphone import doublemetaphone

NICKNAMES_PATH = Path(__file__).with_name("nicknames.csv")
_NON_LETTER = re.compile(r"[^A-Z]")

# --- names -------------------------------------------------------------------------------------


def letters(text: str | None) -> str:
    """Upper-case letters only: the form every name comparison runs on."""
    return _NON_LETTER.sub("", (text or "").upper())


def metaphone_codes(surname: str) -> list[str]:
    """Primary and alternate Double Metaphone codes (empty for an empty name)."""
    primary, alternate = doublemetaphone(surname)
    return sorted({c for c in (primary, alternate) if c})


@dataclass(frozen=True)
class NicknameTable:
    version: str
    sha256: str
    _forms: dict[str, frozenset[str]]

    def forms(self, name: str | None) -> list[str]:
        """The canonical forms a given name stands for: itself, and each name it is short for."""
        key = letters(name)
        if not key:
            return []
        return sorted({key} | self._forms.get(key, frozenset()))

    def match(self, left: str | None, right: str | None) -> bool:
        """One name is a recorded nickname of the other. Two nicknames of the same name do not
        match (``Al`` and ``Fred`` are both short for Alfred): the conservative reading."""
        a, b = letters(left), letters(right)
        return bool(a and b and (a in self.forms(b) or b in self.forms(a)))


@lru_cache(maxsize=4)
def load_nicknames(path: Path = NICKNAMES_PATH) -> NicknameTable:
    raw = path.read_bytes()
    version = "0"
    forms: dict[str, set[str]] = {}
    for line in raw.decode("utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        if text.startswith("#"):
            head = text.lstrip("# ").lower()
            if head.startswith("version:"):
                version = head.split(":", 1)[1].strip()
            continue
        canonical, _, variant = text.partition(",")
        c, v = letters(canonical), letters(variant)
        if c and v:
            forms.setdefault(v, set()).add(c)
    return NicknameTable(
        version, hashlib.sha256(raw).hexdigest(), {k: frozenset(v) for k, v in forms.items()}
    )


def initial_match(left: str | None, right: str | None) -> bool:
    """One side is an initial and it is the first letter of the other (or both are initials)."""
    a, b = letters(left), letters(right)
    if not a or not b:
        return False
    return (len(a) == 1 or len(b) == 1) and a[0] == b[0]


# --- dates and ages ----------------------------------------------------------------------------

_MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}
_DATE_TEXT = re.compile(r"^\s*([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\s*$")
_DATE_SLASH = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{2,4})\s*$")


def parse_dob(text: str | None, *, today: date | None = None) -> date | None:
    """A birth date from ``March 3, 1975`` or ``3/3/75``; None when it is not a plausible one."""
    if not text:
        return None
    today = today or date.today()
    month = day = year = 0
    if m := _DATE_TEXT.match(text):
        month = _MONTHS.get(m.group(1)[:3].lower(), 0)
        day, year = int(m.group(2)), int(m.group(3))
    elif m := _DATE_SLASH.match(text):
        month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if year < 100:  # two digits: the century that makes the person at most 110 and not unborn
            year += 2000 if year + 2000 <= today.year else 1900
    try:
        found = date(year, month, day)
    except ValueError:
        return None
    return found if 1890 <= found.year <= today.year else None


def birth_interval(
    *, dob: date | None, age: int | None, ref_year: int | None
) -> tuple[int | None, int | None]:
    """The birth years a person could have: one year for a date of birth, two for an age (an age
    in a document of ``ref_year`` means born in ``ref_year - age - 1`` or ``ref_year - age``)."""
    if dob is not None:
        return dob.year, dob.year
    if age is not None and ref_year is not None and 0 < age < 115:
        return ref_year - age - 1, ref_year - age
    return None, None


# --- places and case numbers -------------------------------------------------------------------

# The legal suffixes efp.reference.places drops from a Census place name, minus "city": in running
# text "Jersey City" is the whole name, so it is never shortened (a test keeps the lists in step).
_PLACE_SUFFIX = re.compile(
    r"\s+(?:town|village|borough|township|cdp|municipality|plantation|"
    r"city and borough|consolidated government|metropolitan government|metro government|"
    r"urban county|unified government|comunidad|zona urbana)\s*$",
    re.I,
)
_BALANCE = re.compile(r"\s*\(balance\)\s*$", re.I)


def _place_text(city: str | None) -> str:
    text = (city or "").strip()
    if not text:
        return ""
    if "," in text or any(ch.isdigit() for ch in text):
        try:
            import usaddress

            tagged, _ = usaddress.tag(text)
            text = tagged.get("PlaceName", text.split(",")[0])
        except Exception:  # usaddress raises RepeatedLabelError on odd strings
            text = text.split(",")[0]
    return text


def place_key(city: str | None) -> str:
    """``"Springfield, Ill."`` or ``"123 Main St, Springfield"`` -> ``SPRINGFIELD``. Nothing is
    shortened: ``"Salt Lake City"`` stays ``SALTLAKECITY``."""
    return letters(_place_text(city))


def place_keys(city: str | None) -> list[str]:
    """The keys to look a place up by in the Census table, best first: the name as written, then,
    if it ends with a legal suffix other than "city" ("Hartford town"), without it. The second is
    only tried when the first finds nothing, so a name that is whole never loses a word."""
    text = _place_text(city)
    keys = [letters(text)]
    shorter = letters(_PLACE_SUFFIX.sub("", _BALANCE.sub("", text), count=1))
    if shorter and shorter not in keys:
        keys.append(shorter)
    return [k for k in keys if k]


_CASE = re.compile(r"(?:(\d{1,2}):)?\s*(\d{2,4})\s*[-\s]?\s*([A-Za-z]{1,4})\s*[-\s]?\s*0*(\d{1,7})")


def case_key(number: str | None) -> str:
    """``1:19-cr-00123-ABC-1`` and ``19CR123`` -> ``2019-CR-123`` (the division and the judge's
    initials are not part of the case; an unknown shape is reduced to letters and digits)."""
    text = (number or "").strip()
    if not text:
        return ""
    m = _CASE.search(text)
    if m:
        year = int(m.group(2))
        if year < 100:
            year += 2000 if year < 50 else 1900
        return f"{year}-{m.group(3).upper()}-{int(m.group(4))}"
    return re.sub(r"[^A-Z0-9]", "", text.upper())

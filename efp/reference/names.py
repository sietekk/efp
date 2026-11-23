"""Splitting a full name into surname, given and middle, once, at load time.

The installed ``nameparser`` is the 3.x rewrite (``nameparser.parse``); this is the one place that
uses it, so a change of its API touches only here. A name that cannot be split keeps its whole text
as the surname (a blocking key is never empty for a real name). ``surname_key`` is upper-case A-Z
only: the key build 12 blocks on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_NON_LETTER = re.compile(r"[^A-Z]")


@dataclass(frozen=True)
class Split:
    surname: str
    given: str
    middle: str

    @property
    def key(self) -> str:
        return surname_key(self.surname)


def surname_key(surname: str) -> str:
    return _NON_LETTER.sub("", surname.upper())


def split_name(full: str | None) -> Split:
    """``"SMITH, JOHN Q"`` or ``"John Q. Smith"`` into its parts."""
    text = (full or "").strip()
    if not text:
        return Split("", "", "")
    from nameparser import parse  # 3.x; an ImportError is a broken install, never a fallback

    try:
        parsed = parse(text)
        family, given, middle = str(parsed.family), str(parsed.given), str(parsed.middle)
    except Exception:  # a name the parser cannot read is still a name
        family, given, middle = text, "", ""
    if not family:
        family = text
    return Split(family.strip(), given.strip(), middle.strip())


def split_parts(surname: str | None, given: str | None, middle: str | None) -> Split:
    """A source that already has the parts: trimmed, nothing re-parsed."""
    return Split((surname or "").strip(), (given or "").strip(), (middle or "").strip())

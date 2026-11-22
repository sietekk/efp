"""Parse a subject's name and test whether a text names that person.

The Heritage ``name`` column is free text ("Alex Q. Example Jr.", "Example, Alex", sometimes with
an "aka"). This is a small deterministic parser (given, middle, surname, suffix, aliases) plus a
matcher. It decides only whether a *text mentions the subject by name*; whether two people are the
same person is the identity stage's job (build 12).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v", "esq"}
_PARTICLES = {
    "de",
    "del",
    "della",
    "di",
    "da",
    "la",
    "le",
    "van",
    "von",
    "der",
    "den",
    "st",
    "san",
    "mac",
}
_AKA = re.compile(r"\b(?:aka|a/k/a|a\.k\.a\.?|also known as|alias|f/k/a|fka)\b", re.I)
_NICK = re.compile(r"[\"\u201c(]([^\"\u201d)]+)[\"\u201d)]")
_TITLES = {"dr", "mr", "mrs", "ms", "hon", "rev", "sgt", "officer", "mayor", "sheriff"}


@dataclass(frozen=True)
class NameParts:
    given: str
    middle: str
    surname: str
    suffix: str = ""
    aliases: tuple[NameParts, ...] = field(default_factory=tuple)
    nicknames: tuple[str, ...] = field(default_factory=tuple)

    @property
    def usable(self) -> bool:
        """A name that can be matched at all: a surname of at least two letters."""
        return len(self.surname) >= 2

    @property
    def full(self) -> str:
        return " ".join(p for p in (self.given, self.middle, self.surname, self.suffix) if p)

    def variants(self) -> list[NameParts]:
        return [self, *self.aliases]


def _clean(token: str) -> str:
    return token.strip(" .,;:")


def _split_one(text: str) -> NameParts:
    nicknames = tuple(_clean(n) for n in _NICK.findall(text) if _clean(n))
    text = _NICK.sub(" ", text)
    comma_last: str | None = None
    if "," in text:
        head, _, tail = text.partition(",")
        if _clean(tail).lower().rstrip(".") not in _SUFFIXES and _clean(tail):
            comma_last, text = _clean(head), tail  # "Example, Alex Q." -> surname first
    tokens = [t for t in re.split(r"\s+", text.strip()) if _clean(t)]
    tokens = [t for t in tokens if _clean(t).lower() not in _TITLES]
    suffix = ""
    while tokens and _clean(tokens[-1]).lower() in _SUFFIXES:
        suffix = _clean(tokens.pop())
    if comma_last is not None:
        given = _clean(tokens[0]) if tokens else ""
        middle = " ".join(_clean(t) for t in tokens[1:])
        return NameParts(given, middle, comma_last, suffix, nicknames=nicknames)
    if not tokens:
        return NameParts("", "", "", suffix, nicknames=nicknames)
    if len(tokens) == 1:
        return NameParts("", "", _clean(tokens[0]), suffix, nicknames=nicknames)
    end = len(tokens) - 1
    start = end
    while start > 1 and _clean(tokens[start - 1]).lower() in _PARTICLES:
        start -= 1  # "de la Cruz": particles belong to the surname
    surname = " ".join(_clean(t) for t in tokens[start : end + 1])
    given = _clean(tokens[0])
    middle = " ".join(_clean(t) for t in tokens[1:start])
    return NameParts(given, middle, surname, suffix, nicknames=nicknames)


def parse_name(text: str | None) -> NameParts:
    """Parse the Name cell; ``aka`` parts become aliases of the main name."""
    if not text or not text.strip():
        return NameParts("", "", "")
    pieces = [p for p in _AKA.split(text) if p.strip()]
    main = _split_one(pieces[0])
    aliases = tuple(a for a in (_split_one(p) for p in pieces[1:]) if a.usable)
    return NameParts(main.given, main.middle, main.surname, main.suffix, aliases, main.nicknames)


@dataclass(frozen=True)
class NameMatch:
    full: bool  # surname and given name (or a nickname, or a matching initial) all present
    surname_only: bool
    variant: str  # the form of the name that matched
    span: str  # the matching text, verbatim
    initial_only: bool = False  # only an initial and the surname matched: weaker evidence


def _word(token: str) -> str:
    return rf"(?<![A-Za-z]){re.escape(token)}(?![A-Za-z])"


def match_name(parts: NameParts, text: str) -> NameMatch | None:
    """Does ``text`` name this person? Full needs the surname plus a given name or initial."""
    best: NameMatch | None = None
    for variant in parts.variants():
        if not variant.usable:
            continue
        given_forms = {g for g in (variant.given, *variant.nicknames, *parts.nicknames) if g}
        surname = _word(variant.surname)
        initial = variant.given[:1]
        patterns: list[tuple[str, bool]] = []
        for given in given_forms:
            # the optional middle words must be capitalised: "John told May" is not "John May"
            middle = r"(?:\s+(?-i:[A-Z]\.?))?(?:\s+(?-i:[A-Z][a-z]+))?"
            patterns.append((rf"{_word(given)}{middle}\s+{surname}", False))
            patterns.append((rf"{surname}\s*,\s*{_word(given)}", False))
        if initial:
            only = rf"(?<![A-Za-z]){re.escape(initial)}\.\s*(?:[A-Z]\.\s*)?{surname}"
            patterns.append((only, True))
        for pattern, initial_only in patterns:
            found = re.search(pattern, text, re.I)
            if found:
                return NameMatch(True, False, variant.full, found.group(0), initial_only)
        loose = re.search(surname, text, re.I)
        if loose and best is None:
            best = NameMatch(False, True, variant.full, loose.group(0))
    return best

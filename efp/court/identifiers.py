"""Rules-based identifier extraction from located records (press releases, dockets).

Regular expressions over captured text. Each finding carries the verbatim ``span`` it came from,
which is always a substring of the text. Person-level facts (age, city, date of birth, office,
employer, party, alias) are taken only from the neighbourhood of a mention of the subject, so a
release naming several defendants does not hand one defendant another's age; case numbers are
taken from the whole document. The model-based extractor (build 16) adds to these and never
replaces them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from efp.court.names import NameParts

WINDOW_AFTER = 380

_CITY = r"[A-Z][A-Za-z.'\u2019\-]+(?:\s+[A-Z][A-Za-z.'\u2019\-]+){0,3}"
_STATE = r"(?:[A-Z][a-z]+(?:\s[A-Z][a-z]+)?|[A-Z]{2})"

AGE_OF_CITY = re.compile(
    rf"[,(]\s*(?P<age>\d{{2}})\s*[,)]\s*(?:a resident )?of\s+(?P<city>{_CITY})(?:,\s*(?P<state>{_STATE}))?"
)
AGE_YEAR_OLD = re.compile(r"\b(?P<age>\d{2})[- ]year[- ]old\b", re.I)
AGE_AGED = re.compile(r"\b(?:aged?)\s+(?P<age>\d{2})\b", re.I)
OF_CITY = re.compile(
    rf"\b(?:resident of|a resident of|of|from)\s+(?P<city>{_CITY})(?:,\s*(?P<state>{_STATE}))?"
)
DOB = re.compile(
    r"(?:born(?: on)?|date of birth|DOB)[:\s]+(?P<dob>(?:[A-Z][a-z]+\.? \d{1,2}, \d{4})|(?:\d{1,2}/\d{1,2}/\d{2,4}))",
    re.I,
)
CASE_NUMBERS = (
    re.compile(r"\b\d{1,2}:\d{2}-(?:cr|mj|cv|mc)-\d{2,6}(?:-[A-Z]{1,4})*\b", re.I),
    re.compile(r"\b(?:Case\s+)?No\.?\s*(?P<n>\d{2}-(?:CR|cr)-\d{2,6}(?:-[A-Z]{1,4})*)", re.I),
    re.compile(r"\bCase\s+(?:Number|No\.?)[:\s]+(?P<n>[0-9A-Za-z:\-]{6,25})", re.I),
)
OFFICE = re.compile(
    r"\b(?:a\s+)?(?:former|current|then-|then)?\s*"
    r"(?P<o>(?:county|city|town|township|state|deputy|assistant)\s+(?:clerk|registrar|commissioner|treasurer|auditor|"
    r"election\s+(?:judge|official|worker|commissioner|director)|poll\s*worker|mayor|councilman|councilwoman|"
    r"council\s*member|sheriff|alderman|representative|senator|committeeman|committeewoman)|"
    r"election\s+(?:judge|official|worker|commissioner)|poll\s*worker|mayor|precinct\s+(?:chair|captain|committeeman|committeewoman)|"
    r"state\s+(?:representative|senator)|city\s+council\w*)",
    re.I,
)
EMPLOYER = re.compile(
    r"\b(?:employed\s+(?:by|at)|worked\s+(?:for|at)|an\s+employee\s+of)\s+(?:the\s+)?"
    r"(?P<e>[A-Z][\w&.\-]*(?:\s+[A-Za-z&.\-]+){0,5}?)(?=[,.;]|\s+(?:and|when|who|as|in)\b)"
)
PARTY = re.compile(
    r"\b(?P<p>Republican|Democratic|Democrat|Libertarian|Green Party|Independent|Reform Party)\s+"
    r"(?:candidate|nominee|primary candidate|challenger|incumbent)|"
    r"\b(?:ran|running|candidate)\s+(?:for\s+[\w\s]{3,40}?\s+)?as\s+(?:a|an|the)\s+(?P<p2>Republican|Democrat|Libertarian|Independent)\b",
    re.I,
)
ALIAS = re.compile(
    r"\b(?:also known as|a/k/a|a\.k\.a\.?|aka|alias)\s+[\"\u201c']?(?P<a>[A-Z][\w.'\u2019\- ]{1,40}?)[\"\u201d']?(?=[,.;)\]]|\s+(?:was|is|and|of)\b|$)"
)


@dataclass(frozen=True)
class Found:
    kind: str
    value: str
    span: str  # verbatim substring of the text


_ABBREVIATIONS = {
    "no",
    "st",
    "mr",
    "mrs",
    "ms",
    "dr",
    "jr",
    "sr",
    "inc",
    "co",
    "vs",
    "v",
    "u.s",
    "u.s.a",
    "d",
    "r",
}


def _sentence_end(text: str, start: int, limit: int) -> int:
    """The end of the sentence that begins at ``start`` (not fooled by "No." or "U.S.")."""
    for match in re.finditer(r"[.!?][\"\u201d')]*(?=\s+[A-Z\"\u201c])", text[start:limit]):
        word = re.findall(r"[A-Za-z.]+$", text[start : start + match.start()])
        if word and word[0].lower().rstrip(".") in _ABBREVIATIONS:
            continue
        return start + match.end()
    return limit


def _sentence_start(text: str, pos: int) -> int:
    """Where the sentence containing ``pos`` begins."""
    boundary = 0
    for match in re.finditer(r"[.!?][\"\u201d')]*\s+(?=[A-Z\"\u201c])", text[: pos + 1]):
        word = re.findall(r"[A-Za-z.]+$", text[: match.start()])
        if word and word[0].lower().rstrip(".") in _ABBREVIATIONS:
            continue
        boundary = match.end()
    return boundary


_OTHER_PERSON = re.compile(
    r"[A-Z][A-Za-z.'\u2019\-]+(?:\s+[A-Z][A-Za-z.'\u2019\-]+){1,2},\s*\d{2}\s*,"
)


def _cut_at_next_person(text: str, start: int, end: int, own_name_end: int) -> int:
    """Shorten a window that runs on into another defendant's own clause ("..., and Blake Other,
    52, of Hartford"), so their age and city are not read as the subject's."""
    other = _OTHER_PERSON.search(text, own_name_end, end)
    return other.start() if other else end


def _windows(text: str, parts: NameParts | None) -> list[str]:
    """The sentence of each mention of the subject (the whole text if there is no name).

    A release naming several defendants puts each one's age and city in that defendant's own
    sentence, so the window stops at the end of the sentence the name appears in.
    """
    if parts is None or not parts.usable:
        return [text]
    spots: list[tuple[int, int]] = []
    for variant in parts.variants():
        if not variant.usable:
            continue
        for match in re.finditer(
            rf"(?<![A-Za-z]){re.escape(variant.surname)}(?![A-Za-z])", text, re.I
        ):
            end = _sentence_end(text, match.start(), min(len(text), match.end() + WINDOW_AFTER))
            end = _cut_at_next_person(text, match.start(), end, match.end() + 12)
            spots.append((_sentence_start(text, match.start()), end))
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spots):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return [text[s:e] for s, e in merged]


def _add(
    out: list[Found], seen: set[tuple[str, str]], kind: str, value: str, span: str, text: str
) -> None:
    value = re.sub(r"\s+", " ", value).strip(" ,.;")
    if not value or span not in text or (kind, value.lower()) in seen:
        return
    seen.add((kind, value.lower()))
    out.append(Found(kind, value, span))


def extract_identifiers(text: str, parts: NameParts | None = None) -> list[Found]:
    """Every identifier the rules find, with the verbatim span each came from."""
    out: list[Found] = []
    seen: set[tuple[str, str]] = set()
    windows = _windows(text, parts)
    found_numbers: list[tuple[str, str]] = []
    for pattern in CASE_NUMBERS:
        for match in pattern.finditer(text):
            found_numbers.append((match.groupdict().get("n") or match.group(0), match.group(0)))
    distinct = {value.lower() for value, _ in found_numbers}
    for value, span in found_numbers:
        # One case number in the whole document is that document's; with several (a multi-defendant
        # release) only those in the subject's own sentences are taken, so another defendant's case
        # number is not recorded for this one.
        if len(distinct) == 1 or parts is None or any(span in w for w in windows):
            _add(out, seen, "case_number", value, span, text)
    for window in windows:
        for match in AGE_OF_CITY.finditer(window):
            _add(out, seen, "age", match.group("age"), match.group(0), text)
            city = match.group("city")
            _add(out, seen, "city", city, match.group(0), text)
        for pattern in (AGE_YEAR_OLD, AGE_AGED):
            for match in pattern.finditer(window):
                _add(out, seen, "age", match.group("age"), match.group(0), text)
        for match in DOB.finditer(window):
            _add(out, seen, "dob", match.group("dob"), match.group(0), text)
        if not any(f.kind == "city" for f in out):
            for match in OF_CITY.finditer(window):
                _add(out, seen, "city", match.group("city"), match.group(0), text)
                break
        for match in OFFICE.finditer(window):
            _add(out, seen, "office", match.group("o").lower(), match.group(0).strip(), text)
        for match in EMPLOYER.finditer(window):
            _add(out, seen, "employer", match.group("e"), match.group(0), text)
        for match in PARTY.finditer(window):
            party = match.group("p") or match.group("p2")
            _add(out, seen, "party_on_ballot", party.title(), match.group(0), text)
        for match in ALIAS.finditer(window):
            _add(out, seen, "alias", match.group("a"), match.group(0), text)
    return out

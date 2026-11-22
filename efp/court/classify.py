"""Federal, state or unknown: which court strategies apply to a row.

Positive federal signals send a row to the federal strategies; positive state signals alone keep
it out of them. A row with no signal either way is ``unknown`` and gets the federal strategies too:
they are cheap (a local index and a local press-release index), and treating "no signal" as "state"
would leave most rows without a federal attempt. The signals and which way they pointed are kept
in ``summary_json`` so the identity build can see them.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Literal

Kind = Literal["federal", "state", "unknown"]

_FEDERAL_HOSTS = re.compile(
    r"(^|\.)(justice\.gov|uscourts\.gov|courtlistener\.com|pacer\.gov|fbi\.gov|ice\.gov)$"
)
_STATE_HOSTS = re.compile(
    r"(\.state\.[a-z]{2}\.us$|^(www\.)?ag\.[a-z.]+$|attorneygeneral|(^|\.)da\.|districtattorney|"
    r"(^|\.)sos\.|secretaryofstate|(^|\.)courts\.[a-z]{2}\.gov$)"
)
_FEDERAL_TEXT = re.compile(
    r"U\.?S\.? Attorney|United States Attorney|federal (?:court|jury|prosecutors?|grand jury|charges?|indictment)|"
    r"U\.?S\.? District Court|United States District Court|\bFBI\b|"
    r"\b(?:18|42|52)\s*U\.?S\.?C\.?|U\.S\. v\.|United States v\.",
    re.I,
)
_STATE_TEXT = re.compile(
    r"District Attorney|state court|county (?:court|prosecutor)|Superior Court|Circuit Court|"
    r"Court of Common Pleas|Attorney General'?s Office|Commonwealth v\.|State v\.|"
    r"\bcounty (?:clerk|board of elections)\b|state (?:charges|prosecutors)",
    re.I,
)


@dataclass(frozen=True)
class Classification:
    kind: Kind
    basis: list[str] = field(default_factory=list)

    @property
    def federal_strategies_apply(self) -> bool:
        return self.kind in ("federal", "unknown")


def link_hosts(conn: sqlite3.Connection, row_id: int) -> list[str]:
    """The hosts of this row's expanded source links (from build 06), ``www.`` removed."""
    from urllib.parse import urlsplit

    hosts: list[str] = []
    try:
        found = conn.execute(
            "SELECT coalesce(url_expanded, url_raw) FROM links WHERE row_id = ? "
            "AND coalesce(note, '') != 'no_url'",
            (row_id,),
        ).fetchall()
    except sqlite3.Error:  # no links table yet (the stage has not run): no signal
        return []
    for (url,) in found:
        host = (urlsplit(url if "//" in url else f"//{url}").hostname or "").lower()
        hosts.append(host.removeprefix("www."))
    return hosts


def classify(details: str | None, source_raw: str | None, hosts: list[str]) -> Classification:
    federal: list[str] = []
    state: list[str] = []
    for host in hosts:
        if _FEDERAL_HOSTS.search(host):
            federal.append(f"source domain {host}")
        elif _STATE_HOSTS.search(host):
            state.append(f"source domain {host}")
    text = f"{details or ''}\n{source_raw or ''}"
    for match in _FEDERAL_TEXT.finditer(text):
        federal.append(f"text: {match.group(0)}")
    for match in _STATE_TEXT.finditer(text):
        state.append(f"text: {match.group(0)}")
    federal = list(dict.fromkeys(federal))
    state = list(dict.fromkeys(state))
    if federal:
        return Classification("federal", federal + [f"(also state: {s})" for s in state[:2]])
    if state:
        return Classification("state", state)
    return Classification("unknown", ["no federal or state signal"])

"""Extract the URLs from a Source cell.

``rows.source_raw`` is free text: zero or more URLs, sometimes bare (``herit.ag/abc`` with no
scheme), sometimes followed by a period or comma, sometimes with a note. This module is a pure
function of that text. ``url_raw`` keeps each URL as it appears (cleaned of surrounding
punctuation); ``fetch_url`` is what to request (``https://`` is assumed for a bare form, with the
caller falling back to ``http://``); ``key`` de-duplicates within a row.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

# Link shorteners seen in or likely to be in the Source column. herit.ag is Heritage's own.
SHORTENERS = frozenset(
    {
        "herit.ag",
        "bit.ly",
        "ow.ly",
        "tinyurl.com",
        "t.co",
        "goo.gl",
        "is.gd",
        "buff.ly",
        "wp.me",
        "tiny.cc",
        "fb.me",
        "j.mp",
        "dlvr.it",
        "bitly.com",
        "bitly.is",
    }
)
# Bitly operates these; its "+" info page exists for each.
BITLY_HOSTS = frozenset({"bit.ly", "herit.ag", "j.mp"})

# A bare host (no scheme) counts only if its last label is one of these.
_TLDS = (
    "com|org|net|gov|edu|mil|us|info|biz|ag|ly|co|io|news|tv|me|uk|ca|de|fm|to|cc|ws|"
    "am|fyi|press|media|today|live"
)
_SCHEMED = re.compile(r"https?://[^\s<>\"'\u201c\u201d\u2018\u2019\u00ab\u00bb]+", re.I)
_BARE = re.compile(
    rf"(?<![\w@./:-])((?:www\.)?(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?:{_TLDS}))(?![\w-])"
    r"((?:/[^\s<>\"'\u201c\u201d\u2018\u2019\u00ab\u00bb]*)?)",
    re.I,
)
_TRAILING = ".,;:!?'\"\u2026\u201c\u201d\u2018\u2019\u00ab\u00bb|"
_PAIRS = {")": "(", "]": "[", "}": "{", ">": "<"}


@dataclass(frozen=True)
class ParsedUrl:
    raw: str  # as it appears in the cell, without surrounding punctuation
    fetch_url: str
    has_scheme: bool
    host: str
    key: str  # normalised, for de-duplication

    @property
    def is_shortener(self) -> bool:
        return self.host in SHORTENERS


def _trim(candidate: str) -> str:
    """Remove trailing punctuation, keeping a closing bracket that has its opener inside."""
    while candidate:
        last = candidate[-1]
        unbalanced = last in _PAIRS and candidate.count(last) > candidate.count(_PAIRS[last])
        if last in _TRAILING or unbalanced:
            candidate = candidate[:-1]
        else:
            break
    return candidate


def _host(url: str) -> str:
    return (
        (urlsplit(url if "//" in url else f"//{url}").hostname or "").lower().removeprefix("www.")
    )


def _key(url: str) -> str:
    parts = urlsplit(url if "//" in url else f"//{url}")
    host = (parts.hostname or "").lower().removeprefix("www.")
    path = parts.path.rstrip("/")
    query = f"?{parts.query}" if parts.query else ""
    return f"{host}{path}{query}"


def parse_source_cell(text: str | None) -> list[ParsedUrl]:
    """Every distinct URL in the cell, in order of first appearance."""
    if not text or not text.strip():
        return []
    # "a.com/x,http://b.com" and "a.com/x;http://b.com": start a new URL after , or ;
    cleaned = re.sub(r"(?<=[,;|\w])(?=https?://)", " ", text)
    found: list[tuple[int, str, bool]] = []
    taken: list[tuple[int, int]] = []
    for match in _SCHEMED.finditer(cleaned):
        found.append((match.start(), match.group(0), True))
        taken.append((match.start(), match.end()))
    for match in _BARE.finditer(cleaned):
        if any(s <= match.start() < e for s, e in taken):
            continue  # already part of a schemed URL
        path = match.group(2)
        if not path:
            continue  # a bare host with no path is a name in prose ("see cnn.com", "via bit.ly")
        found.append((match.start(), match.group(0), False))
    out: list[ParsedUrl] = []
    seen: set[str] = set()
    for _, candidate, schemed in sorted(found):
        raw = _trim(candidate)
        if not raw or not _host(raw):
            continue
        key = _key(raw)
        if key in seen:
            continue
        seen.add(key)
        fetch = raw if schemed else f"https://{raw}"
        out.append(ParsedUrl(raw, fetch, schemed, _host(raw), key))
    return out

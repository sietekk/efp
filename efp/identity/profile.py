"""Person records on both sides of a comparison, with where each fact came from.

A *person* is the row (and one per ``aka`` alias) or a candidate gathered from a court record or a
reference dataset. Every value carries the capture it was read from and its verbatim span, so
``efp identity explain`` can show the text behind each agreeing field, and so the independence rule
can be applied: a fact only counts as agreement when the two sides read it from different documents
(a case number the court stage lifted from a press release does not "agree" with the same press
release reached through the row's own link).

The pure text helpers of the court package (the name parser and the identifier rules) are reused so
the row and the court record are read by the same rules; no stage state is shared.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from efp.court.identifiers import extract_identifiers
from efp.court.names import NameParts, parse_name
from efp.store import StoreError, read_verified

DOC_KINDS = ("dob", "age", "city", "case_number", "office", "employer", "party_on_ballot", "alias")


@dataclass(frozen=True)
class Value:
    value: str
    capture_id: int | None
    span: str | None
    docs: frozenset[str] = frozenset()

    def as_dict(self) -> dict[str, Any]:
        return {"value": self.value, "capture_id": self.capture_id, "span": self.span}


@dataclass
class Person:
    source: (
        str  # 'row' | 'court' | 'dime' | 'fec' | 'ftm' | 'harvard_local' | 'congress' | 'state_leg'
    )
    source_id: str | None
    given: str
    middle: str
    surname: str
    suffix: str = ""
    state: str | None = None
    values: dict[str, list[Value]] = field(default_factory=dict)
    docs: frozenset[str] = frozenset()
    name_value: Value | None = None  # the verbatim name text and its capture
    extras: dict[str, Any] = field(default_factory=dict)
    alias_index: int = 0  # 0 = the Name cell's own name, 1.. = the aka names in order

    def add(self, kind: str, value: Value) -> None:
        self.values.setdefault(kind, []).append(value)

    def first(self, kind: str, exclude: frozenset[str] = frozenset()) -> Value | None:
        for value in self.values.get(kind, []):
            if not (value.docs & exclude):
                return value
        return None


_WAYBACK = re.compile(r"https?://web\.archive\.org/web/\d+[a-z_]*/(.+)$", re.I)


def doc_keys(sha256: str | None, *urls: str | None) -> frozenset[str]:
    """A document's identity: its content hash and its URL (a Wayback copy also by the URL it
    archived), so two readings of one page are recognised as one document."""
    keys = set()
    if sha256:
        keys.add(f"sha:{sha256}")
    every = [u.strip() for u in urls if u]
    every += [m.group(1) for u in every if (m := _WAYBACK.match(u))]
    for url in every:
        stripped = re.sub(r"^https?://(www\.)?", "", url.lower()).split("#")[0].rstrip("/")
        keys.add("url:" + stripped)
    return frozenset(keys)


class CaptureText:
    """Text of stored captures, read once per run."""

    def __init__(self, conn: sqlite3.Connection, store_root: Path) -> None:
        self.conn = conn
        self.store = store_root
        self._text: dict[int, str | None] = {}
        self._docs: dict[int, frozenset[str]] = {}

    def docs(self, capture_id: int) -> frozenset[str]:
        if capture_id not in self._docs:
            found = self.conn.execute(
                "SELECT content_sha256, url_final, url_requested FROM captures "
                "WHERE capture_id = ?",
                (capture_id,),
            ).fetchone()
            self._docs[capture_id] = doc_keys(*found) if found else frozenset()
        return self._docs[capture_id]

    def text(self, capture_id: int) -> str | None:
        if capture_id in self._text:
            return self._text[capture_id]
        found = self.conn.execute(
            "SELECT content_sha256, content_type FROM captures WHERE capture_id = ?", (capture_id,)
        ).fetchone()
        text: str | None = None
        if found is not None:
            ctype = str(found[1] or "").lower()
            try:
                body = read_verified(self.store, str(found[0]))
            except StoreError:
                body = b""
            if "pdf" in ctype or body[:5] == b"%PDF-":
                text = None  # the documents stage owns PDFs
            elif "html" in ctype or body.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
                import trafilatura

                try:
                    text = trafilatura.extract(body, include_comments=False, no_fallback=False)
                except Exception:  # third-party extractor over arbitrary pages
                    text = None
            else:
                text = body.decode("utf-8", errors="replace")
        self._text[capture_id] = text
        return text


def row_people(
    conn: sqlite3.Connection,
    texts: CaptureText,
    row_id: int,
    source: dict[str, Any],
    state: str | None,
) -> tuple[NameParts, list[Person]]:
    """The row as a person: its own name, one person per alias, and the identifiers the row's own
    details text and the documents its links resolved to say about the subject."""
    parts = parse_name(source.get("name"))
    values: dict[str, list[Value]] = {}
    details = str(source.get("details") or "")
    ref = frozenset({f"row:{row_id}:details"})
    for found in extract_identifiers(details, parts) if details else []:
        values.setdefault(found.kind, []).append(Value(found.value, None, found.span, ref))
    for (capture_id,) in conn.execute(
        "SELECT DISTINCT capture_id FROM links WHERE row_id = ? AND capture_id IS NOT NULL "
        "ORDER BY link_id",
        (row_id,),
    ).fetchall():
        text = texts.text(int(capture_id))
        if not text:
            continue
        docs = texts.docs(int(capture_id))
        for found in extract_identifiers(text, parts):
            values.setdefault(found.kind, []).append(
                Value(found.value, int(capture_id), found.span, docs)
            )
    people = []
    for index, variant in enumerate(parts.variants()):
        people.append(
            Person(
                "row", str(row_id), variant.given, variant.middle, variant.surname, variant.suffix,
                state, {k: list(v) for k, v in values.items()}, frozenset(),
                Value(variant.full, None, None, ref), {}, index,
            )
        )  # fmt: skip
    return parts, people

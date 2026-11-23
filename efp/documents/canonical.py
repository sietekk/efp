"""The canonical text of a document, and the word map that resolves a span to a page and a box.

The canonical text is defined here once. Changing any rule means bumping ``CANONICAL_VERSION``
(recorded in ``documents.canonical_version``); a text hash is only comparable within one version.

Rules (version 1):
  * pages in order, joined by ``\\f`` (U+000C), with no ``\\f`` after the last page;
  * within a page, lines in reading order joined by ``\\n``; a page with no words is ``""``;
  * within a line, words joined by one space;
  * each word is NFC-normalised, with zero-width characters (U+200B, U+200C, U+200D, U+2060,
    U+FEFF) and all internal whitespace removed; a word left empty is dropped, and a line with no
    word is dropped.
Offsets are Python ``str`` indices (code points) into that text. ``text_sha256`` is the SHA-256 of
its UTF-8 encoding.
"""

from __future__ import annotations

import hashlib
import unicodedata
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Any

from efp.documents.pdfio import Word

CANONICAL_VERSION = 1
_DROP = dict.fromkeys(map(ord, "​‌‍⁠﻿"))


def normalise_word(text: str) -> str:
    return "".join(unicodedata.normalize("NFC", text).translate(_DROP).split())


@dataclass
class PageText:
    """One page as the ladder settled it."""

    page_no: int  # 1-based
    method: str
    lines: list[list[Word]] = field(default_factory=list)
    width: float = 0.0
    height: float = 0.0
    bbox_source: str = "word"  # word | item (a Docling text item) | page (a model transcription)


@dataclass
class Canonical:
    text: str
    units: list[dict[str, Any]]
    spans: dict[int, tuple[int, int]]  # page_no -> [start, end) in text

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


def build(pages: list[PageText]) -> Canonical:
    chunks: list[str] = []
    units: list[dict[str, Any]] = []
    spans: dict[int, tuple[int, int]] = {}
    pos = 0
    for index, page in enumerate(pages):
        if index:
            chunks.append("\f")
            pos += 1
        start = pos
        first_line = True
        for line in page.lines:
            words = [(normalise_word(w.text), w) for w in line]
            words = [(t, w) for t, w in words if t]
            if not words:
                continue
            if not first_line:
                chunks.append("\n")
                pos += 1
            first_line = False
            for j, (text, word) in enumerate(words):
                if j:
                    chunks.append(" ")
                    pos += 1
                units.append(
                    {
                        "page": page.page_no,
                        "bbox": [
                            round(word.x0, 2),
                            round(word.top, 2),
                            round(word.x1, 2),
                            round(word.bottom, 2),
                        ],
                        "char_start": pos,
                        "char_end": pos + len(text),
                        "method": page.method,
                        "bbox_source": page.bbox_source,
                    }
                )
                chunks.append(text)
                pos += len(text)
        spans[page.page_no] = (start, pos)
    return Canonical("".join(chunks), units, spans)


def layout_json(canon: Canonical, pages: list[PageText]) -> dict[str, Any]:
    return {
        "canonical_version": CANONICAL_VERSION,
        "text_sha256": canon.sha256,
        "pages": [
            {
                "page": p.page_no,
                "method": p.method,
                "width": p.width,
                "height": p.height,
                "char_start": canon.spans[p.page_no][0],
                "char_end": canon.spans[p.page_no][1],
            }
            for p in pages
        ],
        "units": canon.units,
    }


def resolve(layout: dict[str, Any], start: int, end: int) -> list[dict[str, Any]]:
    """The pages and boxes under the character span ``[start, end)``: one entry per page, with the
    union box of the words the span touches, in page order."""
    units = layout["units"]
    ends = [u["char_end"] for u in units]
    found: dict[int, list[float]] = {}
    i = bisect_right(ends, start)  # first word that ends after the span starts
    while i < len(units) and units[i]["char_start"] < end:
        unit = units[i]
        box = found.setdefault(unit["page"], list(unit["bbox"]))
        box[0] = min(box[0], unit["bbox"][0])
        box[1] = min(box[1], unit["bbox"][1])
        box[2] = max(box[2], unit["bbox"][2])
        box[3] = max(box[3], unit["bbox"][3])
        i += 1
    return [{"page": page, "bbox": box} for page, box in sorted(found.items())]

"""The per-page quality score in [0, 1] (documented in docs/documents.md).

A weighted mean of the parts that can be measured for the page, renormalised over those present:

  dictionary  0.40  share of lowercase words (3+ letters) found in the word list; capitalised
                    words are skipped (names), unless the page has fewer than 5 lowercase words
                    (an all-capitals page), when every word counts. 0.40 maps to 0, 0.90 to 1.
                    A page with no such word at all scores 0 here (symbols, bare digits).
  charset     0.30  share of characters that are letters, digits, white space or ordinary
                    punctuation, less 5x the share of replacement or control characters, less runs
                    of 4+ identical symbols. 0.50 maps to 0, 0.95 to 1. Multiplied by how much of
                    the page is letters and digits (0.25 maps to 0, 0.65 to 1).
  lines       0.15  share of lines that hold 2+ words or 10+ characters, less the excess of
                    one-short-token lines over 30% (column garbage). 0.30 maps to 0, 0.90 to 1.
                    Absent for a page of fewer than 3 lines.
  confidence  0.15  the engine's mean word confidence, where it gives one (PaddleOCR). 0.50 maps
                    to 0, 0.95 to 1. OCRmyPDF does not expose Tesseract's confidence, so a
                    Tesseract page is scored without this part.

A page under 40 characters is scored on charset alone and marked ``short``. A page with no text
scores 0.0 and is marked ``empty`` (the caller decides between blank and failed).
"""

from __future__ import annotations

import gzip
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

SCORE_VERSION = 1
WEIGHTS = {"dictionary": 0.40, "charset": 0.30, "lines": 0.15, "confidence": 0.15}
SHORT_PAGE_CHARS = 40
WORDLIST = Path(__file__).resolve().parents[2] / "reference" / "wordlist.txt.gz"

_PUNCT = set(".,;:'\"()-/&$%§¶#@!?[]{}*+=_<>|\\~`^") | set(
    "\u2013\u2014\u2018\u2019\u201c\u201d\u2022\u00b0"
)
_BAD = re.compile(r"[�\x00-\x08\x0b\x0e-\x1f]")
_RUN = re.compile(r"([^\w\s\-_.=*])\1{3,}")
_TOKEN = re.compile(r"[A-Za-z]{3,}")


@lru_cache(maxsize=1)
def wordlist() -> frozenset[str]:
    with gzip.open(WORDLIST, "rt", encoding="utf-8") as handle:
        return frozenset(line.strip() for line in handle if line.strip())


@dataclass
class PageScore:
    score: float
    parts: dict[str, float] = field(default_factory=dict)
    short: bool = False
    empty: bool = False
    chars: int = 0


def _scale(value: float, low: float, high: float) -> float:
    return max(0.0, min(1.0, (value - low) / (high - low)))


def _dictionary(text: str) -> float:
    tokens = _TOKEN.findall(text)
    lowers = [t for t in tokens if t.islower()]
    pool = lowers if len(lowers) >= 5 else [t.lower() for t in tokens]
    if not pool:
        return 0.0  # text with no word in it at all (symbols, bare digits) is not readable prose
    words = wordlist()
    return _scale(sum(1 for t in pool if t in words) / len(pool), 0.40, 0.90)


def _charset(text: str) -> float:
    total = len(text)
    ok = sum(1 for c in text if c.isalnum() or c.isspace() or c in _PUNCT)
    bad = len(_BAD.findall(text))
    runs = sum(len(m.group(0)) for m in _RUN.finditer(text))
    nonspace = sum(1 for c in text if not c.isspace())
    alnum_share = sum(1 for c in text if c.isalnum()) / max(1, nonspace)
    base = _scale(ok / total - 5 * bad / total - runs / total, 0.50, 0.95)
    return base * _scale(alnum_share, 0.25, 0.65)  # a page of punctuation is not text


def _lines(lines: list[str]) -> float | None:
    real = [ln.strip() for ln in lines if ln.strip()]
    if len(real) < 3:
        return None
    good = sum(1 for ln in real if len(ln.split()) >= 2 or len(ln) >= 10)
    tiny = sum(1 for ln in real if len(ln.split()) == 1 and len(ln) <= 2)
    share = good / len(real) - max(0.0, tiny / len(real) - 0.30)
    return _scale(share, 0.30, 0.90)


def score_lines(lines: list[str], confidences: list[float] | None = None) -> PageScore:
    text = "\n".join(lines)
    stripped = "".join(text.split())
    if not stripped:
        return PageScore(0.0, empty=True)
    charset = _charset(text)
    if len(stripped) < SHORT_PAGE_CHARS:
        return PageScore(charset, {"charset": charset}, short=True, chars=len(stripped))
    parts: dict[str, float] = {"charset": charset}
    parts["dictionary"] = _dictionary(text)
    line_part = _lines(lines)
    if line_part is not None:
        parts["lines"] = line_part
    if confidences:
        parts["confidence"] = _scale(sum(confidences) / len(confidences), 0.50, 0.95)
    weight = sum(WEIGHTS[k] for k in parts)
    score = sum(WEIGHTS[k] * v for k, v in parts.items()) / weight
    return PageScore(
        round(score, 4), {k: round(v, 4) for k, v in parts.items()}, chars=len(stripped)
    )

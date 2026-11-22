"""Main text and publication date from a captured HTML body (trafilatura and htmldate)."""

from __future__ import annotations

import logging
from importlib import metadata

import htmldate
import trafilatura

log = logging.getLogger(__name__)

TRAFILATURA_VERSION = metadata.version("trafilatura")


def extract(body: bytes, url: str | None) -> tuple[str | None, str | None]:
    """``(text, publication date)``; either may be None. Never raises on malformed HTML."""
    try:
        text = trafilatura.extract(
            body, url=url, include_comments=False, include_tables=False, no_fallback=False
        )
    except Exception as exc:  # the extractor is third-party code over arbitrary web pages
        log.warning("trafilatura failed: %s", type(exc).__name__)
        text = None
    try:
        html = body.decode("utf-8", errors="replace")
        date = htmldate.find_date(html, url=url, extensive_search=False, original_date=True)
    except Exception as exc:
        log.warning("htmldate failed: %s", type(exc).__name__)
        date = None
    return (text.strip() if text else None), date

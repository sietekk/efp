"""Ballotpedia public pages: a read-only identity check for a public figure.

``lookup(ctx, name, state)`` fetches the person's public page through the capture layer (a low
budget: ``ballotpedia.org`` 0.2/s) and reads office, party and years from the page's infobox. The
Ballotpedia API stays excluded (sales-gated, pending the academic-access question). The name is a
public figure's, taken from an already-located record; this is confirmation, not discovery.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import quote

from lxml import html

from efp.capture import CaptureError
from efp.reference.states import STATES
from efp.types import Context

BASE = "https://ballotpedia.org"


@dataclass
class Page:
    url: str
    found: bool
    title: str = ""
    offices: list[str] = field(default_factory=list)
    party: str = ""
    years: list[int] = field(default_factory=list)
    state: str = ""
    capture_id: int | None = None
    reason: str = ""


def page_url(name: str) -> str:
    slug = re.sub(r"\s+", "_", name.strip())
    return f"{BASE}/{quote(slug, safe='_.,()-')}"


def parse(text: str, url: str, state: str | None = None) -> Page:
    """Office, party and years from a Ballotpedia person page's infobox and headings."""
    tree = html.fromstring(text)
    title = " ".join(tree.xpath("string(//h1[1])").split())
    if not title or "does not have an article" in text.lower():
        return Page(url, False, reason="no article")
    info = tree.xpath("//div[contains(@class, 'infobox')]")
    box = " ".join(" ".join(info[0].itertext()).split()) if info else ""
    offices = [
        " ".join(e.text_content().split())
        for e in tree.xpath(
            "//div[contains(@class, 'infobox')]//div[contains(@class, 'office-title')]"
        )
        if e.text_content().strip()
    ]
    party_match = re.search(
        r"\b(Democratic|Republican|Libertarian|Green|Independent|Nonpartisan)\b", box
    )
    years = sorted({int(y) for y in re.findall(r"\b(19[89]\d|20[0-3]\d)\b", box)})
    found_state = ""
    if state and STATES.get(state.upper()) and STATES[state.upper()] in box:
        found_state = state.upper()
    return Page(
        url, True, title, offices, party_match.group(1) if party_match else "", years, found_state
    )


def lookup(ctx: Context, name: str, state: str | None = None, *, row_id: int | None = None) -> Page:
    """Fetch and read one person's public page. A page that is not there is ``found = False``."""
    capture = ctx.capture
    if capture is None:
        raise RuntimeError("the capture client is required")
    url = page_url(name)
    try:
        record = capture.fetch(url, row_id=row_id, stage="reference", kind="page")
    except CaptureError as exc:
        return Page(url, False, reason=str(exc))
    if record.http_status == 404:
        return Page(url, False, capture_id=record.capture_id, reason="HTTP 404")
    if not record.ok:
        return Page(url, False, capture_id=record.capture_id, reason=f"HTTP {record.http_status}")
    page = parse(capture.read_body(record).decode("utf-8", errors="replace"), url, state)
    page.capture_id = record.capture_id
    return page

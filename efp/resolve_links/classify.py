"""Classification of a fetched page: is it a real page, or a dead one wearing a 200?

A link is ``live`` only when the final page was fetched live, answered 2xx and has a non-trivial
body. Parked domains, shortener error pages and soft 404s (a deep URL that now redirects to the
site root) are *dead pages*: their bodies are still captured, but they do not make a link live.
Relevance is never judged here, only whether there is a page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

MIN_TEXT_CHARS = 200  # an HTML page with less main text than this is a stub or an error template
MIN_BYTES = 512  # a non-HTML body (a PDF, an image) smaller than this is an error stub

_PARKED = re.compile(
    r"this domain (?:name )?(?:is|may be) for sale|domain (?:is )?for sale|buy this domain|"
    r"domain has expired|parked (?:free|domain|at)|sedo domain parking|hugedomains|"
    r"this web ?page is parked|website coming soon|account suspended|"
    r"default web ?site page|index of /",
    re.I,
)
_SHORTENER_ERROR = re.compile(
    r"link (?:not found|has been removed|is invalid|doesn.?t exist)|"
    r"(?:page|link) (?:you are looking for )?(?:could not be found|does not exist)|"
    r"this link (?:has|was) (?:been )?(?:disabled|blocked|removed)|bitly.{0,40}not found",
    re.I,
)


# An error page says so in its title. (Matched against the title or a short body only: a long
# article that happens to quote "account suspended" is still an article.)
_ERROR_TITLE = re.compile(
    r"\b(?:404|page not found|not found|no longer available|page cannot be found|"
    r"page does not exist|error 40\d|file not found|access denied|forbidden)\b",
    re.I,
)
# Title forms that only an error page has (the whole title, not a phrase inside a headline).
_ERROR_TITLE_STRICT = re.compile(
    r"^\W*(?:error\s*)?(?:40[0-9]|50[0-9])\b|^\W*(?:page|file)?\s*not found\W*(?:[-|:].*)?$|"
    r"^\W*(?:access denied|forbidden)\W*$",
    re.I,
)
_HOME_SEGMENT = re.compile(r"(?:index|home|default|main)(?:\.\w+)?|[a-z]{2}(?:[-_][a-z]{2})?", re.I)
SHORT_BODY_CHARS = 1500  # error and parked phrases are trusted in the body only below this length
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_TAGS = re.compile(r"<(script|style)\b.*?</\1>|<[^>]+>", re.I | re.S)


def title_of(html: str) -> str:
    found = _TITLE.search(html)
    return re.sub(r"\s+", " ", found.group(1)).strip() if found else ""


def visible_text(html: str) -> str:
    """The text a reader would see, for when the main-text extractor returns nothing."""
    return re.sub(r"\s+", " ", _TAGS.sub(" ", html)).strip()


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str  # why it is not ok, or "ok"


def is_html(content_type: str | None) -> bool:
    return bool(content_type) and "html" in (content_type or "").lower()


def is_parked(text: str) -> bool:
    return bool(_PARKED.search(text))


def is_shortener_error(text: str) -> bool:
    return bool(_SHORTENER_ERROR.search(text))


def is_soft_404(original_url: str, final_url: str | None) -> bool:
    """A deep URL that ended up at the site root: the page it named is gone."""
    if not final_url:
        return False
    deep = urlsplit(original_url).path.strip("/")
    if _HOME_SEGMENT.fullmatch(deep):
        return False  # /index.html, /home, /en: canonicalisation, not a gone page
    final = urlsplit(final_url).path.strip("/")
    return bool(deep) and not final and not urlsplit(final_url).query


def judge(
    *,
    http_status: int | None,
    content_type: str | None,
    body_len: int,
    text: str | None,
    original_url: str,
    final_url: str | None,
    title: str = "",
) -> Verdict:
    """Is this a real page? ``text`` is the extracted main text for an HTML body."""
    if http_status is None:
        return Verdict(False, "no response")
    if not 200 <= http_status < 300:
        return Verdict(False, f"HTTP {http_status}")
    if is_soft_404(original_url, final_url):
        return Verdict(False, "redirected to the site root (the page is gone)")
    if is_html(content_type) or content_type is None:
        body_text = text or ""
        short = len(body_text) < SHORT_BODY_CHARS
        if is_parked(title) or (short and is_parked(body_text)):
            return Verdict(False, "parked or placeholder domain")
        if is_shortener_error(title) or (short and is_shortener_error(body_text)):
            return Verdict(False, "shortener error page")
        if _ERROR_TITLE_STRICT.search(title) or (
            short and (_ERROR_TITLE.search(title) or _ERROR_TITLE.search(body_text[:200]))
        ):
            return Verdict(False, "error page served with a 200")
        if len(body_text) < MIN_TEXT_CHARS:
            return Verdict(False, f"trivial body ({len(body_text)} characters of text)")
        return Verdict(True, "ok")
    if body_len < MIN_BYTES:
        return Verdict(False, f"trivial body ({body_len} bytes)")
    return Verdict(True, "ok")

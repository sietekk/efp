"""Record a state's cassette: one listing page (trimmed) and one unrelated release page.

``efp court ag record <state>`` fetches through the capture layer and writes
``tests/cassettes/ag/<state>/{index.html,release.html,meta.json}``. The listing is cut to a few
items; the release is chosen from them by title (a company, a settlement, a rule: not a person), so
a cassette holds nothing about the dataset's subjects.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from efp.capture import CaptureError
from efp.court.adapters.ag.base import StateAdapter
from efp.court.located import cap
from efp.types import Context

SAFE_TITLE = re.compile(
    r"settle|lawsuit|sues|coalition|antitrust|consumer|rule|compan|opinion|comment|petition|"
    r"merger|privacy|amicus|corporation|utility|rate",
    re.I,
)


_TOKEN = re.compile(
    r"""((?:theme_token|authenticity_token|csrf[-_]?token|form_build_id|form_token)["']?"""
    r"""\s*[:=]\s*["'])[^"']{6,}(["'])""",
    re.I,
)
_TOKEN_VALUE = re.compile(
    r"""(name=["']?(?:authenticity_token|csrf[-_]?token|form_build_id|form_token)["']?\s*value=["'])"""
    r"""[^"']{6,}(["'])""",
    re.I,
)


_META = re.compile(
    r"""(<meta\s+name=["'](?:google-site-verification|msvalidate\.01|yandex-verification)["']"""
    r"""\s+content=["'])[^"']+(["'])""",
    re.I,
)
_HIDDEN = re.compile(
    r"""(<input\s+type=["']hidden["']\s+name=["'](?:__\w+|csrf\w*|authenticity_token)["'][^>]*"""
    r"""value=["'])[^"']+(["'])""",
    re.I,
)
_INTEGRITY = re.compile(r"""(integrity=["'])sha\d+-[^"']+(["'])""", re.I)
_DRUPAL_JSON = re.compile(
    r"""(<script type="application/json" data-drupal-selector="drupal-settings-json">)"""
    r""".*?(</script>)""",
    re.S,
)


def redact(html_text: str) -> str:
    """Page tokens are replaced (CSRF and view-state values, site-verification codes, integrity
    hashes, the CMS settings blob): they are not ours and not for the repository, and the secret
    scanner cannot tell them from ours."""
    out = _TOKEN_VALUE.sub(r"\1REDACTED\2", _TOKEN.sub(r"\1REDACTED\2", html_text))
    out = _META.sub(r"\1REDACTED\2", _HIDDEN.sub(r"\1REDACTED\2", out))
    out = _INTEGRITY.sub(r"\1REDACTED\2", out)
    return _DRUPAL_JSON.sub(r"\1{}\2", out)


def trim(html_text: str, marker: str, keep: int) -> str:
    """The page up to the start of item ``keep + 1`` (enough HTML for the parsers)."""
    starts = [m.start() for m in re.finditer(re.escape(marker), html_text)]
    return html_text[: starts[keep]] if len(starts) > keep else html_text


def record(ctx: Context, cls: type[StateAdapter], root: Path, keep: int = 6) -> dict[str, str]:
    adapter = cls(ctx)
    try:
        listing = adapter.get(adapter.listing_url(adapter.first_cursor()))
        if not listing.ok:
            raise CaptureError(f"{adapter.state}: the listing answered HTTP {listing.http_status}")
        html_text = adapter.body(listing)
        first = adapter.parse(trim(html_text, adapter.trim_marker, keep))
        if not first:
            raise CaptureError(f"{adapter.state}: no release parsed from the listing")
        pick = next((r for r in first if SAFE_TITLE.search(r.title)), first[0])
        page = adapter.fetch(pick)
        if not page.ok:
            raise CaptureError(f"{adapter.state}: the release answered HTTP {page.http_status}")
        folder = root / adapter.state.lower()
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "index.html").write_text(
            redact(trim(html_text, adapter.trim_marker, keep)), encoding="utf-8"
        )
        (folder / "release.html").write_text(
            redact(cap(ctx).read_body(page).decode("utf-8", errors="replace")), encoding="utf-8"
        )
        meta = {
            "state": adapter.state,
            "listing_url": adapter.listing_url(adapter.first_cursor()),
            "release_url": pick.url,
            "release_title": pick.title,
            "release_date": pick.date.isoformat() if pick.date else "",
            "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        (folder / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        return meta
    finally:
        adapter.close()

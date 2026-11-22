"""Ohio Attorney General: https://www.ohioattorneygeneral.gov/Media/News-Releases.

The listing is by month: ``/Media/News-Releases/<Month>-<YYYY>``, with ``ohio-news`` blocks holding
the title link, a summary and a ``news-date`` (``MM/DD/YYYY``). The month links are not in the
server-rendered page, but the URL pattern is regular, so the cursor is the month (``YYYY-MM``) and
the walk goes back one month at a time. A month with no page is a month with no releases.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from efp.court.adapters.ag import Release, register
from efp.court.adapters.ag.base import StateAdapter, clean, parse_date

FLOOR_YEAR = 2000
_MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]
_ITEM = re.compile(
    r'<div class="ohio-news">\s*<h2[^>]*>\s*<a href="([^"]+)">(.*?)</a>\s*</h2>.*?'
    r'<div class="news-summary">(.*?)</div>\s*<div class="news-date">\s*([\d/]+)',
    re.S,
)


def parse_listing(html_text: str, portal: str) -> list[Release]:
    return [
        Release(portal + path, clean(title), parse_date(day), "AG", clean(summary))
        for path, title, summary, day in _ITEM.findall(html_text)
    ]


@register
class Ohio(StateAdapter):
    state = "OH"
    portal = "https://www.ohioattorneygeneral.gov"
    search_mode = "archive_listing"
    wayback_fallback = False  # a month with no page is a month with no releases, not a gone index
    empty_limit = (
        12  # a year of empty months with nothing indexed is a broken walk, not a quiet year
    )
    trim_marker = '<div class="ohio-news">'

    def first_cursor(self) -> str:
        now = datetime.now(UTC)
        return f"{now.year}-{now.month:02d}"

    def listing_url(self, cursor: str) -> str:
        year, month = (int(part) for part in cursor.split("-"))
        return f"{self.portal}/Media/News-Releases/{_MONTHS[month - 1]}-{year}"

    def parse(self, html_text: str) -> list[Release]:
        return parse_listing(html_text, self.portal)

    def list_page(self, cursor: str) -> tuple[list[Release], str | None]:
        year, month = (int(part) for part in cursor.split("-"))
        url = self.listing_url(cursor)
        page = self.listing(url)
        if page is None:
            # A month with no page has no releases, but an edge can answer 404 for a page that
            # exists: ask once more, past the cache, before accepting the month as empty.
            self._fresh = True
            try:
                page = self.listing(url)
            finally:
                self._fresh = False
        releases = self.parse(page) if page else []
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
        return releases, (f"{year}-{month:02d}" if year >= FLOOR_YEAR else None)

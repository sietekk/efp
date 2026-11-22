"""Illinois Attorney General: https://illinoisattorneygeneral.gov/news-room/.

One page lists every story it holds (`a.news-item` with a ``<time datetime>`` and the title in a
``<p>``), newest first. At the time of writing it reaches back only to 2024, so a row whose window
starts earlier is reported as out of reach, not as a negative (see ``StateAdapter.out_of_reach``).
"""

from __future__ import annotations

import re

from efp.court.adapters.ag import Release, register
from efp.court.adapters.ag.base import StateAdapter, clean, parse_date

_ITEM = re.compile(
    r'<a class="news-item[^"]*" href="(/news/story/[^"]+)"[^>]*>\s*'
    r'<time datetime="(\d{4}-\d{2}-\d{2})[^"]*">[^<]*</time>\s*<p>(.*?)</p>',
    re.S,
)


def parse_listing(html_text: str, portal: str) -> list[Release]:
    return [
        Release(portal + path, clean(title), parse_date(day), "AG", "")
        for path, day, title in _ITEM.findall(html_text)
    ]


@register
class Illinois(StateAdapter):
    state = "IL"
    portal = "https://illinoisattorneygeneral.gov"
    search_mode = "archive_listing"
    trim_marker = '<a class="news-item'

    def listing_url(self, cursor: str) -> str:
        return f"{self.portal}/news-room/"

    def parse(self, html_text: str) -> list[Release]:
        return parse_listing(html_text, self.portal)

    def list_page(self, cursor: str) -> tuple[list[Release], str | None]:
        page = self.listing(self.listing_url(cursor))
        releases = self.parse(page) if page else []
        return releases, None  # a single page holds the whole archive

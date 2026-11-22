"""Texas Attorney General: https://www.texasattorneygeneral.gov/news/releases (Drupal).

A date-sorted listing, ten releases a page, ``?page=N`` from 0. Each item is an ``<h4>`` holding the
link ``/news/releases/slug`` and the title, a ``<p>`` summary, and a ``<p class="meta">`` with
``Month DD, YYYY | Press Release``.
County and district attorneys are separate offices with their own sites; they are added when the
domain table (``efp links report``) shows which occur in the data.
"""

from __future__ import annotations

import re

from efp.court.adapters.ag import Release, register
from efp.court.adapters.ag.base import StateAdapter, clean, parse_date

_ITEM = re.compile(
    r'<h4[^>]*>\s*<a href="(/news/releases/[^"]+)">(.*?)</a>\s*</h4>\s*<p[^>]*>(.*?)</p>\s*'
    r'<p class="meta[^"]*">\s*([A-Z][a-z]+ \d{1,2}, \d{4})',
    re.S,
)


def parse_listing(html_text: str, portal: str) -> list[Release]:
    return [
        Release(portal + path, clean(title), parse_date(day), "AG", clean(summary))
        for path, title, summary, day in _ITEM.findall(html_text)
    ]


@register
class Texas(StateAdapter):
    state = "TX"
    portal = "https://www.texasattorneygeneral.gov"
    search_mode = "archive_listing"
    trim_marker = '<div class="m-b-3">'

    def listing_url(self, cursor: str) -> str:
        return f"{self.portal}/news/releases?page={cursor}"

    def parse(self, html_text: str) -> list[Release]:
        return parse_listing(html_text, self.portal)

    def list_page(self, cursor: str) -> tuple[list[Release], str | None]:
        page = self.listing(self.listing_url(cursor))
        releases = self.parse(page) if page else []
        return releases, (str(int(cursor) + 1) if releases else None)

"""Texas Attorney General: https://www.texasattorneygeneral.gov/news/releases (Drupal).

A date-sorted listing, ten releases a page, ``?page=N`` from 0. Each item is an ``<h4>`` holding the
link ``/news/releases/slug`` and the title, a ``<p>`` summary, and a ``<p class="meta">`` with
``Month DD, YYYY | Press Release``.
County and district attorneys are separate offices with their own sites; they are added when the
domain table (``efp links report``) shows which occur in the data.
"""

from __future__ import annotations

import re

from efp.capture import CaptureError
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
        url = self.listing_url(cursor)
        page = self.listing(url)
        if page is None:
            return [], None  # 404/410: the page is gone, which is the end
        releases = self.parse(page)
        if not releases:
            # a 200 that holds no release is not the end of an archive: it is a layout change or
            # an interstitial, and must not be recorded as "finished"
            raise CaptureError(
                f"{url}: parsed to no releases (a layout change or a challenge page?)"
            )
        if 'rel="next"' in page:
            return releases, str(int(cursor) + 1)
        if "pager" not in page:
            raise CaptureError(f"{url}: no pager found, so the end of the archive cannot be told")
        return releases, None  # a pager with no next link: the last page

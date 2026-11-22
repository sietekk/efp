"""California Attorney General: https://oag.ca.gov/media/news (Drupal views).

Thirty releases a page, ``?page=N`` from 0, newest first. Each ``views-row`` has the title link
(``/news/press-releases/slug``) and a ``dc:date`` span whose ``content`` is the ISO date. The
site's published Crawl-delay is 10 seconds, so ``rate_limits."oag.ca.gov"`` is 0.08 (80% of
0.1/s). District attorneys are added when the domain table shows which occur in the data.
"""

from __future__ import annotations

import re

from efp.capture import CaptureError
from efp.court.adapters.ag import Release, register
from efp.court.adapters.ag.base import StateAdapter, clean, parse_date

_LINK = re.compile(r'<a href="(/news/press-releases/[^"]+)">(.*?)</a>', re.S)
_DATE = re.compile(r'content="(\d{4}-\d{2}-\d{2})')


def parse_listing(html_text: str, portal: str) -> list[Release]:
    out = []
    for chunk in html_text.split("views-row views-row-")[1:]:
        link, day = _LINK.search(chunk), _DATE.search(chunk)
        if link:
            out.append(
                Release(
                    portal + link.group(1),
                    clean(link.group(2)),
                    parse_date(day.group(1)) if day else None,
                    "AG",
                    "",
                )
            )
    return out


@register
class California(StateAdapter):
    state = "CA"
    portal = "https://oag.ca.gov"
    search_mode = "archive_listing"
    trim_marker = "views-row views-row-"

    def listing_url(self, cursor: str) -> str:
        return f"{self.portal}/media/news?page={cursor}"

    def parse(self, html_text: str) -> list[Release]:
        return parse_listing(html_text, self.portal)

    def list_page(self, cursor: str) -> tuple[list[Release], str | None]:
        url = self.listing_url(cursor)
        page = self.listing(url)
        if page is None:
            if cursor == self.first_cursor():
                return [], None  # no first page: the caller decides what an empty archive means
            # A 404 in the middle of a walk is not the end of an archive (an edge or CDN can
            # answer one for any page): only the pager says where an archive ends.
            raise CaptureError(
                f"{url}: HTTP 404 in the middle of the walk (the pager showed a next page)"
            )
        releases = self.parse(page)
        if not releases:
            raise CaptureError(
                f"{url}: parsed to no releases (a layout change or a challenge page?)"
            )
        if "pager-next" in page:
            return releases, str(int(cursor) + 1)
        if "pager" not in page:
            raise CaptureError(f"{url}: no pager found, so the end of the archive cannot be told")
        return releases, None  # a pager with no next link: the last page

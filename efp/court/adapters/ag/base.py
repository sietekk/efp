"""The shared machinery of the state adapters (the warn-scraper idea: one small module per state
on top of a common fetch, cache and retry layer).

A state module only says where its release index lives and how one page of it parses
(:meth:`StateAdapter.list_page`). Everything else is here:

* **Fetching** is the capture layer (budgets, retries, WARC, a ``captures`` row per request).
* **The index** is a local sqlite file per state under ``paths.bulk`` (``ag_<state>.sqlite``),
  filled newest-first by a resumable harvest, so a state's index is fetched once, not once per
  row. A row reads only what is already indexed; it may fetch a few more pages
  (``court.ag_harvest_pages_per_row``) and ``efp court ag harvest`` walks the rest in one go.
* **Freshness**: once per run the walk is repeated from the newest page until it reaches a release
  the index already holds, so releases published after the first harvest are seen.
* **Reach**: when an archive runs out before a row's window, that is recorded as the archive's reach
  (``out_of_reach``), not as a negative for the row.
* **Gone indexes**: a 404/410 on a listing falls back to the Wayback copy of it.
"""

from __future__ import annotations

import html
import re
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import ClassVar

from efp.capture import CaptureError, CaptureRecord
from efp.court.adapters.ag import Release
from efp.court.doj import ELECTION_TERMS
from efp.court.located import STAGE, cap
from efp.types import Context

DATE_FORMATS = ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%Y-%m-%d")
_TAGS = re.compile(r"<[^>]+>")
_INVISIBLE = dict.fromkeys(map(ord, "­​‌‍﻿"))


class ListingFailed(CaptureError):
    """A listing page that answered with an error status (after the capture layer's retries)."""

    def __init__(self, message: str, status: int | None) -> None:
        super().__init__(message)
        self.status = status


_TRANSIENT = re.compile(r"ConnectionError|Timeout|timed out|reset|budget", re.I)


def is_transient(exc: Exception) -> bool:
    """Is this a failure that says nothing lasting about the site (a throttle, a 5xx, a dropped
    connection, a spent budget)? Such a failure is retried; it never changes a state's mode."""
    if isinstance(exc, ListingFailed) and exc.status is not None:
        return exc.status == 429 or exc.status >= 500
    return bool(_TRANSIENT.search(str(exc)))


class SiteBlocked(CaptureError):
    """The site refuses automated requests (HTTP 401 or 403). Recorded as ``manual`` with the
    reason; the harvester does not try to get around it."""


def clean(fragment: str) -> str:
    """Visible text of an HTML fragment: tags dropped, entities decoded, soft hyphens removed."""
    text = html.unescape(_TAGS.sub(" ", fragment)).translate(_INVISIBLE)
    return re.sub(r"\s+", " ", text).strip()


def parse_date(text: str) -> date | None:
    """A date as the listings write it (``October 09, 2026``, ``11/20/2026``, ISO)."""
    text = (text or "").strip()
    for form in DATE_FORMATS:
        try:
            return datetime.strptime(text[:10] if form == "%Y-%m-%d" else text, form).date()
        except ValueError:
            continue
    return None


class AgIndex:
    """One state's local release index and the progress of its harvest."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.executescript(
            "CREATE TABLE IF NOT EXISTS releases (url TEXT PRIMARY KEY, title TEXT, date TEXT, "
            "office TEXT, snippet TEXT);"
            "CREATE TABLE IF NOT EXISTS progress (id INTEGER PRIMARY KEY CHECK (id = 1), "
            "cursor TEXT, oldest TEXT, done INTEGER NOT NULL DEFAULT 0, "
            "pages INTEGER NOT NULL DEFAULT 0);"
            "CREATE TABLE IF NOT EXISTS newest (id INTEGER PRIMARY KEY CHECK (id = 1), "
            "newest TEXT NOT NULL);"
        )

    def close(self) -> None:
        self.conn.close()

    def add(self, releases: list[Release]) -> None:
        self.conn.executemany(
            "INSERT OR IGNORE INTO releases (url, title, date, office, snippet) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (r.url, r.title, r.date.isoformat() if r.date else None, r.office, r.snippet)
                for r in releases
            ],
        )
        self.conn.commit()

    def has(self, url: str) -> bool:
        return (
            self.conn.execute("SELECT 1 FROM releases WHERE url = ?", (url,)).fetchone() is not None
        )

    def count(self) -> int:
        return int(self.conn.execute("SELECT count(*) FROM releases").fetchone()[0])

    def between(self, first_year: int, last_year: int) -> list[Release]:
        rows = self.conn.execute(
            "SELECT url, title, date, office, snippet FROM releases "
            "WHERE date >= ? AND date < ? ORDER BY date",
            (f"{first_year:04d}", f"{last_year + 1:04d}"),
        ).fetchall()
        return [Release(u, t, date.fromisoformat(d) if d else None, o, s) for u, t, d, o, s in rows]

    def progress(self) -> tuple[str | None, str | None, bool, int]:
        """(cursor of the next page, oldest date seen, feed exhausted, pages walked)."""
        found = self.conn.execute("SELECT cursor, oldest, done, pages FROM progress").fetchone()
        return (
            (None, None, False, 0)
            if found is None
            else (found[0], found[1], bool(found[2]), found[3])
        )

    def started(self) -> bool:
        return self.conn.execute("SELECT 1 FROM progress").fetchone() is not None

    def save_progress(self, cursor: str | None, oldest: str | None, done: bool, pages: int) -> None:
        updated = self.conn.execute(
            "UPDATE progress SET cursor = ?, oldest = ?, done = ?, pages = ? WHERE id = 1",
            (cursor, oldest, int(done), pages),
        ).rowcount
        if not updated:
            self.conn.execute(
                "INSERT INTO progress (id, cursor, oldest, done, pages) VALUES (1, ?, ?, ?, ?)",
                (cursor, oldest, int(done), pages),
            )
        self.conn.commit()

    def newest(self) -> str | None:
        found = self.conn.execute("SELECT newest FROM newest WHERE id = 1").fetchone()
        return str(found[0]) if found else None

    def note_newest(self, releases: list[Release]) -> None:
        dated = [r.date.isoformat() for r in releases if r.date]
        if not dated:
            return
        known = self.newest()
        best = max(dated) if known is None else max(max(dated), known)
        if not self.conn.execute("UPDATE newest SET newest = ? WHERE id = 1", (best,)).rowcount:
            self.conn.execute("INSERT INTO newest (id, newest) VALUES (1, ?)", (best,))
        self.conn.commit()


class StateAdapter:
    """One state's adapter: the declared facts, plus the page parser a subclass supplies."""

    state: ClassVar[str]
    portal: ClassVar[str]  # the office's site
    search_mode: ClassVar[str] = (
        "archive_listing"  # search_form | archive_listing | rss | sitemap | none
    )
    mode: ClassVar[str] = "automated"  # automated | manual
    reason: ClassVar[str] = ""  # why a manual state is manual
    office: ClassVar[str] = "AG"
    wayback_fallback: ClassVar[bool] = True
    trim_marker: ClassVar[str] = ""  # what starts one item in a listing (for recorded cassettes)
    # Consecutive empty pages tolerated while nothing at all is indexed. Beyond it the archive is
    # not "empty": the page layout changed, or a challenge page stands in front (OH walks months,
    # so a run of empty ones is normal for a while).
    empty_limit: ClassVar[int] = 1

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.config = ctx.config
        self._fresh = False  # bypass the response cache (the freshness walk)
        self.index = AgIndex(self.config.paths.bulk / f"ag_{self.state.lower()}.sqlite")

    @property
    def adapter_module(self) -> str:
        return type(self).__module__

    def close(self) -> None:
        self.index.close()

    # -- what a state module supplies ------------------------------------------------------------

    def first_cursor(self) -> str:
        return "0"

    def listing_url(self, cursor: str) -> str:
        """The URL of the listing page at ``cursor``."""
        raise NotImplementedError  # pragma: no cover

    def parse(self, html_text: str) -> list[Release]:
        """The releases one listing page holds."""
        raise NotImplementedError  # pragma: no cover

    def list_page(self, cursor: str) -> tuple[list[Release], str | None]:
        """One listing request: its releases and the cursor of the next page (None at the end)."""
        raise NotImplementedError  # pragma: no cover  (every automated adapter overrides this)

    # -- fetching --------------------------------------------------------------------------------

    def get(self, url: str, *, row_id: int | None = None) -> CaptureRecord:
        """The listing page at ``url``: live; the Wayback copy if the live page is gone."""
        record: CaptureRecord = cap(self.ctx).fetch(
            url, row_id=row_id, stage=STAGE, kind="page", no_cache=self._fresh
        )
        if record.http_status in (401, 403):
            raise SiteBlocked(f"{self.portal}: HTTP {record.http_status} to automated requests")
        if record.http_status in (404, 410) and self.wayback_fallback:
            stamp = datetime.now(UTC).strftime("%Y%m%d")
            archived: CaptureRecord = cap(self.ctx).fetch(
                f"https://web.archive.org/web/{stamp}id_/{url}",
                row_id=row_id,
                stage=STAGE,
                kind="api",
                record_url=url,
                tool="wayback",
            )
            if archived.ok:
                return archived
        return record

    def listing(self, url: str) -> str | None:
        """The listing page's HTML, or None for a page that is genuinely not there (404/410).

        Any other failure (a 429 or 5xx after the retries) raises: an unreadable page is not an
        empty one, and a harvest must never record an archive as finished because a fetch failed."""
        record = self.get(url)
        if record.ok:
            return self.body(record)
        if record.http_status in (404, 410):
            return None
        raise ListingFailed(f"{url}: HTTP {record.http_status}", record.http_status)

    def body(self, record: CaptureRecord) -> str:
        return str(cap(self.ctx).read_body(record).decode("utf-8", errors="replace"))

    def fetch(self, release: Release, *, row_id: int | None = None) -> CaptureRecord:
        """Capture the release page itself."""
        record: CaptureRecord = cap(self.ctx).fetch(
            release.url, row_id=row_id, stage=STAGE, kind="page"
        )
        return record  # a refusal here is one release's failure, not a verdict on the site

    # -- the index -------------------------------------------------------------------------------

    def _walk(self, cursor: str, row_id: int | None) -> tuple[list[Release], str | None]:
        releases, following = self.list_page(cursor)
        self.index.add(releases)
        return releases, following

    def harvest(
        self, until_year: int, row_id: int | None = None, *, max_pages: int | None = None
    ) -> int:
        """Page the archive newest-first into the index until it is older than ``until_year``.

        Resumable. Inside a row it fetches at most ``court.ag_harvest_pages_per_row`` pages;
        ``efp court ag harvest`` passes a larger budget."""
        cursor, oldest, done, pages = self.index.progress()
        if done or (oldest and int(oldest[:4]) < until_year):
            return 0
        cursor = cursor or self.first_cursor()
        cfg = self.config.court
        budget = max_pages if max_pages is not None else cfg.ag_harvest_pages_per_row
        fetched = 0
        empty = 0
        while cursor is not None and fetched < budget and pages < cfg.ag_harvest_max_pages:
            releases, following = self._walk(cursor, row_id)
            fetched += 1
            pages += 1
            if releases:
                empty = 0
            elif self.index.count() == 0:
                empty += 1
                if empty >= self.empty_limit or following is None:
                    # Nothing indexed and nothing on the page: not an archive that has ended but a
                    # page that did not parse (a layout change, an interstitial). Never recorded
                    # as finished.
                    raise CaptureError(
                        f"{self.state}: {self.listing_url(cursor)} parsed to no releases and "
                        "nothing is indexed yet (did the page layout change?)"
                    )
            self.index.note_newest(releases)
            dated = [r.date.isoformat() for r in releases if r.date]
            if dated:
                oldest = min([*dated, *([oldest] if oldest else [])])
            cursor = following
            self.index.save_progress(cursor, oldest, cursor is None, pages)
            if cursor is None or (oldest and int(oldest[:4]) < until_year):
                break
        return fetched

    def refresh_newest(self, row_id: int | None = None) -> int:
        """Pick up releases published since the harvest began (once per run): walk from the newest
        page until a page holds a release the index already has (an empty page, such as a month
        with no releases yet, does not end the walk)."""
        key = f"ag-refresh-{self.state}"
        if self.index.count() == 0 or cap(self.ctx).ran_this_run(key):
            return 0
        fetched = 0
        cursor: str | None = self.first_cursor()
        self._fresh = True
        try:
            while cursor is not None and fetched < self.config.court.ag_harvest_max_pages:
                releases, following = self.list_page(cursor)
                fetched += 1
                known = any(self.index.has(r.url) for r in releases)
                self.index.add(releases)
                self.index.note_newest(releases)
                if known:
                    break
                cursor = following
        finally:
            self._fresh = False
        cap(self.ctx).mark_ran(key)  # only a walk that finished counts: a failed one is retried
        return fetched

    def covered(self, first_year: int) -> bool:
        """Has the index reached back past ``first_year`` (or run out of archive)?"""
        _cursor, oldest, done, _pages = self.index.progress()
        return done or bool(oldest and int(oldest[:4]) < first_year)

    def out_of_reach(self, first_year: int) -> str | None:
        """Why this archive can never cover a window starting at ``first_year``, if it cannot."""
        _cursor, oldest, done, _pages = self.index.progress()
        if done and oldest and int(oldest[:4]) > first_year:
            return f"the {self.state} archive reaches back only to {oldest[:4]}"
        return None

    def probe(self) -> list[Release]:
        """A live check: the first page(s) of the archive, never from the cache. An empty first
        page is followed by the next one (a month with no releases yet is not a broken adapter)."""
        self._fresh = True
        try:
            cursor: str | None = self.first_cursor()
            for _ in range(3):
                if cursor is None:
                    break
                releases, cursor = self.list_page(cursor)
                if releases:
                    return releases
            return []
        finally:
            self._fresh = False

    # -- the protocol ----------------------------------------------------------------------------

    def search(self, name: str, year: int, window: int = 2) -> list[Release]:
        """Releases in the year window that could be about an election charge or name the subject.

        Listings carry a title and sometimes a snippet, not the body: a release is a candidate when
        its title or snippet has election wording or the surname; the caller reads and matches the
        full text."""
        first, last = year - window, year + window
        # ``applies`` already walked for this row (its page budget is spent): do not walk twice
        if not cap(self.ctx).run_state.pop(f"ag-walked-{self.state}", False):
            self.refresh_newest()
            self.harvest(first)
        surname = (name.split()[-1] if name.split() else "").lower()
        out = []
        for release in self.index.between(first, last):
            text = f"{release.title} {release.snippet}"
            if ELECTION_TERMS.search(text) or (len(surname) > 2 and surname in text.lower()):
                out.append(release)
        return out

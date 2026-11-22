"""DOJ press-release strategy: ``court.doj`` (spec section 4, strategy 1).

The API (``justice.gov/api/v1/press_releases.json``, no key, under 4 requests a second) silently
ignores its ``topic`` parameter, so topics are filtered here. Searching by title keyword is the
primary path. Whether the server honours the keyword is *checked*, not assumed: if the titles that
come back do not contain it, the parameter was ignored and the client falls back to harvesting the
date-sorted feed once into a local index and searching that. Every page, either way, is a capture.

A release is a located record only when it names the subject in full (given name or initial plus
surname) near the top, mentions an election charge, and is dated within a year of the row's year.
Whether it is the *same* person is for the identity stage; this strategy records what it matched on
and marks a match ``weak`` when the state is not corroborated.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from efp.capture import CaptureError
from efp.config import Config
from efp.court import identifiers as ident
from efp.court.located import STAGE, CourtTask, Located, cap, store_text
from efp.court.names import NameParts, match_name
from efp.modules import Availability, ModuleResult, Outcome
from efp.resolve_links import classify as page_classify
from efp.resolve_links import text as page_text
from efp.types import Context, Row

API = "https://www.justice.gov/api/v1/press_releases.json"
TITLE_PARAM = "parameters[title]"  # the title-keyword parameter (spec section 4)
COVERAGE_FROM = 2013  # the API is substantive from about 2013


def newest_indexed(config: Config) -> str | None:
    """The newest release date in the local harvest index (None when there is none): part of a
    row's input hash, so ``--recheck`` revisits rows when the feed's fresh end has moved."""
    path = config.paths.bulk / "doj_releases.sqlite"
    if not path.is_file():
        return None
    index = DojIndex(path)
    try:
        return index.newest()
    finally:
        index.close()


def harvest_target_year(config: Config) -> int:
    """How far back the harvest must reach for every row the DOJ module admits.

    The earliest row admitted has ``year + slack >= COVERAGE_FROM``, and its release window starts
    ``slack`` years before its year, so the harvest has to get past that first year."""
    return int(COVERAGE_FROM - 2 * config.court.doj_year_slack)


ELECTION_TERMS = re.compile(
    r"\b(voter|voting|vote|ballot|election|registration|absentee|polling)\w*", re.I
)
CASE_HEAD = 600  # a release names its subject in its title or opening
MAX_MATCHES = 3
MAX_TITLE_PAGES = 6  # pages of 50 per surname before giving up on an enormous result set
STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "PR": "Puerto Rico", "GU": "Guam", "VI": "Virgin Islands",
}  # fmt: skip
_STATE_BY_NAME = {name.lower(): abbreviation for abbreviation, name in STATES.items()}


def normalize_state(value: object) -> str | None:
    """A row's state as a postal abbreviation, or None when it is not one we can place.

    The workbook's state column is free text: besides "CT" it may hold a full name, "D.C.", blanks,
    "N/A", "Unknown", "Multiple", or anything else. Only a value that names a state or territory
    is used; everything else is unknown, never a filter that matches nothing."""
    text = str(value or "").strip()
    compact = text.replace(".", "").replace(" ", "").upper()
    if compact in STATES:
        return compact
    return _STATE_BY_NAME.get(re.sub(r"\s+", " ", text).lower())


DISTRICT = re.compile(
    r"\b(?:(Eastern|Western|Northern|Southern|Middle|Central)\s+)?District of "
    r"([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?)"
)


@dataclass(frozen=True)
class Release:
    uuid: str
    title: str
    url: str
    date: datetime | None
    body_html: str
    listing_capture_id: int | None = None

    @property
    def text(self) -> str:
        return page_classify.visible_text(self.body_html)


def parse_date(value: Any) -> datetime | None:
    """The API gives the date as a Unix timestamp string; accept ISO dates as well."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        if text.isdigit():
            return datetime.fromtimestamp(int(text), UTC)
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)
    except (ValueError, OverflowError, OSError):
        return None


def parse_releases(payload: Any, capture_id: int | None) -> list[Release]:
    results = (payload or {}).get("results") if isinstance(payload, dict) else None
    out: list[Release] = []
    for item in results or []:
        if not isinstance(item, dict):
            continue
        body = item.get("body") or ""
        out.append(
            Release(
                uuid=str(item.get("uuid") or item.get("url") or item.get("title") or ""),
                title=str(item.get("title") or ""),
                url=str(item.get("url") or ""),
                date=parse_date(item.get("date")),
                body_html=str(body),
                listing_capture_id=capture_id,
            )
        )
    return out


def honors_title(releases: list[Release], keyword: str) -> bool:
    """Did the server apply the title keyword? (It ignored ``topic``; do not assume ``title``.)"""
    if not releases:
        return False  # nothing came back: no evidence that the keyword was applied
    hits = sum(1 for r in releases if keyword.lower() in r.title.lower())
    return hits >= max(1, len(releases) // 2)


class DojIndex:
    """A local copy of releases seen so far, for filtering and for the harvest fallback."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.executescript(
            "CREATE TABLE IF NOT EXISTS releases (uuid TEXT PRIMARY KEY, date TEXT, title TEXT, "
            "url TEXT, body TEXT, capture_id INTEGER);"
            "CREATE TABLE IF NOT EXISTS harvest (id INTEGER PRIMARY KEY CHECK (id = 1), "
            "next_page INTEGER NOT NULL, oldest TEXT, done INTEGER NOT NULL DEFAULT 0);"
        )
        self.conn.execute(  # the newest release the date-sorted walk has seen
            "CREATE TABLE IF NOT EXISTS harvest_newest "
            "(id INTEGER PRIMARY KEY CHECK (id = 1), newest TEXT NOT NULL)"
        )

    def close(self) -> None:
        self.conn.close()

    def add(self, releases: list[Release]) -> None:
        self.conn.executemany(
            "INSERT OR IGNORE INTO releases (uuid, date, title, url, body, capture_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    r.uuid,
                    r.date.isoformat() if r.date else None,
                    r.title,
                    r.url,
                    r.body_html,
                    r.listing_capture_id,
                )
                for r in releases
            ],
        )
        self.conn.commit()

    def between(
        self, first_year: int, last_year: int, surnames: list[str] | None = None
    ) -> list[Release]:
        """Releases in the years, optionally only those that mention one of the surnames (so the
        caller never parses the HTML of releases that cannot be about the subject)."""
        where, args = "date >= ? AND date < ?", [f"{first_year:04d}", f"{last_year + 1:04d}"]
        if surnames:
            clause = []
            for name in surnames:
                like = (
                    "%"
                    + name.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    + "%"
                )
                clause.append("(lower(title) LIKE ? ESCAPE '\\' OR lower(body) LIKE ? ESCAPE '\\')")
                args += [like, like]
            where += " AND (" + " OR ".join(clause) + ")"
        rows = self.conn.execute(
            "SELECT uuid, title, url, date, body, capture_id FROM releases "  # noqa: S608
            f"WHERE {where} ORDER BY date",
            args,
        ).fetchall()
        return [
            Release(u, t, url, datetime.fromisoformat(d) if d else None, b, c)
            for u, t, url, d, b, c in rows
        ]

    def progress(self) -> tuple[int, str | None, bool]:
        found = self.conn.execute(
            "SELECT next_page, oldest, done FROM harvest WHERE id = 1"
        ).fetchone()
        return (0, None, False) if found is None else (int(found[0]), found[1], bool(found[2]))

    def save_progress(self, next_page: int, oldest: str | None, done: bool) -> None:
        updated = self.conn.execute(
            "UPDATE harvest SET next_page = ?, oldest = ?, done = ? WHERE id = 1",
            (next_page, oldest, int(done)),
        ).rowcount
        if not updated:
            self.conn.execute(
                "INSERT INTO harvest (id, next_page, oldest, done) VALUES (1, ?, ?, ?)",
                (next_page, oldest, int(done)),
            )
        self.conn.commit()

    def newest(self) -> str | None:
        """The newest release the date-sorted walk has indexed (the feed's fresh end)."""
        found = self.conn.execute("SELECT newest FROM harvest_newest WHERE id = 1").fetchone()
        return str(found[0]) if found and found[0] else None

    def note_newest(self, releases: list[Release]) -> None:
        dated = [r.date.isoformat() for r in releases if r.date]
        if not dated:
            return
        known = self.newest()
        best = max(dated) if known is None else max(max(dated), known)
        updated = self.conn.execute(
            "UPDATE harvest_newest SET newest = ? WHERE id = 1", (best,)
        ).rowcount
        if not updated:
            self.conn.execute("INSERT INTO harvest_newest (id, newest) VALUES (1, ?)", (best,))
        self.conn.commit()

    def count(self) -> int:
        return int(self.conn.execute("SELECT count(*) FROM releases").fetchone()[0])


TITLE_PROBES = (
    3  # consecutive probes that find no keyword in the titles before the search is dropped
)


class DojClient:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.config = ctx.config
        self.index = DojIndex(self.config.paths.bulk / "doj_releases.sqlite")
        # whether the server honours the title keyword is a property of the server, not of a row:
        # remembered on the capture client so it is probed once per run
        self.title_search_works: bool | None = cap(ctx).run_state.get("doj_title_works")
        self.title_capped = False  # a title search stopped on its page cap before the window

    def close(self) -> None:
        self.index.close()

    def _page(
        self, params: dict[str, str], row_id: int | None, *, fresh: bool = False
    ) -> list[Release]:
        record = cap(self.ctx).fetch(
            API, row_id=row_id, stage=STAGE, kind="api", params=params, no_cache=fresh
        )
        if not record.ok:
            raise CaptureError(f"DOJ API answered HTTP {record.http_status}")
        try:
            payload = json.loads(cap(self.ctx).read_body(record))
        except ValueError as exc:
            raise CaptureError("DOJ API did not return JSON") from exc
        releases = parse_releases(payload, record.capture_id)
        self.index.add(releases)
        return releases

    def _title_ignored(self) -> bool:
        return self.title_search_works is False

    def search_title(self, keyword: str, row_id: int | None, first_year: int) -> list[Release]:
        """Newest first, page by page, until the releases are older than ``first_year``."""
        found: list[Release] = []
        self.title_capped = False
        for page in range(MAX_TITLE_PAGES):
            params = {
                TITLE_PARAM: keyword,
                "pagesize": str(self.config.court.doj_pagesize),
                "sort": "date",
                "direction": "DESC",
                "page": str(page),
            }
            releases = self._page(params, row_id)
            if page == 0:
                if not releases:
                    # An empty answer proves nothing about the parameter (the name may simply
                    # be absent from titles): leave it undecided, and the caller reads the
                    # harvest index instead of calling this an authoritative miss.
                    return releases
                if honors_title(releases, keyword):
                    self.title_search_works = True
                    cap(self.ctx).run_state["doj_title_works"] = True
                    cap(self.ctx).run_state["doj_title_misses"] = (
                        0  # consecutive misses: a success resets
                    )
                else:
                    # One keyword that matched nothing in the titles is weak evidence the server
                    # ignores the parameter (it may match bodies, or the name may be absent from
                    # titles). Only several misses in a row, before it has ever been shown to
                    # honor the parameter, disable the title search for the run.
                    if self.title_search_works is not True:
                        misses = cap(self.ctx).run_state.get("doj_title_misses", 0) + 1
                        cap(self.ctx).run_state["doj_title_misses"] = misses
                        if misses >= TITLE_PROBES:
                            self.title_search_works = False
                            cap(self.ctx).run_state["doj_title_works"] = False
                    return releases
            found.extend(releases)
            dated = [r.date.year for r in releases if r.date]
            if len(releases) < self.config.court.doj_pagesize or (
                dated and min(dated) < first_year
            ):
                break
        else:
            # ran out of pages with every release still newer than the window: the title search
            # does not cover the row's years (a common surname), so the caller must not read its
            # silence as absence
            self.title_capped = True
        return found

    def refresh_newest(self, row_id: int | None) -> int:
        """Pick up releases published since the harvest began (once per run).

        The harvest walks backward from the newest page and resumes where it stopped, so it never
        sees what was published afterwards. Re-walk from page 0 until a page reaches a release the
        index already has, within the per-row page budget."""
        _page, _oldest, _done = self.index.progress()
        known = self.index.newest()
        if known is None or not cap(self.ctx).once_per_run("doj-refresh"):
            return 0  # nothing harvested yet (the harvest itself starts at the newest page)
        fetched = 0
        for number in range(self.config.court.doj_harvest_pages_per_row):
            params = {
                "pagesize": str(self.config.court.doj_pagesize),
                "sort": "date",
                "direction": "DESC",
                "page": str(number),
            }
            releases = self._page(params, row_id, fresh=True)  # never the cached copy of page 0
            fetched += 1
            self.index.note_newest(releases)
            if not releases or any(r.date and r.date.isoformat() <= known for r in releases):
                break  # back at what the index already holds
        return fetched

    def covered(self, first_year: int) -> bool:
        """Has the harvest reached back to ``first_year`` (or exhausted the feed)?"""
        _page, oldest, exhausted = self.index.progress()
        # "exhausted" is the feed having no older page; reaching some other row's window is not
        # coverage of this one's, so only the oldest date reached counts besides that
        return exhausted or bool(oldest and int(oldest[:4]) < first_year)

    def harvest(self, until_year: int, row_id: int | None, *, max_pages: int | None = None) -> int:
        """Page the date-sorted feed (newest first) into the index back to ``until_year``.

        Inside a row this fetches at most ``doj_harvest_pages_per_row`` pages (so no row holds the
        stage for hours: progress is kept and the next call carries on); ``efp court doj harvest``
        passes a larger budget to do the whole walk in one go."""
        page, oldest, exhausted = self.index.progress()
        if exhausted or (oldest and int(oldest[:4]) < until_year):
            return 0
        fetched = 0
        budget = max_pages if max_pages is not None else self.config.court.doj_harvest_pages_per_row
        while page < self.config.court.doj_harvest_max_pages and fetched < budget:
            params = {
                "pagesize": str(self.config.court.doj_pagesize),
                "sort": "date",
                "direction": "DESC",
                "page": str(page),
            }
            releases = self._page(params, row_id)
            self.index.note_newest(releases)
            fetched += 1
            page += 1
            dated = [r.date for r in releases if r.date]
            if dated:
                oldest = min(dated).isoformat()
            self.index.save_progress(page, oldest, not releases)  # done = the feed ran out
            if not releases or (oldest and int(oldest[:4]) < until_year):
                return fetched
        return fetched

    def candidates(
        self,
        parts: NameParts,
        first_year: int,
        last_year: int,
        row_id: int | None,
        accept: Callable[[Release], bool] | None = None,
    ) -> tuple[list[Release], str]:
        """Releases that could name the subject, and how they were found.

        The title search is the answer only when one of its releases survives ``accept`` (the
        caller's name match): a title that carries the surname may belong to someone else, while
        the subject is named only in a body, so otherwise the harvest is read as well."""
        # the subject's surname and the surnames of any aliases ("aka")
        keywords = list(dict.fromkeys(v.surname for v in parts.variants() if v.usable))
        capped = False
        found: list[Release] = []
        if self.title_search_works is not False:
            for keyword in keywords:
                found.extend(self.search_title(keyword, row_id, first_year))
                capped = capped or self.title_capped
                if self._title_ignored():
                    break
            unique = {r.uuid: r for r in found}
            in_window = [
                r for r in unique.values() if r.date and first_year <= r.date.year <= last_year
            ]
            if (
                self.title_search_works
                and not capped
                and in_window
                and (accept is None or any(accept(r) for r in in_window))
            ):
                return in_window, "title_search"
            found = (
                in_window if self.title_search_works else []
            )  # an ignored keyword proves nothing
        # The server ignores the title parameter, or a common surname filled the title search's
        # pages before the window: page the date-sorted feed back to the window instead (the
        # index is shared by every row) and read only releases that mention the surname.
        self.refresh_newest(row_id)  # the fresh end of the feed, once per run
        self.harvest(first_year, row_id)
        local = self.index.between(first_year, last_year, keywords)
        if found or capped:  # what the title search did return stays beside the harvest's
            merged = {r.uuid: r for r in found}
            merged.update({r.uuid: r for r in local})
            return list(merged.values()), "title_search+harvest"
        return local, "harvest"


@dataclass(frozen=True)
class ReleaseMatch:
    release: Release
    full_name: bool
    state_corroborated: bool
    variant: str
    span: str
    district: str | None
    initial_only: bool = False


def match_release(
    parts: NameParts, release: Release, *, row_year: int, row_state: str | None, slack: int
) -> ReleaseMatch | None:
    if release.date is None or abs(release.date.year - row_year) > slack:
        return None
    text = release.text
    head = f"{release.title}\n{text[:CASE_HEAD]}"
    named = match_name(parts, head)
    if named is None or not named.full:
        return None  # a surname alone is not enough to call a release the subject's
    if not ELECTION_TERMS.search(f"{release.title}\n{text}"):
        return None
    corroborated = mentions_state(f"{release.title}\n{text}", row_state)
    district = DISTRICT.search(f"{release.title}\n{text}")
    return ReleaseMatch(
        release, True, corroborated, named.variant, named.span,
        district.group(0) if district else None, named.initial_only,
    )  # fmt: skip


def mentions_state(text: str, abbreviation: str | None) -> bool:
    """Does the text place the case in this state? ("Virginia" is not "West Virginia"; a
    Washington dateline is not the state.)"""
    abbreviation = normalize_state(abbreviation)
    if not abbreviation:
        return False
    name = STATES.get(abbreviation, "")
    if name and re.search(
        rf"(?<!West )(?<!North )(?<!South )\b{re.escape(name)}\b(?!,? D\.?C)", text
    ):
        return True
    return bool(re.search(rf",\s*{re.escape(abbreviation)}\b", text))


def case_numbers(text: str) -> list[str]:
    return [f.value for f in ident.extract_identifiers(text) if f.kind == "case_number"]


def _release_text(ctx: Context, task: CourtTask, release: Release) -> tuple[str, int, int, str]:
    """(text, page capture id, text capture id, tool): the release page if reachable, else the
    body the API returned."""
    try:
        record = cap(ctx).fetch(release.url, row_id=task.row_id, stage=STAGE, kind="page")
        if record.ok:
            extracted, _date = page_text.extract(cap(ctx).read_body(record), record.url_final)
            if extracted:
                text_id = store_text(
                    ctx,
                    task.row_id,
                    parent_capture_id=record.capture_id,
                    url=release.url,
                    text=extracted,
                    tool="trafilatura",
                    version=page_text.TRAFILATURA_VERSION,
                )
                return extracted, record.capture_id, text_id, "page"
    except CaptureError:
        pass
    text = release.text
    parent = release.listing_capture_id or 0
    text_id = store_text(
        ctx,
        task.row_id,
        parent_capture_id=parent,
        url=release.url,
        text=text,
        tool="doj_release_text",
    )
    return text, parent, text_id, "api_body"


class CourtDoj:
    name = "court.doj"
    stage = STAGE

    def available(self, ctx: Context) -> Availability:
        return Availability.available()

    def applies(self, row: Row, ctx: Context) -> bool | str:
        task: CourtTask = row  # type: ignore[assignment]
        cs = task.cs
        if not cs.classification.federal_strategies_apply:
            return "classified as a state case"
        if not cs.parts.usable:
            return "no usable name"
        if not row.year:
            return "the row has no year: the release window cannot be built"
        if row.year + ctx.config.court.doj_year_slack < COVERAGE_FROM:
            return f"DOJ coverage starts about {COVERAGE_FROM}; the PIN reports cover earlier years"
        return True

    def run(self, row: Row, ctx: Context) -> ModuleResult:
        task: CourtTask = row  # type: ignore[assignment]
        cs = task.cs
        slack = ctx.config.court.doj_year_slack
        year = row.year or 0
        client = DojClient(ctx)
        try:
            releases, how = client.candidates(
                cs.parts,
                year - slack,
                year + slack,
                task.row_id,
                accept=lambda r: (
                    match_release(cs.parts, r, row_year=year, row_state=row.state, slack=slack)
                    is not None
                ),
            )
            covered = client.covered(year - slack)
            oldest = client.index.progress()[1]
        except CaptureError as exc:
            cs.notes.append(f"DOJ API: {exc}"[:200])
            return ModuleResult(Outcome.error, error=f"DOJ API: {exc}"[:200])
        finally:
            client.close()  # one local index connection per row would pile up over a run
        matches = [
            m
            for r in releases
            if (m := match_release(cs.parts, r, row_year=year, row_state=row.state, slack=slack))
        ]
        matches.sort(
            key=lambda m: (
                not m.state_corroborated,
                abs((m.release.date or datetime.now(UTC)).year - year),
            )
        )
        cs.flags["doj"] = {"found_by": how, "candidates": len(releases), "matched": len(matches)}
        if "harvest" in how and not covered:
            # The capped harvest stopped before the row's window: an absence here proves nothing.
            cs.flags["doj_coverage_gap"] = True
            cs.flags["doj"]["oldest_harvested"] = oldest
            cs.notes.append(
                f"DOJ harvest reaches back only to {oldest or 'nothing'}; the row's window starts "
                f"{year - slack}: run `efp court doj harvest`, then `efp run court --recheck`"
            )
        if not matches:
            return ModuleResult(Outcome.miss, wrote={"candidates": len(releases)})
        found_case = False
        for match in matches[:MAX_MATCHES]:
            try:
                text, page_id, text_id, source = _release_text(ctx, task, match.release)
            except CaptureError as exc:
                cs.notes.append(f"DOJ release not captured: {exc}"[:200])
                continue
            numbers = [
                f.value
                for f in ident.extract_identifiers(text, cs.parts)
                if f.kind == "case_number"
            ]
            several = len({v.lower() for v in case_numbers(text)}) > 1
            district = match.district
            host = urlsplit(match.release.url).hostname or ""
            item = Located(
                source_kind="doj_release",
                jurisdiction="US",
                court=district,
                case_number=numbers[0] if numbers else None,
                document_kind="press_release",
                url=match.release.url,
                strategy=self.name,
                capture_id=page_id or match.release.listing_capture_id,
                text=text,
                text_capture_id=text_id,
                basis={
                    "release_uuid": match.release.uuid,  # the stable identity of the record
                    "name_variant": match.variant,
                    "name_span": match.span,
                    "state_corroborated": match.state_corroborated,
                    "release_date": match.release.date.date().isoformat()
                    if match.release.date
                    else None,
                    "text_from": source,
                    "host": host,
                },
                weak=not match.state_corroborated or match.initial_only or several,
            )
            item.basis["initial_only"] = match.initial_only
            item.basis["document_has_several_case_numbers"] = several
            cs.located.append(item)
            found_case = found_case or (bool(numbers) and not item.weak)  # weak never stops it
        return ModuleResult(
            Outcome.hit if found_case else Outcome.miss, wrote={"matched": len(matches)}
        )

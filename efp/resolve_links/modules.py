"""The link-resolution ladder: five modules over one chain (``[stages.resolve_links]``).

The chain runs once per *link*, not per row. A link travels through the framework as a
:class:`LinkTask` (a ``Row`` that also carries the link's mutable :class:`LinkState`), so the
build 04 chain runner records each module's outcome unchanged. A module returns a *hit* only when
it ends the ladder with a body (status ``live`` or ``archived``); otherwise it returns a miss after
adding what it learned to the state (an expansion, a note, the evidence of a dead page), and the
next module takes over.

``expansion_method`` records how the final URL was learned; ``status`` records how a body was
obtained. For a URL that is not a shortener the "expansion" is the URL itself (method ``live``).
Everything fetched goes through ``ctx.capture``; every body is captured whether or not it helps.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

from efp import wayback
from efp.capture import CaptureError, CaptureRecord
from efp.modules import Availability, ModuleResult, Outcome, Registry
from efp.resolve_links import classify, text
from efp.resolve_links.parse import BITLY_HOSTS, SHORTENERS
from efp.types import Context, Row

STAGE = "resolve_links"
MAX_ARCHIVE_TRIES = 3  # archived copies fetched per link and source before giving up

_WAYBACK_PREFIX = re.compile(r"^https?://web\.archive\.org/web/\d+(?:[a-z]{2}_)?/", re.I)
_ORIG_LOCATION = "x-archive-orig-location"

# Hosts that are never a Bitly destination (the page's own furniture).
_BITLY_FURNITURE = (
    "bit.ly", "bitly.com", "bitly.is", "herit.ag", "w3.org", "google", "gstatic", "facebook.com",
    "twitter.com", "linkedin.com", "cloudfront.net", "googletagmanager",
)  # fmt: skip


@dataclass
class LinkState:
    """What the ladder has learned about one link so far."""

    expansion: str | None = None
    method: str | None = None  # how the expansion was learned
    status: str | None = None  # live | archived once a body is in hand
    capture_id: int | None = None  # the body that makes the link live or archived
    evidence_capture_id: int | None = None  # a dead page, kept as evidence if nothing better
    live_expanded: bool = False  # the live fetch itself produced the expansion
    robots_blocked: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def done(self) -> bool:
        return self.status is not None


class LinkTask(Row):
    """One link of one row, with its mutable ladder state."""

    link_id: int
    url_raw: str
    fetch_url: str
    has_scheme: bool
    host: str
    is_shortener: bool
    ladder: Any  # the LinkState (the Row's own "state" field is the US state)

    @property
    def st(self) -> LinkState:
        state: LinkState = self.ladder
        return state


# --- shared helpers -----------------------------------------------------------------------------


def _cap(ctx: Context) -> Any:
    if ctx.capture is None:
        raise CaptureError("no capture client in the context")
    return ctx.capture


def _db(ctx: Context) -> Any:
    if ctx.db is None:
        raise CaptureError("no database connection in the context")
    return ctx.db


def _note(task: LinkTask, message: str) -> None:
    task.st.notes.append(message)


def _body(ctx: Context, record: CaptureRecord) -> bytes:
    body: bytes = _cap(ctx).read_body(record)
    return body


def _fetch_page(ctx: Context, task: LinkTask, url: str) -> CaptureRecord | None:
    """Fetch a page live. None when nothing came back (the reason goes in the notes)."""
    try:
        record: CaptureRecord = _cap(ctx).fetch(url, row_id=task.row_id, stage=STAGE, kind="page")
    except CaptureError as exc:
        _note(task, f"no response from {urlsplit(url).hostname}: {exc}"[:200])
        return None
    return record


def _store_text(
    ctx: Context,
    task: LinkTask,
    parent: CaptureRecord,
    extracted: str | None,
    date: str | None,
) -> None:
    """Keep the extracted text as its own capture so later stages never re-parse the HTML."""
    if not extracted:
        return
    found = (
        _db(ctx)
        .execute(
            "SELECT 1 FROM captures WHERE tool = 'trafilatura' "
            "AND json_extract(headers_json, '$.parent_capture_id') = ? LIMIT 1",
            (parent.capture_id,),
        )
        .fetchone()
    )
    if found is not None:
        return
    _cap(ctx).store_bytes(
        extracted.encode("utf-8"),
        row_id=task.row_id,
        stage=STAGE,
        url_requested=parent.url_requested,
        url_final=parent.url_final,
        tool="trafilatura",
        tool_version=text.TRAFILATURA_VERSION,
        content_type="text/plain; charset=utf-8",
        headers={"parent_capture_id": parent.capture_id, "publication_date": date},
    )


def _judge(
    ctx: Context, task: LinkTask, record: CaptureRecord, original_url: str, *, check_redirect: bool
) -> tuple[classify.Verdict, str | None, str | None]:
    body = _body(ctx, record)
    extracted = date = None
    title = ""
    if classify.is_html(record.content_type) or record.content_type is None:
        extracted, date = text.extract(body, record.url_final)
        html = body.decode("utf-8", errors="replace")
        title = classify.title_of(html)
        # When the extractor finds no article, judge by what a reader would see.
        extracted_for_judging = extracted or classify.visible_text(html)
    else:
        extracted_for_judging = None
    verdict = classify.judge(
        http_status=record.http_status,
        content_type=record.content_type,
        body_len=len(body),
        text=extracted_for_judging,
        original_url=original_url,
        final_url=record.url_final if check_redirect else None,
        title=title,
    )
    return verdict, extracted, date


def _try_live(ctx: Context, task: LinkTask, url: str, original: str) -> ModuleResult:
    """Fetch ``url`` live and judge it. Used by the live step and for a learned expansion."""
    st = task.st
    record = _fetch_page(ctx, task, url)
    if record is None:
        return ModuleResult(Outcome.miss)
    if record.tool == "robots_disallow":
        st.robots_blocked = True
        _note(task, f"robots.txt disallows {urlsplit(url).hostname}: unreachable by policy")
        return ModuleResult(Outcome.miss)
    verdict, extracted, date = _judge(ctx, task, record, original, check_redirect=True)
    final = record.url_final or url
    redirected = urlsplit(final).hostname != urlsplit(url).hostname or final != url
    first_attempt = url == task.fetch_url
    final_host = (urlsplit(final).hostname or "").lower().removeprefix("www.")
    if verdict.ok and task.is_shortener and first_attempt and final_host in SHORTENERS:
        # A shortener that sends you to a shortener's own site (its error or marketing page)
        # has not told you where the link goes, however long that page is.
        verdict = classify.Verdict(False, "redirected to a shortener's own site")
    if verdict.ok:
        if st.expansion is None or first_attempt:
            st.expansion = final
            st.method = st.method or "live"
            st.live_expanded = st.live_expanded or first_attempt
    elif task.is_shortener and first_attempt and redirected and _names_a_page(final):
        # The shortener did redirect, to a deep address: that address is the expansion even though
        # it is dead now, so the archive is asked about it. A redirect to a site root, to a
        # shortener's own error page, or no redirect at all is not an expansion.
        st.expansion, st.method, st.live_expanded = final, "live", True
    # A plain URL whose page is dead keeps its own address as the archive target: where it
    # happened to redirect (a homepage, a login wall) is not the page the source meant.
    if verdict.ok:
        st.status = "live"
        st.capture_id = record.capture_id
        _store_text(ctx, task, record, extracted, date)
        return ModuleResult(Outcome.hit, wrote={"capture_id": record.capture_id})
    _note(task, f"live page is not usable: {verdict.reason}")
    st.evidence_capture_id = record.capture_id
    return ModuleResult(Outcome.miss)


def _names_a_page(url: str) -> bool:
    """A deep address on a host that is not a shortener: not a site root or an error page."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    return bool(parts.path.strip("/") or parts.query) and host not in SHORTENERS


def _try_expansion_live(ctx: Context, task: LinkTask) -> bool:
    """After the archive revealed where a shortener pointed, try that address live first."""
    st = task.st
    if st.expansion is None or st.done:
        return st.done
    result = _try_live(ctx, task, st.expansion, st.expansion)
    return result.outcome is Outcome.hit


def _closest(rows: list[wayback.CdxRow], year: int | None) -> list[wayback.CdxRow]:
    """Captures nearest the row's year first (earliest wins a tie)."""

    def distance(row: wayback.CdxRow) -> tuple[int, str]:
        captured = int(row.timestamp[:4]) if row.timestamp[:4].isdigit() else 0
        return (abs(captured - year) if year else 0, row.timestamp)

    return sorted(rows, key=distance)


def _cdx_near_year(
    ctx: Context,
    task: LinkTask,
    url: str,
    *,
    statuscode: str,
    limit: int,
    source: str = "wayback",
) -> list[wayback.CdxRow]:
    """Captures near the row's year first (the newest 20 may all be recent parked copies); if the
    window is empty, whatever the archive has."""
    if task.year and source == "wayback":
        windowed: list[wayback.CdxRow] = _cap(ctx).cdx(
            url,
            row_id=task.row_id,
            stage=STAGE,
            limit=limit,
            statuscode=statuscode,
            from_year=task.year - 2,
            to_year=task.year + 3,
        )
        if windowed:
            return windowed
    found: list[wayback.CdxRow] = _cap(ctx).cdx(
        url, row_id=task.row_id, stage=STAGE, limit=limit, statuscode=statuscode, source=source
    )
    return found


def _orig_location(record: CaptureRecord, base: str) -> str | None:
    """Where an archived redirect pointed, from the archive's own headers."""
    headers = {k.lower(): v for k, v in (record.headers.get("response") or {}).items()}
    target = headers.get(_ORIG_LOCATION) or headers.get("location")
    if not target:
        return None
    target = _WAYBACK_PREFIX.sub("", target)
    return urljoin(base, target)


def _target_url(task: LinkTask) -> str | None:
    """The address to look up in an archive: the expansion, or the URL itself if it is not a
    shortener. None for a shortener whose expansion is unknown (nothing to look up)."""
    st = task.st
    if st.expansion:
        return st.expansion
    return None if task.is_shortener else task.fetch_url


# --- the modules --------------------------------------------------------------------------------


class _Base:
    stage = STAGE

    def available(self, ctx: Context) -> Availability:
        return Availability.available()

    def applies(self, row: Row, ctx: Context) -> bool | str:
        return True


class ResolveLive(_Base):
    name = "resolve.live"

    def run(self, row: Row, ctx: Context) -> ModuleResult:
        task: LinkTask = row  # type: ignore[assignment]
        st = task.st
        record = _try_live(ctx, task, task.fetch_url, task.fetch_url)
        if record.outcome is Outcome.miss and st.expansion is None and not task.has_scheme:
            # A bare address: https was tried first; fall back to plain http.
            insecure = "http://" + task.fetch_url.removeprefix("https://")
            if not st.robots_blocked:
                record = _try_live(ctx, task, insecure, insecure)
        return record


class ResolveBitlyInfo(_Base):
    name = "resolve.bitly_info"

    def available(self, ctx: Context) -> Availability:
        if not ctx.config.first_pass.bitly_info_enabled:
            return Availability.unavailable("first_pass.bitly_info_enabled is false")
        return Availability.available()

    def applies(self, row: Row, ctx: Context) -> bool | str:
        task: LinkTask = row  # type: ignore[assignment]
        if task.host not in BITLY_HOSTS:
            return "not a Bitly link"
        if task.st.done or task.st.live_expanded:
            return "live resolution already found where it points"
        return True

    def run(self, row: Row, ctx: Context) -> ModuleResult:
        task: LinkTask = row  # type: ignore[assignment]
        st = task.st
        parts = urlsplit(task.fetch_url)  # the "+" goes on the path, never after a query string
        info_url = parts._replace(path=parts.path.rstrip("/") + "+", query="", fragment="").geturl()
        record = _fetch_page(ctx, task, info_url)
        if record is None:
            return ModuleResult(Outcome.miss)
        if record.tool == "robots_disallow":
            _note(task, "robots.txt disallows the Bitly info page: unreachable by policy")
            return ModuleResult(Outcome.miss)
        if not record.ok:
            _note(task, f"Bitly info page: HTTP {record.http_status}")
            return ModuleResult(Outcome.miss)
        destination = long_url_from_info_page(_body(ctx, record).decode("utf-8", "replace"))
        if destination is None:
            _note(task, "Bitly info page names no destination")  # abstain rather than guess
            return ModuleResult(Outcome.miss)
        st.expansion, st.method = destination, "bitly_info"
        hit = _try_expansion_live(ctx, task)
        return ModuleResult(Outcome.hit if hit else Outcome.miss, wrote={"expansion": destination})


class ResolveWaybackShort(_Base):
    name = "resolve.wayback_short"

    def applies(self, row: Row, ctx: Context) -> bool | str:
        task: LinkTask = row  # type: ignore[assignment]
        if not task.is_shortener:
            return "not a shortener"
        if task.st.expansion:
            return "the destination is already known"
        return True

    def run(self, row: Row, ctx: Context) -> ModuleResult:
        task: LinkTask = row  # type: ignore[assignment]
        st = task.st
        try:
            captures = _cdx_near_year(ctx, task, task.fetch_url, statuscode="3..", limit=10)
        except CaptureError as exc:
            _note(task, f"Wayback CDX (shortener): {exc}"[:200])
            return ModuleResult(Outcome.miss)
        if not captures:
            _note(task, "the Internet Archive never captured this shortener's redirect")
            return ModuleResult(Outcome.miss)
        for found in _closest(captures, task.year)[:MAX_ARCHIVE_TRIES]:
            try:
                record = _cap(ctx).fetch_archived(
                    found, row_id=task.row_id, stage=STAGE, follow=False
                )
            except CaptureError as exc:
                _note(task, f"archived redirect fetch failed: {exc}"[:200])
                continue
            destination = _orig_location(record, task.fetch_url)
            host = (urlsplit(destination or "").hostname or "").lower().removeprefix("www.")
            if destination and (host in SHORTENERS or host == task.host):
                continue  # http to https, or on to another shortener: not where the link goes
            if destination:
                st.expansion, st.method = destination, "wayback_short"
                hit = _try_expansion_live(ctx, task)
                return ModuleResult(
                    Outcome.hit if hit else Outcome.miss, wrote={"expansion": destination}
                )
        _note(task, "archived redirect captures did not name a destination")
        return ModuleResult(Outcome.miss)


class _ArchiveLookup(_Base):
    """Shared by the Wayback and Common Crawl target lookups."""

    source = "wayback"
    method = "wayback_target"

    def applies(self, row: Row, ctx: Context) -> bool | str:
        task: LinkTask = row  # type: ignore[assignment]
        if task.st.done:
            return "already resolved"
        if _target_url(task) is None:
            return "no destination is known, so there is nothing to look up"
        return True

    def _accept(
        self, ctx: Context, task: LinkTask, record: CaptureRecord, target: str
    ) -> CaptureRecord | None:
        verdict, extracted, date = _judge(ctx, task, record, target, check_redirect=False)
        if not verdict.ok:
            _note(task, f"{self.source} copy is not usable: {verdict.reason}")
            return None
        _store_text(ctx, task, record, extracted, date)
        return record

    def run(self, row: Row, ctx: Context) -> ModuleResult:
        task: LinkTask = row  # type: ignore[assignment]
        st = task.st
        target = _target_url(task)
        if target is None:
            return ModuleResult(Outcome.miss)
        try:
            captures = _cdx_near_year(
                ctx, task, target, statuscode="200", limit=20, source=self.source
            )
        except CaptureError as exc:
            _note(task, f"{self.source} index: {exc}"[:200])
            return ModuleResult(Outcome.miss)
        if not captures:
            _note(task, f"no {self.source} capture of the destination")
            return ModuleResult(Outcome.miss)
        for found in _closest(captures, task.year)[:MAX_ARCHIVE_TRIES]:
            try:
                accepted = self._fetch_and_accept(ctx, task, found, target)
            except CaptureError as exc:
                _note(task, f"{self.source} fetch failed: {exc}"[:200])
                continue
            if accepted is not None:
                if st.expansion is None:
                    st.expansion = target
                st.method = st.method or self.method
                st.status, st.capture_id = "archived", accepted.capture_id
                return ModuleResult(Outcome.hit, wrote={"capture_id": accepted.capture_id})
        return ModuleResult(Outcome.miss)

    def _fetch_and_accept(
        self, ctx: Context, task: LinkTask, found: wayback.CdxRow, target: str
    ) -> CaptureRecord | None:
        record = _cap(ctx).fetch_archived(found, row_id=task.row_id, stage=STAGE)
        return self._accept(ctx, task, record, target)


class ResolveWaybackTarget(_ArchiveLookup):
    name = "resolve.wayback_target"


class ResolveCommonCrawl(_ArchiveLookup):
    name = "resolve.commoncrawl"
    source = "commoncrawl"
    method = "commoncrawl"

    def available(self, ctx: Context) -> Availability:
        if not ctx.config.first_pass.commoncrawl_enabled:
            return Availability.unavailable("first_pass.commoncrawl_enabled is false")
        return Availability.available()

    def _fetch_and_accept(
        self, ctx: Context, task: LinkTask, found: wayback.CdxRow, target: str
    ) -> CaptureRecord | None:
        member = _cap(ctx).fetch_archived(found, row_id=task.row_id, stage=STAGE)
        payload = wayback.commoncrawl_payload(_body(ctx, member))
        if payload is None:
            _note(task, "the Common Crawl record could not be read")
            return None
        status, content_type, body = payload
        page = _cap(ctx).store_bytes(
            body,
            row_id=task.row_id,
            stage=STAGE,
            url_requested=found.original,
            url_final=found.original,
            http_status=status,
            content_type=content_type,
            tool="commoncrawl-payload",
            tool_version="1",
            headers={"parent_capture_id": member.capture_id, "warc_member": found.filename},
        )
        return self._accept(ctx, task, page, target)


MODULES = (
    ResolveLive,
    ResolveBitlyInfo,
    ResolveWaybackShort,
    ResolveWaybackTarget,
    ResolveCommonCrawl,
)


def register_modules(registry: Registry) -> None:
    for module in MODULES:
        if registry.get(module.name) is None:
            registry.register(module)


# --- Bitly's "+" info page ----------------------------------------------------------------------

_LONG_URL_PATTERNS = (
    re.compile(r'"(?:long_?[uU]rl|longUrl|long-url)"\s*:\s*"([^"]+)"'),
    re.compile(r'(?:data-(?:long|expanded)-url|data-long-url)\s*=\s*"([^"]+)"', re.I),
    re.compile(r'<meta[^>]+property="og:url"[^>]+content="([^"]+)"', re.I),
    re.compile(
        r"(?:long\s*url|destination|redirects? to)[^<]{0,60}</[^>]+>\s*<a[^>]+href=\"([^\"]+)\"",
        re.I,
    ),
)


def long_url_from_info_page(html: str) -> str | None:
    """The destination named on a Bitly ``+`` page, or None.

    Bitly's markup is not a stable interface, so this accepts only labelled or structured forms
    (a ``long_url`` field, a data attribute, an ``og:url``, a "Long URL" label followed by a
    link) and never a bare link from the page's furniture. No match means no answer.
    """
    for pattern in _LONG_URL_PATTERNS:
        for match in pattern.finditer(html):
            candidate = match.group(1).replace("\\/", "/").replace("&amp;", "&")
            host = (urlsplit(candidate).hostname or "").lower()
            if (
                urlsplit(candidate).scheme in ("http", "https")
                and host
                and not any(
                    host == f or host.endswith("." + f) or f in host for f in _BITLY_FURNITURE
                )
            ):
                return candidate
    return None

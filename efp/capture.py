"""The capture layer: the one place the pipeline touches the network (spec sections 4, 7 and 10).

``Capture.fetch`` is the single client for every network call. In order, it

1. checks ``robots.txt`` for page fetches (cached per domain; a disallow is a ``captures`` row with
   ``http_status = NULL`` and ``tool = 'robots_disallow'``, never a bypass),
2. sends the request through a ``requests-cache`` session (SQLite, honoring ``Cache-Control``) with
   the project ``User-Agent``; the session's adapter takes the per-domain budget before every
   network hop, so cached responses cost nothing and redirects to another domain use that
   domain's budget,
3. retries 429, 5xx and connection errors with jitter (``stamina``),
4. appends the request and response to the day's WARC, writes the body to the content-addressed
   store, and inserts one ``captures`` row.

A cached response still produces a ``captures`` row (``tool = 'requests-cache'``): "what was
fetched when" must stay answerable. Relevance is never judged here: whatever came back is captured.

Secrets never reach ``captures.headers_json``, the URL columns, the WARC or the HTTP cache:
``Authorization`` and similar headers and key-like query parameters are replaced before anything is
stored. Nothing else in the codebase may import ``requests`` (a test enforces it).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
import urllib.robotparser
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import metadata
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
import requests_cache
import stamina
from requests.adapters import HTTPAdapter

from efp.config import Config
from efp.logging import redact as redact_secret_values
from efp.ratelimit import DomainLimiter, RateLimitTimeout
from efp.store import body_relpath, read_verified, sha256_hex, write_body
from efp.version import package_version
from efp.warcstore import WarcStore

log = logging.getLogger(__name__)

_CACHE_VERSION = metadata.version("requests-cache")

REDACTED = "REDACTED"

SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "x-auth-token",
        "x-api-token",
        "x-access-token",
    }
)
# Always stripped from stored URLs.
_ALWAYS_PARAMS = frozenset(
    {"api_key", "apikey", "api-key", "access_token", "client_secret", "password"}
)
# Also stripped when the call is an API call (kind="api"), where these names are credentials.
_API_PARAMS = frozenset({"key", "token", "auth", "secret", "signature", "sig"})
# What the HTTP cache must not key or store on.
# requests-cache matches these names case-sensitively, so list the usual spellings of each header.
_HEADER_SPELLINGS = {
    "authorization": "Authorization",
    "proxy-authorization": "Proxy-Authorization",
    "cookie": "Cookie",
    "set-cookie": "Set-Cookie",
    "x-api-key": "X-API-Key",
    "x-auth-token": "X-Auth-Token",
    "x-api-token": "X-API-Token",
    "x-access-token": "X-Access-Token",
}
CACHE_IGNORED = sorted(
    SENSITIVE_HEADERS | set(_HEADER_SPELLINGS.values()) | _ALWAYS_PARAMS | _API_PARAMS
)

_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})
_ROBOTS_TOOL = "robots_txt"


class CaptureError(Exception):
    """A fetch could not be completed (no response was received, or an input was invalid)."""


def redact_url(url: str, *, api: bool = False) -> str:
    """The URL with credential-looking query parameters replaced, for anything that is stored."""
    parts = urlsplit(url)
    if not parts.query:
        return redact_secret_values(url)
    names = _ALWAYS_PARAMS | (_API_PARAMS if api else frozenset())
    pairs = [
        (k, REDACTED if k.lower() in names else v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return redact_secret_values(urlunsplit(parts._replace(query=urlencode(pairs))))


_CREDENTIAL_PARAM = re.compile(
    r"(?i)\b(api[_-]?key|access_token|client_secret|password|key|token|auth|secret|signature|sig)"
    r"=([^&\s'\"]+)"
)


def scrub_text(text: str) -> str:
    """Error and log text with credential-looking ``name=value`` pairs and secret values removed."""
    return redact_secret_values(_CREDENTIAL_PARAM.sub(lambda m: f"{m.group(1)}={REDACTED}", text))


def with_params(url: str, params: Mapping[str, str] | None) -> str:
    """``url`` with extra query parameters appended, as the request will actually be sent."""
    if not params:
        return url
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True) + list(params.items())
    return urlunsplit(parts._replace(query=urlencode(query)))


def redact_headers(headers: Mapping[str, str] | Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    items = headers.items() if isinstance(headers, Mapping) else headers
    out: list[tuple[str, str]] = []
    for name, value in items:
        shown = REDACTED if name.lower() in SENSITIVE_HEADERS else redact_secret_values(str(value))
        out.append((str(name), shown))
    return out


@dataclass(frozen=True)
class CaptureRecord:
    """What a fetch returns: the ``captures`` row plus what a caller needs without a lookup."""

    capture_id: int
    row_id: int | None
    stage: str
    url_requested: str
    url_final: str | None
    http_status: int | None
    content_type: str | None
    content_sha256: str
    body_path: str
    tool: str
    tool_version: str
    retrieved_at: str
    warc_path: str | None = None
    from_cache: bool = False
    headers: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.http_status is not None and 200 <= self.http_status < 300


class _RetryableStatus(Exception):
    def __init__(self, response: requests.Response) -> None:
        super().__init__(f"HTTP {response.status_code}")
        self.response = response


def _cacheable(response: requests.Response) -> bool:
    """A response that sets a cookie is never cached: the cookie value is a credential, and the
    cache would store it (headers, cookie jar and raw response). It is still captured and recorded
    (with the cookie redacted); it is simply fetched again next time."""
    return not (response.headers.get("Set-Cookie") or len(response.cookies))


def _redact_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    """A caller-supplied headers dict, with credential headers and secret values removed."""
    out: dict[str, Any] = {}
    for name, item in value.items():
        if name.lower() in SENSITIVE_HEADERS:
            out[name] = REDACTED
        elif isinstance(item, Mapping):
            out[name] = _redact_mapping(item)
        elif isinstance(item, str):
            out[name] = scrub_text(item)
        else:
            out[name] = item
    return out


class _PoliteAdapter(HTTPAdapter):
    """Takes the domain budget immediately before each network hop (cache hits never get here)."""

    def __init__(self, limiter: DomainLimiter, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._limiter = limiter
        self.network_requests = 0

    def send(
        self, request: requests.PreparedRequest, *args: Any, **kwargs: Any
    ) -> requests.Response:
        host = urlsplit(request.url or "").hostname or ""
        self._limiter.acquire(host)
        self.network_requests += 1
        return super().send(request, *args, **kwargs)


@dataclass
class _Robots:
    fetched_at: float
    allow_all: bool
    parser: urllib.robotparser.RobotFileParser | None
    url: str
    body: bytes
    status: int | None


class Capture:
    """The client. One per process; safe to share between threads only if the connection is."""

    def __init__(
        self,
        config: Config,
        conn: sqlite3.Connection,
        *,
        limiter: DomainLimiter | None = None,
        session: requests_cache.CachedSession | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.config = config
        self._conn = conn
        self._sleep = sleep
        self._clock = clock
        self.limiter = limiter or DomainLimiter(config)
        self.warcs = WarcStore(config.paths.warc, software=f"efp/{package_version()}")
        self._robots: dict[str, _Robots] = {}
        self._lock = threading.Lock()
        self.user_agent = config.project.user_agent
        self._ua_token = self.user_agent.split("/")[0].split()[0]
        if session is None:
            config.paths.cache.mkdir(parents=True, exist_ok=True)
            expire = config.capture.cache_expire_hours * 3600
            session = requests_cache.CachedSession(
                cache_name=str(config.paths.cache / "http_cache"),
                backend="sqlite",
                expire_after=expire if expire > 0 else -1,
                cache_control=True,
                allowable_codes=(200, 203, 300, 301, 404, 410),
                allowable_methods=("GET", "HEAD"),
                ignored_parameters=CACHE_IGNORED,
                filter_fn=_cacheable,
                match_headers=False,
            )
        self.adapter = _PoliteAdapter(self.limiter)
        session.mount("https://", self.adapter)
        session.mount("http://", self.adapter)
        session.headers["User-Agent"] = self.user_agent
        session.max_redirects = config.capture.max_redirects
        self.session = session

    # --- public API -------------------------------------------------------------------------

    def fetch(
        self,
        url: str,
        *,
        row_id: int | None,
        stage: str,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
        allow_redirects: bool = True,
        timeout: float | None = None,
        kind: str = "page",
        params: Mapping[str, str] | None = None,
        data: bytes | Mapping[str, str] | None = None,
        record_url: str | None = None,
        tool: str | None = None,
        enqueue_spn: bool | None = None,
        retry: bool = True,
        no_cache: bool = False,
    ) -> CaptureRecord:
        """Fetch ``url`` and capture whatever comes back.

        ``kind="page"`` is a scrape target (robots.txt is honored, successful responses are queued
        for Save Page Now); ``kind="api"`` is a documented API endpoint (no robots check, not
        queued). ``record_url`` is stored as ``url_requested`` when it differs from the URL
        actually requested (Wayback fetches record the original URL); ``tool`` overrides the
        recorded tool. ``retry=False`` makes a single attempt (Save Page Now treats a 429 as
        deferral, not something to hammer); ``no_cache=True`` bypasses a cached answer (status
        polls).
        """
        if urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).hostname:
            raise CaptureError(f"not a fetchable http(s) URL: {scrub_text(url)!r}")
        api = kind == "api"
        wanted = record_url or with_params(url, params)
        check_robots = kind == "page" and self.config.rate_limits.honor_robots_txt
        seconds = timeout or self.config.capture.timeout_seconds
        # Redirects are followed here, one hop at a time, so every hop is checked against its own
        # robots.txt and budget, and credentials are not carried to a different host.
        origin_host = urlsplit(url).hostname
        current, current_method, current_params, current_data = url, method, params, data
        hops: list[requests.Response] = []
        while True:
            if check_robots:
                blocked = self._robots_block(
                    with_params(current, current_params),
                    wanted,
                    row_id=row_id,
                    stage=stage,
                    hops=hops,
                )
                if blocked is not None:
                    return blocked
            same_host = urlsplit(current).hostname == origin_host
            sent_headers = {
                k: v
                for k, v in (headers or {}).items()
                if same_host or k.lower() not in SENSITIVE_HEADERS
            }
            response = self._send(
                current_method,
                current,
                headers=sent_headers,
                allow_redirects=False,
                timeout=seconds,
                params=current_params,
                data=current_data,
                retry=retry,
                no_cache=no_cache,
            )
            location = response.headers.get("Location")
            if allow_redirects and response.status_code in _REDIRECT_STATUS and location:
                hops.append(response)
                if len(hops) > self.config.capture.max_redirects:
                    raise CaptureError(f"{scrub_text(url)}: too many redirects")
                current = urljoin(response.url, location)
                if urlsplit(current).scheme not in ("http", "https"):
                    raise CaptureError(f"{scrub_text(url)}: redirect to a non-http URL")
                current_params = None
                if response.status_code == 303 or (
                    response.status_code in (301, 302) and current_method.upper() == "POST"
                ):
                    current_method, current_data = "GET", None
                continue
            break
        return self._record_response(
            response,
            hops=hops,
            row_id=row_id,
            stage=stage,
            url_requested=redact_url(wanted, api=api),
            tool=tool,
            api=api,
            enqueue_spn=(kind == "page") if enqueue_spn is None else enqueue_spn,
        )

    # --- the wayback and perma interfaces, as methods ---------------------------------------

    def cdx(self, url: str, **kwargs: Any) -> list[Any]:
        """CDX search (Wayback, or ``source="commoncrawl"``): see :func:`efp.wayback.cdx`."""
        from efp import wayback

        return wayback.cdx(self, url, **kwargs)

    def availability(self, url: str, **kwargs: Any) -> Any:
        from efp import wayback

        return wayback.availability(self, url, **kwargs)

    def fetch_archived(self, row: Any, *, row_id: int | None, stage: str) -> CaptureRecord:
        from efp import wayback

        return wayback.fetch_archived(self, row, row_id=row_id, stage=stage)

    def perma(self, url: str, secrets: Any = None) -> Any:
        """Archive a cited URL at Perma.cc (no-op without a key): see :func:`efp.perma.archive`."""
        from efp import perma

        return perma.archive(self, url, secrets)

    def store_bytes(
        self,
        data: bytes,
        *,
        row_id: int | None,
        stage: str,
        url_requested: str,
        tool: str,
        tool_version: str = "0",
        url_final: str | None = None,
        http_status: int | None = None,
        content_type: str | None = None,
        manual_source: str | None = None,
        headers: Mapping[str, Any] | None = None,
        site_data_sha256: str | None = None,
    ) -> CaptureRecord:
        """Capture a body that arrived another way (a download, a manual import)."""
        digest = write_body(self.config.paths.store, data)
        return self._insert(
            row_id=row_id,
            stage=stage,
            url_requested=redact_url(url_requested, api=True),
            url_final=redact_url(url_final, api=True) if url_final else None,
            http_status=http_status,
            content_type=content_type,
            digest=digest,
            headers=_redact_mapping(headers or {}),
            warc_path=None,
            tool=tool,
            tool_version=tool_version,
            manual_source=manual_source,
            site_data_sha256=site_data_sha256,
            from_cache=False,
        )

    def read_body(self, record: CaptureRecord) -> bytes:
        return read_verified(self.config.paths.store, record.content_sha256)

    def close(self) -> None:
        self.session.close()  # type: ignore[no-untyped-call]
        self.limiter.close()

    # --- robots -----------------------------------------------------------------------------

    def _robots_for(self, url: str, *, row_id: int | None, stage: str) -> _Robots:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        ttl = self.config.capture.robots_cache_hours * 3600
        with self._lock:
            cached = self._robots.get(origin)
        if cached is not None and time.monotonic() - cached.fetched_at < ttl:
            return cached
        robots_url = f"{origin}/robots.txt"
        response = self._send(
            "GET",
            robots_url,
            headers=None,
            allow_redirects=True,
            timeout=self.config.capture.timeout_seconds,
            params=None,
            data=None,
            retry=True,
            no_cache=False,
        )
        self._record_response(
            response,
            row_id=row_id,
            stage=stage,
            url_requested=robots_url,
            tool=_ROBOTS_TOOL if not getattr(response, "from_cache", False) else None,
            api=True,
            enqueue_spn=False,
        )
        status = response.status_code
        body = response.content
        if status == 200:
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(body.decode("utf-8", errors="replace").splitlines())
            entry = _Robots(time.monotonic(), False, parser, robots_url, body, status)
        elif status in (401, 403, 429) or status >= 500:
            # Unreadable rules: fetch less. The whole site stays disallowed until they can be read.
            entry = _Robots(time.monotonic(), False, None, robots_url, body, status)
        else:  # 404 and other 4xx: no robots.txt, nothing is disallowed
            entry = _Robots(time.monotonic(), True, None, robots_url, body, status)
        with self._lock:
            self._robots[origin] = entry
        return entry

    def _robots_block(
        self,
        url: str,
        wanted: str,
        *,
        row_id: int | None,
        stage: str,
        hops: Sequence[requests.Response] = (),
    ) -> CaptureRecord | None:
        robots = self._robots_for(url, row_id=row_id, stage=stage)
        if robots.allow_all:
            return None
        if robots.parser is not None and robots.parser.can_fetch(self._ua_token, url):
            return None
        digest = write_body(self.config.paths.store, robots.body)
        return self._insert(
            row_id=row_id,
            stage=stage,
            url_requested=redact_url(wanted),
            url_final=robots.url,
            http_status=None,
            content_type="text/plain",
            digest=digest,
            headers={
                "robots_txt_status": robots.status,
                "rules_url": robots.url,
                "blocked_url": redact_url(url),
                "redirected_via": [
                    {"url": redact_url(h.url), "status": h.status_code} for h in hops
                ],
            },
            warc_path=None,
            tool="robots_disallow",
            tool_version=package_version(),
            manual_source=None,
            site_data_sha256=None,
            from_cache=False,
        )

    # --- sending ----------------------------------------------------------------------------

    def _send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None,
        allow_redirects: bool,
        timeout: float,
        params: Mapping[str, str] | None,
        data: bytes | Mapping[str, str] | None,
        retry: bool,
        no_cache: bool,
    ) -> requests.Response:
        limits = self.config.rate_limits
        last: requests.Response | None = None
        try:
            for attempt in stamina.retry_context(
                on=(requests.ConnectionError, requests.Timeout, _RetryableStatus),
                attempts=limits.retry_attempts + 1 if retry else 1,
                wait_initial=limits.retry_backoff_seconds,
                wait_max=max(limits.retry_backoff_seconds * 8, limits.retry_backoff_seconds),
                wait_jitter=limits.retry_backoff_seconds / 2,
            ):
                with attempt:
                    response = self.session.request(
                        method,
                        url,
                        headers=dict(headers or {}),
                        allow_redirects=allow_redirects,
                        timeout=timeout,
                        params=dict(params) if params else None,
                        data=data,
                        force_refresh=no_cache,
                    )
                    last = response
                    if response.status_code in _RETRY_STATUS and not getattr(
                        response, "from_cache", False
                    ):
                        self._honor_retry_after(response)
                        raise _RetryableStatus(response)
        except _RetryableStatus as exc:
            return exc.response  # retries exhausted: a 429/5xx is still a capture
        except RateLimitTimeout as exc:
            raise CaptureError(str(exc)) from exc
        except requests.RequestException as exc:
            raise CaptureError(
                scrub_text(f"{redact_url(url, api=True)}: {type(exc).__name__}: {exc}")
            ) from exc
        if last is None:  # unreachable: retry_context either returns or raises
            raise CaptureError("no response received")
        return last

    def _honor_retry_after(self, response: requests.Response) -> None:
        value = response.headers.get("Retry-After", "")
        if value.isdigit():
            self._sleep(min(float(value), self.config.capture.max_wait_seconds))

    # --- recording --------------------------------------------------------------------------

    def _record_response(
        self,
        response: requests.Response,
        *,
        hops: Sequence[requests.Response] = (),
        row_id: int | None,
        stage: str,
        url_requested: str,
        tool: str | None,
        api: bool,
        enqueue_spn: bool,
    ) -> CaptureRecord:
        body = response.content
        digest = sha256_hex(body)
        write_body(self.config.paths.store, body)
        from_cache = bool(getattr(response, "from_cache", False))
        when = self._clock()
        req = response.request
        req_headers = redact_headers(dict(req.headers))
        res_headers = redact_headers(list(response.headers.items()))
        url_final = redact_url(response.url, api=api)
        warc_path: str | None
        if from_cache:
            warc_path = self._earlier_warc(digest)
            used_tool, used_version = "requests-cache", _CACHE_VERSION
        else:
            for hop in hops:  # each redirect response is evidence too, so the original URL replays
                if not getattr(hop, "from_cache", False):
                    self._write_warc(hop, when, api=api)
            warc_path = self._write_warc(response, when, api=api)
            used_tool, used_version = "requests", requests.__version__
        headers = {
            "request": dict(req_headers),
            "response": dict(res_headers),
            "history": [{"url": redact_url(h.url, api=api), "status": h.status_code} for h in hops],
            "from_cache": from_cache,
        }
        record = self._insert(
            row_id=row_id,
            stage=stage,
            url_requested=url_requested,
            url_final=url_final,
            http_status=response.status_code,
            content_type=response.headers.get("Content-Type"),
            digest=digest,
            headers=headers,
            warc_path=warc_path,
            tool=tool or used_tool,
            tool_version=used_version if tool is None or from_cache else package_version(),
            manual_source=None,
            site_data_sha256=None,
            from_cache=from_cache,
            retrieved_at=when,
        )
        if enqueue_spn and record.ok:
            self.enqueue_spn(record.capture_id, record.url_final or record.url_requested)
        return record

    def _write_warc(self, response: requests.Response, when: datetime, *, api: bool) -> str:
        req = response.request
        request_body = req.body if isinstance(req.body, bytes) else (req.body or "").encode()
        return self.warcs.write_pair(
            when=when,
            url=redact_url(response.url, api=api),
            method=req.method or "GET",
            request_headers=redact_headers(dict(req.headers)),
            request_body=request_body,
            status=response.status_code,
            reason=response.reason or "",
            response_headers=redact_headers(list(response.headers.items())),
            body=response.content,
        )

    def _earlier_warc(self, digest: str) -> str | None:
        found = self._conn.execute(
            "SELECT warc_path FROM captures WHERE content_sha256 = ? AND warc_path IS NOT NULL "
            "ORDER BY capture_id LIMIT 1",
            (digest,),
        ).fetchone()
        return None if found is None else str(found[0])

    def _insert(
        self,
        *,
        row_id: int | None,
        stage: str,
        url_requested: str,
        url_final: str | None,
        http_status: int | None,
        content_type: str | None,
        digest: str,
        headers: dict[str, Any],
        warc_path: str | None,
        tool: str,
        tool_version: str,
        manual_source: str | None,
        site_data_sha256: str | None,
        from_cache: bool,
        retrieved_at: datetime | None = None,
    ) -> CaptureRecord:
        stamp = (retrieved_at or self._clock()).astimezone(UTC).isoformat(timespec="seconds")
        body_path = body_relpath(digest)
        cursor = self._conn.execute(
            "INSERT INTO captures (row_id, stage, url_requested, url_final, retrieved_at, "
            "http_status, content_type, content_sha256, body_path, headers_json, warc_path, "
            "tool, tool_version, site_data_sha256, manual_source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row_id,
                stage,
                url_requested,
                url_final,
                stamp,
                http_status,
                content_type,
                digest,
                body_path,
                json.dumps(headers, sort_keys=True),
                warc_path,
                tool,
                tool_version,
                site_data_sha256,
                manual_source,
            ),
        )
        self._conn.commit()
        log.info(
            "captured %s status=%s tool=%s sha256=%s row=%s stage=%s",
            scrub_text(url_requested)[:200],
            http_status,
            tool,
            digest[:12],
            row_id,
            stage,
        )
        capture_id = cursor.lastrowid
        if capture_id is None:
            raise CaptureError("the captures row was not inserted")
        return CaptureRecord(
            capture_id=capture_id,
            row_id=row_id,
            stage=stage,
            url_requested=url_requested,
            url_final=url_final,
            http_status=http_status,
            content_type=content_type,
            content_sha256=digest,
            body_path=body_path,
            tool=tool,
            tool_version=tool_version,
            retrieved_at=stamp,
            warc_path=warc_path,
            from_cache=from_cache,
            headers=headers,
        )

    # --- Save Page Now queue ------------------------------------------------------------------

    def enqueue_spn(self, capture_id: int, url: str) -> bool:
        """Queue a captured URL for Save Page Now unless that URL is already queued or archived."""
        clean = redact_url(url)
        known = self._conn.execute(
            "SELECT 1 FROM spn_queue WHERE url = ? AND status != 'failed' LIMIT 1", (clean,)
        ).fetchone()
        if known is not None:
            return False
        self._conn.execute(
            "INSERT OR IGNORE INTO spn_queue (capture_id, url, enqueued_at) VALUES (?, ?, ?)",
            (capture_id, clean, self._clock().astimezone(UTC).isoformat(timespec="seconds")),
        )
        self._conn.commit()
        return True


class LazyCapture:
    """``ctx.capture`` before anything fetches: builds the real client on first use.

    Building a client creates the cache and rate-limit databases, which commands such as
    ``efp status`` and ``efp modules`` should not do.
    """

    def __init__(self, config: Config, conn: sqlite3.Connection | None) -> None:
        self._config = config
        self._conn = conn
        self._real: Capture | None = None

    def _client(self) -> Capture:
        if self._real is None:
            if self._conn is None:
                raise CaptureError("no database connection: the capture layer needs one")
            self._real = Capture(self._config, self._conn)
        return self._real

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client(), name)

    def close(self) -> None:
        if self._real is not None:
            self._real.close()

"""The capture layer: the one place the pipeline touches the network (spec sections 4, 7 and 10).

``Capture.fetch`` is the single client for every network call. In order, it

1. sends the request through a ``requests-cache`` session (SQLite, honoring ``Cache-Control``) with
   the project ``User-Agent``; the session's adapter takes the per-domain rate-limit budget before
   every network hop, so cached responses cost nothing and redirects to another domain use that
   domain's budget,
2. retries 429, 5xx and connection errors with jitter (``stamina``),
3. appends the request and response to the day's WARC, writes the body to the content-addressed
   store, and inserts one ``captures`` row.

A cached response still produces a ``captures`` row (``tool = 'requests-cache'``): "what was
fetched when" must stay answerable. Relevance is never judged here: whatever came back is captured.

Secrets never reach ``captures.headers_json``, the URL columns, the WARC or the HTTP cache:
``Authorization`` and similar headers and key-like query parameters are replaced before anything is
stored. Nothing else in the codebase may import ``requests`` (a test enforces it).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
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


def image_tag(image: str) -> str:
    """The tag of a Docker image reference (``repo/name:1.13.2`` or ``repo/name:1.13.2@sha256:…``),
    or "" when there is none (a bare name, or a digest-only pin)."""
    name = image.split("@", 1)[0]
    last = name.rsplit("/", 1)[-1]
    return last.split(":", 1)[1] if ":" in last else ""


MAX_WACZ_BYTES = 250 * 1024 * 1024  # a browser capture of one page is far smaller than this
_REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})


@dataclass(frozen=True)
class LlmResult:
    """What one model call returned, and the capture that holds its request and response."""

    text: str
    model: str
    input_tokens: int
    output_tokens: int
    request_id: str | None
    stop_reason: str | None
    capture_id: int


class CaptureError(Exception):
    """A fetch could not be completed (no response was received, or an input was invalid)."""


class LlmHttpError(CaptureError):
    """The API answered with an HTTP error (not billed). The stored request and answer are the
    capture ``capture_id``, so the caller's log row can point at them."""

    def __init__(self, message: str, *, capture_id: int | None, request_id: str | None) -> None:
        super().__init__(message)
        self.capture_id = capture_id
        self.request_id = request_id


class LlmCallError(CaptureError):
    """A model call that may have been billed although no usable answer came back: a timeout or a
    dropped connection after the request went out, or a failure after a successful answer (the
    tokens, when known, are what was billed). The caller logs the cost; it is never zero."""

    def __init__(
        self,
        message: str,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.request_id = request_id


class CaptureBudgetError(CaptureError):
    """The domain's rate budget did not free up in time (a spent daily quota, typically)."""


def _unsatisfied_total(content_range: str) -> int | None:
    """N from a 416's ``Content-Range: bytes */N`` (the length of the server's file)."""
    found = re.match(r"bytes\s+\*/(\d+)", content_range.strip())
    return int(found.group(1)) if found else None


def _validator(etag: str | None, modified: str | None) -> str | None:
    """What a resume sends as If-Range: a strong ETag, else the Last-Modified date."""
    if etag and not etag.startswith("W/"):
        return etag
    return modified or None


def _range_starts_at(content_range: str, offset: int) -> bool:
    found = re.match(r"bytes\s+(\d+)-", content_range.strip())
    return found is not None and int(found.group(1)) == offset


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


class _BudgetAdapter(HTTPAdapter):
    """Takes the domain budget immediately before each network hop (cache hits never get here)."""

    def __init__(self, limiter: DomainLimiter, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._limiter = limiter
        self.network_requests = 0
        self._local = threading.local()  # wait_limit is per thread: a parallel fetch keeps its own

    @property
    def max_wait(self) -> float | None:
        return getattr(self._local, "max_wait", None)

    @max_wait.setter
    def max_wait(self, value: float | None) -> None:
        self._local.max_wait = value

    def send(
        self, request: requests.PreparedRequest, *args: Any, **kwargs: Any
    ) -> requests.Response:
        host = urlsplit(request.url or "").hostname or ""
        self._limiter.acquire(host, max_wait=self.max_wait)
        self.network_requests += 1
        return super().send(request, *args, **kwargs)


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
        self._throttled: set[str] = set()  # services that answered 429 in this run
        self._failed: set[str] = set()  # URLs that could not be fetched in this run
        self._once: set[str] = set()  # work done once per run (see once_per_run)
        self.run_state: dict[str, Any] = {}  # what a component learned this run (cleared per run)
        self._lock = threading.Lock()
        self.user_agent = config.project.user_agent
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
        self.adapter = _BudgetAdapter(self.limiter)
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

        ``kind="page"`` is a scrape target (successful responses are queued for Save Page Now);
        ``kind="api"`` is a documented API endpoint (not queued). ``record_url`` is stored as
        ``url_requested`` when it differs from the URL actually requested (Wayback fetches record
        the original URL); ``tool`` overrides the recorded tool. ``retry=False`` makes a single
        attempt (Save Page Now treats a 429 as deferral, not something to hammer);
        ``no_cache=True`` bypasses a cached answer (status polls).
        """
        if urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).hostname:
            raise CaptureError(f"not a fetchable http(s) URL: {scrub_text(url)!r}")
        api = kind == "api"
        wanted = record_url or with_params(url, params)
        seconds = timeout or self.config.capture.timeout_seconds
        # Redirects are followed here, one hop at a time, so every hop takes its own domain's
        # budget, and credentials are not carried to a different host.
        origin_host = urlsplit(url).hostname
        current, current_method, current_params, current_data = url, method, params, data
        hops: list[requests.Response] = []
        while True:
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

    def fetch_archived(
        self, row: Any, *, row_id: int | None, stage: str, follow: bool = True
    ) -> CaptureRecord:
        """Fetch an archived body (``follow=False`` keeps an archived redirect as the answer)."""
        from efp import wayback

        return wayback.fetch_archived(self, row, row_id=row_id, stage=stage, follow=follow)

    def perma(self, url: str, secrets: Any = None) -> Any:
        """Archive a cited URL at Perma.cc (no-op without a key): see :func:`efp.perma.archive`."""
        from efp import perma

        return perma.archive(self, url, secrets)

    # --- throttle memory ---------------------------------------------------------------------

    def mark_throttled(self, name: str) -> None:
        """Remember that ``name`` (a service) answered 429, so the rest of this run does not ask."""
        self._throttled.add(name)

    def is_throttled(self, name: str) -> bool:
        return name in self._throttled

    def clear_throttles(self) -> None:
        """Forget every throttle and failed fetch (the runner calls this at the start of a run)."""
        self._throttled.clear()
        self._failed.clear()
        self._once.clear()
        self.run_state.clear()

    def note_failed(self, url: str) -> None:
        """Remember that ``url`` could not be fetched, so this run tries it once (not per row)."""
        self._failed.add(url)

    def failed_this_run(self, url: str) -> bool:
        return url in self._failed

    def ran_this_run(self, key: str) -> bool:
        """Has ``key`` been marked done in this run? (See :meth:`mark_ran`.)"""
        return key in self._once

    def mark_ran(self, key: str) -> None:
        """Mark ``key`` done for this run: for work that must not be skipped when it failed."""
        self._once.add(key)

    def once_per_run(self, key: str) -> bool:
        """True the first time ``key`` is asked in a run, False after (for a refresh done once)."""
        if key in self._once:
            return False
        self._once.add(key)
        return True

    @contextmanager
    def wait_limit(self, seconds: float) -> Iterator[None]:
        """Within this block a fetch waits at most ``seconds`` for a domain budget, then raises
        ``CaptureError``. For a daily quota (CourtListener's 125 a day), where waiting is no use."""
        previous = self.adapter.max_wait
        self.adapter.max_wait = seconds
        try:
            yield
        finally:
            self.adapter.max_wait = previous

    def download(
        self,
        url: str,
        dest: Path,
        *,
        stage: str,
        row_id: int | None = None,
        expected_size: int | None = None,
        _resumes: int = 0,
    ) -> CaptureRecord:
        """Stream a large file to ``dest`` (resumable), hashing as it goes.

        Bulk datasets run to tens of gigabytes, so they are not copied into the WARC or the
        content store. The call still takes the domain's budget and is recorded: the ``captures``
        row's body is a small JSON manifest (URL, size, SHA-256, ETag, last-modified, path), which
        is the provenance of the file the loader later reads.
        """
        if urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).hostname:
            raise CaptureError(f"not a fetchable http(s) URL: {scrub_text(url)!r}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        digest = hashlib.sha256()
        have = 0
        if part.exists():  # resume: re-hash what is there, ask for the rest
            with part.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(chunk)
                    have += len(chunk)
        if expected_size is not None and have > expected_size:
            part.unlink()  # longer than the file can be: not a prefix of it, start over
            digest, have = hashlib.sha256(), 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        tag_file = part.with_name(part.name + ".etag")  # the validator the part was started under
        tag = ""
        if have and tag_file.is_file():
            tag = tag_file.read_text(encoding="utf-8").strip()
            if tag.startswith("W/"):  # a weak validator cannot be used for If-Range
                tag = ""
        whole_part = expected_size is not None and have == expected_size
        if have and not tag and not whole_part:
            # Nothing says the server's file is still the one this part came from (the part was
            # begun without a strong ETag or a Last-Modified date): resuming could splice two
            # files into one that then hashes as if it were genuine. Start again.
            part.unlink()
            digest, have = hashlib.sha256(), 0
            headers = {}
        elif have and tag:
            headers["If-Range"] = tag  # file replaced since: the server answers 200, in full
        etag: str | None = None
        modified: str | None = None
        status = 0
        if expected_size is not None and have == expected_size:
            status = 200  # a complete .part (killed before the rename): finalize without a request
        else:
            response = self._open_download(url, headers)
            with response:
                status = response.status_code
                if status == 416 and have:
                    # "bytes */N": the server's file is N long. N == what we hold means the part
                    # is the whole file (killed before the rename, size not known beforehand):
                    # finish it. Anything else means the part is stale: start again from zero.
                    total = _unsatisfied_total(response.headers.get("Content-Range", ""))
                    if total == have:
                        status = 200
                        etag = response.headers.get("ETag")
                        modified = response.headers.get("Last-Modified")
                    else:
                        part.unlink()
                        return self.download(
                            url,
                            dest,
                            stage=stage,
                            row_id=row_id,
                            expected_size=expected_size,
                            _resumes=_resumes,
                        )  # with no part left this cannot loop
                else:
                    if status == 200 and have:  # the server ignored the range: start over
                        digest, have = hashlib.sha256(), 0
                        part.unlink()
                    elif status in _REDIRECT_STATUS:
                        raise CaptureError(
                            f"{redact_url(url, api=True)}: redirected; not followed (a "
                            "download is made from the address listed): use the final address"
                        )
                    elif status not in (200, 206):
                        raise CaptureError(f"{redact_url(url, api=True)}: HTTP {status}")
                    elif status == 206 and not _range_starts_at(
                        response.headers.get("Content-Range", ""), have
                    ):
                        part.unlink()
                        raise CaptureError(
                            f"{dest.name}: the server resumed at a different offset; the partial "
                            "file was removed, run the download again"
                        )
                    validator = _validator(
                        response.headers.get("ETag"), response.headers.get("Last-Modified")
                    )
                    if validator:
                        tag_file.write_text(validator, encoding="utf-8")
                    else:
                        tag_file.unlink(missing_ok=True)
                    try:
                        with part.open("ab") as out:
                            for chunk in response.iter_content(chunk_size=1 << 20):
                                out.write(chunk)
                                digest.update(chunk)
                                have += len(chunk)
                    except requests.RequestException as exc:
                        # The connection broke mid-stream: what arrived is in the .part file, so
                        # ask again for the rest (a bounded number of times), then give up cleanly.
                        limits = self.config.rate_limits
                        if _resumes >= limits.retry_attempts:
                            raise CaptureError(
                                scrub_text(
                                    f"{dest.name}: the connection broke after {have} bytes "
                                    f"({type(exc).__name__}); run the download again to resume"
                                )
                            ) from exc
                        self._sleep(limits.retry_backoff_seconds * 2.0 ** (_resumes + 1))
                        return self.download(
                            url,
                            dest,
                            stage=stage,
                            row_id=row_id,
                            expected_size=expected_size,
                            _resumes=_resumes + 1,
                        )
                    etag = response.headers.get("ETag")
                    modified = response.headers.get("Last-Modified")
        if expected_size is not None and have != expected_size:
            part.unlink(missing_ok=True)  # a wrong-sized part can never be resumed into the file
            raise CaptureError(f"{dest.name}: got {have} bytes, expected {expected_size}")
        part.replace(dest)
        tag_file.unlink(missing_ok=True)
        manifest = {
            "url": redact_url(url, api=True),
            "path": str(dest),
            "size": have,
            "sha256": digest.hexdigest(),
            "etag": etag,
            "last_modified": modified,
        }
        return self.store_bytes(
            json.dumps(manifest, sort_keys=True).encode(),
            row_id=row_id,
            stage=stage,
            url_requested=url,
            url_final=url,
            http_status=status,
            content_type="application/json",
            tool="bulk_download",
            tool_version=package_version(),
            headers=manifest,
        )

    def _open_download(self, url: str, headers: dict[str, str]) -> requests.Response:
        """GET for a download, retried with a pause on 429/5xx like ``fetch`` (a download is the
        same rate limit: the domain budget is taken per attempt)."""
        limits = self.config.rate_limits
        attempts = limits.retry_attempts + 1
        for attempt in range(1, attempts + 1):
            try:
                with self.session.cache_disabled():
                    response = self.session.get(
                        url,
                        stream=True,
                        headers=headers,
                        timeout=self.config.capture.timeout_seconds,
                        allow_redirects=False,  # fetched from the address that was listed
                    )
            except RateLimitTimeout as exc:
                raise CaptureBudgetError(str(exc)) from exc
            except requests.RequestException as exc:
                raise CaptureError(scrub_text(f"{redact_url(url, api=True)}: {exc}")) from exc
            if response.status_code not in _RETRY_STATUS or attempt == attempts:
                return response
            self._honor_retry_after(response)
            if not response.headers.get("Retry-After", "").isdigit():
                self._sleep(
                    min(
                        limits.retry_backoff_seconds * 2.0**attempt,
                        self.config.capture.max_wait_seconds,
                    )
                )
            response.close()
        raise CaptureError("no response received")  # unreachable

    def browser(
        self, url: str, *, row_id: int | None, stage: str, refresh: bool = False
    ) -> CaptureRecord:
        """Capture one JavaScript-heavy page with Browsertrix Crawler in Docker, as a WACZ.

        For an ``automated`` docket portal that renders its results only in a browser and whose
        terms the operator has cleared: never for a portal that is ``assisted`` or ``manual``.
        Browsertrix (AGPL) runs as a separate container, not linked. The domain's budget is taken
        first, the WACZ is stored content-addressed and a ``captures`` row names it in
        ``wacz_path``. If Docker is not installed or not running this raises ``CaptureError``: the
        caller records the row deferred, never as a negative.

        What the capture store already holds is not fetched again: an earlier WACZ of the URL is
        reused (a new ``captures`` row says so, no Docker, no budget) unless ``refresh`` is set.
        The image must be pinned to a version or digest; the resolved image id is recorded.
        """
        if urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).hostname:
            raise CaptureError(f"not a fetchable http(s) URL: {scrub_text(url)!r}")
        image = self.config.court.docket_browser_image
        tag = image_tag(image)
        if "@sha256:" not in image and tag in ("", "latest"):
            raise CaptureError(
                f"court.docket_browser_image {image!r} is not pinned to a version or digest"
            )
        clean = redact_url(url, api=False)
        if not refresh:
            earlier = self._conn.execute(
                "SELECT capture_id, content_sha256, wacz_path, tool_version FROM captures "
                "WHERE tool = 'browsertrix' AND url_requested = ? AND wacz_path IS NOT NULL "
                "ORDER BY capture_id DESC LIMIT 1",
                (clean,),
            ).fetchone()
            if earlier is not None:
                return self._insert(
                    row_id=row_id,
                    stage=stage,
                    url_requested=clean,
                    url_final=clean,
                    http_status=None,
                    content_type="application/wacz",
                    digest=earlier[1],
                    headers={"reused_capture_id": earlier[0], "image": image},
                    warc_path=None,
                    wacz_path=earlier[2],
                    tool="browsertrix",
                    tool_version=earlier[3],
                    manual_source=None,
                    site_data_sha256=None,
                    from_cache=True,
                )
        docker = shutil.which("docker")
        if docker is None:
            raise CaptureError("docker is not available: the browser capture is deferred")
        try:
            running = subprocess.run(  # noqa: S603
                [docker, "info"], capture_output=True, timeout=20, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CaptureError(f"docker is not running: {type(exc).__name__}") from exc
        if running.returncode != 0:
            raise CaptureError("docker is not running: the browser capture is deferred")
        try:
            self.limiter.acquire(urlsplit(url).hostname or "", max_wait=None)
        except RateLimitTimeout as exc:
            raise CaptureBudgetError(str(exc)) from exc
        name = f"efp-bt-{uuid.uuid4().hex[:12]}"
        # The container may write the WACZ as another user (root, on a Linux host): a cleanup that
        # cannot remove it must not turn a successful capture into an error.
        with tempfile.TemporaryDirectory(prefix="efp-wacz-", ignore_cleanup_errors=True) as scratch:
            try:
                done = subprocess.run(  # noqa: S603
                    [
                        docker, "run", "--rm", "--name", name, "-v", f"{scratch}:/crawls", image,
                        "crawl", "--url", url, "--limit", "1", "--scopeType", "page",
                        "--userAgent", self.user_agent, "--generateWACZ", "--collection", "efp",
                    ],
                    capture_output=True,
                    timeout=self.config.capture.timeout_seconds * 10,
                    check=False,
                )  # fmt: skip
            except subprocess.TimeoutExpired as exc:
                # killing the client does not stop the container: remove it by name (and if even
                # that fails the capture is still only deferred, never an unhandled error)
                try:
                    subprocess.run(  # noqa: S603
                        [docker, "rm", "-f", name], capture_output=True, timeout=30, check=False
                    )
                except (OSError, subprocess.TimeoutExpired):
                    log.warning("could not remove the timed-out container %s", name)
                raise CaptureError(f"{scrub_text(url)}: browser capture timed out") from exc
            except OSError as exc:
                raise CaptureError(f"{scrub_text(url)}: browser capture failed: {exc}") from exc
            found = sorted(Path(scratch).rglob("*.wacz"))
            if done.returncode != 0 or not found:
                raise CaptureError(f"{scrub_text(url)}: the browser capture produced no WACZ")
            if found[0].stat().st_size > MAX_WACZ_BYTES:
                raise CaptureError(
                    f"{scrub_text(url)}: the WACZ is larger than {MAX_WACZ_BYTES} bytes"
                )
            data = found[0].read_bytes()
            try:
                inspected = subprocess.run(  # noqa: S603
                    [docker, "image", "inspect", "--format", "{{.Id}}", image],
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
                image_id = inspected.stdout.decode("utf-8", errors="replace").strip()
            except (OSError, subprocess.TimeoutExpired):
                image_id = ""
        digest = write_body(self.config.paths.store, data)
        digest_pin = image.split("@sha256:", 1)[1][:19] if "@sha256:" in image else ""
        return self._insert(
            row_id=row_id,
            stage=stage,
            url_requested=clean,
            url_final=clean,
            http_status=None,
            content_type="application/wacz",
            digest=digest,
            headers={"image": image, "image_id": image_id},
            warc_path=None,
            wacz_path=body_relpath(digest),
            tool="browsertrix",
            tool_version=tag
            or digest_pin
            or image_id[:19]
            or "unknown",  # Browsertrix's, not efp's
            manual_source=None,
            site_data_sha256=None,
            from_cache=False,
        )

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

    def llm_message(
        self,
        *,
        api_key: str,
        row_id: int | None,
        stage: str,
        purpose: str,
        model: str,
        system: str,
        content: list[dict[str, Any]],
        max_tokens: int,
        thinking: Mapping[str, Any] | None = None,
        effort: str | None = None,
        timeout: float = 120.0,
    ) -> LlmResult:
        """One model call, through the capture layer: it takes the ``api.anthropic.com`` budget,
        and the request (images replaced by their hashes) and the response are stored as captures.

        There is deliberately no ``temperature`` parameter: API version 2026-10-21 rejects it, so no
        caller can send one (see ``efp/llm.py``). Any failure raises :class:`CaptureError` or a
        subclass (a budget wait that times out raises :class:`CaptureBudgetError`), never a bare SDK
        or store exception. Nothing is retried here: one call is one request, one budget slot and
        one log row, and the page is revisited by a later run.
        """
        try:
            self.limiter.acquire("api.anthropic.com", max_wait=None)
        except RateLimitTimeout as exc:
            raise CaptureBudgetError(str(exc)) from exc
        try:
            import anthropic
        except ImportError as exc:
            raise CaptureError(
                "the anthropic package is not installed (the documents extra)"
            ) from exc
        args: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": content}],
        }
        if thinking is not None:
            args["thinking"] = dict(thinking)
        if effort is not None:
            args["output_config"] = {"effort": effort}
        stored_request = {
            **{k: v for k, v in args.items() if k != "messages"},
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "media_type": b["source"]["media_type"],
                            "sha256": hashlib.sha256(b["source"]["data"].encode()).hexdigest(),
                        }
                        if b.get("type") == "image"
                        else b
                        for b in content
                    ],
                }
            ],
        }
        url = "https://api.anthropic.com/v1/messages"
        client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=0)
        try:
            message = client.messages.create(**args)
        except anthropic.APIStatusError as exc:
            stored_id: int | None = None
            try:
                stored = self.store_bytes(
                    json.dumps({"request": stored_request, "status": exc.status_code}).encode(),
                    row_id=row_id, stage=stage, url_requested=url, http_status=exc.status_code,
                    tool="llm", tool_version=model, content_type="application/json",
                    headers={"purpose": purpose, "request_id": exc.request_id or ""},
                )  # fmt: skip
                stored_id = int(stored.capture_id)
            except Exception:  # the API's refusal is the news; failing to file it must not hide it
                log.warning("could not store the model API's error answer for %s", model)
            raise LlmHttpError(
                f"{model}: HTTP {exc.status_code} ({scrub_text(exc.message)[:160]})",
                capture_id=stored_id,
                request_id=exc.request_id,
            ) from exc
        except anthropic.APIError as exc:
            # no answer, but the request went out: it may have been billed
            raise LlmCallError(f"{model}: {type(exc).__name__}") from exc
        except Exception as exc:
            # not an API answer (the SDK refused the arguments, say a version that lacks a field):
            # nothing was sent, so nothing was billed
            raise CaptureError(
                f"{model}: request not sent ({type(exc).__name__}: {exc})"[:300]
            ) from exc
        finally:
            client.close()
        usage = message.usage
        tokens = (int(usage.input_tokens or 0), int(usage.output_tokens or 0))
        try:
            text = "".join(b.text for b in message.content if b.type == "text")
            record = self.store_bytes(
                json.dumps(
                    {"request": stored_request, "response": json.loads(message.to_json())},
                    sort_keys=True,
                ).encode(),
                row_id=row_id, stage=stage, url_requested=url, http_status=200, tool="llm",
                tool_version=model, content_type="application/json",
                headers={"purpose": purpose, "request_id": message._request_id or ""},
            )  # fmt: skip
        except Exception as exc:  # the answer was billed whatever happened to it afterwards
            raise LlmCallError(
                f"{model}: answer received but not recorded ({type(exc).__name__})",
                input_tokens=tokens[0],
                output_tokens=tokens[1],
                request_id=message._request_id,
            ) from exc
        return LlmResult(
            text=text,
            model=message.model,
            input_tokens=tokens[0],
            output_tokens=tokens[1],
            request_id=message._request_id,
            stop_reason=message.stop_reason,
            capture_id=int(record.capture_id),
        )

    def close(self) -> None:
        self.session.close()  # type: ignore[no-untyped-call]
        self.limiter.close()

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
            max_attempts = limits.retry_attempts + 1 if retry else 1
            for attempt in stamina.retry_context(
                on=(requests.ConnectionError, requests.Timeout, _RetryableStatus),
                attempts=max_attempts,
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
                        if attempt.num < max_attempts:  # only when another attempt follows
                            self._honor_retry_after(response)
                        raise _RetryableStatus(response)
        except _RetryableStatus as exc:
            return exc.response  # retries exhausted: a 429/5xx is still a capture
        except RateLimitTimeout as exc:
            raise CaptureBudgetError(str(exc)) from exc
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
        wacz_path: str | None = None,
    ) -> CaptureRecord:
        stamp = (retrieved_at or self._clock()).astimezone(UTC).isoformat(timespec="seconds")
        body_path = body_relpath(digest)
        cursor = self._conn.execute(
            "INSERT INTO captures (row_id, stage, url_requested, url_final, retrieved_at, "
            "http_status, content_type, content_sha256, body_path, headers_json, warc_path, "
            "tool, tool_version, site_data_sha256, manual_source, wacz_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                wacz_path,
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

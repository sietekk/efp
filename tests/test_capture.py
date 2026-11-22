from __future__ import annotations

import gzip
import socket
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
import requests
import responses
import stamina

from efp.capture import (
    REDACTED,
    Capture,
    CaptureBudgetError,
    CaptureError,
    redact_headers,
    redact_url,
)
from efp.config import Config
from efp.store import config_store_path
from tests.capturehelpers import captures, headers_of, make_capture, warc_records
from tests.dbhelpers import insert_row

PAGE = b"<html><body>A public page.</body></html>"


@pytest.fixture(autouse=True)
def _fast_retries() -> Iterator[None]:
    stamina.set_testing(True, attempts=4, cap=False)  # retry without waiting
    yield
    stamina.set_testing(False)


@pytest.fixture
def rsps() -> Iterator[responses.RequestsMock]:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield mock


@pytest.fixture
def cap(config: Config, conn: sqlite3.Connection) -> Iterator[Capture]:
    insert_row(conn, 1)
    conn.commit()
    client = make_capture(config, conn)
    yield client
    client.close()


def _robots_ok(rsps: responses.RequestsMock, host: str = "example.test") -> None:
    rsps.get(f"https://{host}/robots.txt", body="User-agent: *\nDisallow: /private\n", status=200)


# --- the fetch itself -----------------------------------------------------------------------


def test_a_fetch_produces_a_captures_row_a_warc_pair_and_a_matching_body(
    cap: Capture, config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    _robots_ok(rsps)
    rsps.get("https://example.test/a", body=PAGE, content_type="text/html")
    record = cap.fetch("https://example.test/a", row_id=1, stage="resolve_links")
    assert record.ok and record.http_status == 200 and not record.from_cache
    assert record.tool == "requests" and record.url_requested == "https://example.test/a"
    assert cap.read_body(record) == PAGE  # hash-verified read
    assert config_store_path(record.content_sha256, config).read_bytes() == PAGE
    row = next(r for r in captures(conn) if r["url_requested"].endswith("/a"))
    assert row["body_path"] == f"store/{record.content_sha256[:2]}/{record.content_sha256}"
    assert row["retrieved_at"].endswith("+00:00") and row["content_type"] == "text/html"
    pair = [r for r in warc_records(config) if r["uri"] == "https://example.test/a"]
    assert [r["type"] for r in pair] == ["request", "response"]
    response = pair[1]
    assert response["payload"] == PAGE and response["payload_sha256"] == record.content_sha256
    assert response["efp_sha256"] == record.content_sha256 and response["status"] == "200"
    assert row["warc_path"].split("#")[0].endswith(".warc.gz")


def test_the_project_user_agent_is_sent(
    cap: Capture, config: Config, rsps: responses.RequestsMock
) -> None:
    _robots_ok(rsps)
    rsps.get("https://example.test/ua", body=PAGE)
    cap.fetch("https://example.test/ua", row_id=1, stage="resolve_links")
    sent = [c.request.headers["User-Agent"] for c in rsps.calls]
    assert set(sent) == {config.project.user_agent}


def test_a_redirect_records_both_urls_and_the_hop(
    cap: Capture, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    _robots_ok(rsps)
    rsps.get(
        "https://example.test/old", status=301, headers={"Location": "https://example.test/new"}
    )
    rsps.get("https://example.test/new", body=PAGE)
    record = cap.fetch("https://example.test/old", row_id=1, stage="resolve_links")
    assert record.url_requested == "https://example.test/old"
    assert record.url_final == "https://example.test/new"
    final = next(r for r in captures(conn) if (r["url_final"] or "").endswith("/new"))
    assert headers_of(final)["history"] == [{"url": "https://example.test/old", "status": 301}]
    assert cap.adapter.network_requests == 3  # robots.txt, the redirect, the target


def test_a_429_is_retried_and_retry_after_is_honored(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    insert_row(conn, 1)
    sleeps: list[float] = []
    cap = make_capture(config, conn, sleeps=sleeps)
    _robots_ok(rsps)
    rsps.get("https://example.test/busy", status=429, headers={"Retry-After": "7"})
    rsps.get("https://example.test/busy", body=PAGE)
    record = cap.fetch("https://example.test/busy", row_id=1, stage="resolve_links")
    assert record.http_status == 200 and cap.read_body(record) == PAGE
    assert sleeps == [7.0]
    assert len([c for c in rsps.calls if c.request.url.endswith("/busy")]) == 2
    cap.close()


def test_exhausted_retries_still_capture_the_last_response(
    cap: Capture, rsps: responses.RequestsMock, config: Config
) -> None:
    _robots_ok(rsps)
    rsps.get("https://example.test/down", status=503, body=b"unavailable")
    record = cap.fetch("https://example.test/down", row_id=1, stage="resolve_links")
    assert record.http_status == 503 and not record.ok  # capture before judging
    calls = [c for c in rsps.calls if c.request.url.endswith("/down")]
    assert len(calls) == config.rate_limits.retry_attempts + 1


def test_a_404_is_captured_not_judged(cap: Capture, rsps: responses.RequestsMock) -> None:
    _robots_ok(rsps)
    rsps.get("https://example.test/gone", status=404, body=b"not found")
    record = cap.fetch("https://example.test/gone", row_id=1, stage="resolve_links")
    assert record.http_status == 404 and cap.read_body(record) == b"not found"


def test_a_connection_error_is_a_capture_error_and_writes_no_row_for_the_page(
    cap: Capture, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    _robots_ok(rsps)
    rsps.get("https://example.test/x", body=requests.ConnectionError("reset"))
    with pytest.raises(CaptureError, match="ConnectionError"):
        cap.fetch("https://example.test/x", row_id=1, stage="resolve_links")
    assert [r["url_requested"] for r in captures(conn)] == ["https://example.test/robots.txt"]


@pytest.mark.parametrize(
    "url", ["ftp://example.test/x", "file:///etc/passwd", "not a url", "https:///x"]
)
def test_only_http_urls_are_fetchable(cap: Capture, url: str) -> None:
    with pytest.raises(CaptureError, match="fetchable"):
        cap.fetch(url, row_id=1, stage="resolve_links")


# --- robots.txt -----------------------------------------------------------------------------


def test_a_robots_disallow_is_a_row_with_no_status_and_no_request(
    cap: Capture, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    _robots_ok(rsps)
    record = cap.fetch("https://example.test/private/doc", row_id=1, stage="resolve_links")
    assert (record.http_status, record.tool) == (None, "robots_disallow")
    assert b"Disallow: /private" in cap.read_body(record)  # the evidence is the rule itself
    assert record.url_requested == "https://example.test/private/doc"
    assert not [c for c in rsps.calls if "/private/doc" in c.request.url]  # never fetched
    assert "robots_txt" in {r["tool"] for r in captures(conn)}


def test_robots_is_fetched_once_per_domain_and_cached(
    cap: Capture, rsps: responses.RequestsMock
) -> None:
    _robots_ok(rsps)
    rsps.get("https://example.test/a", body=PAGE)
    rsps.get("https://example.test/b", body=PAGE + b"b")
    cap.fetch("https://example.test/a", row_id=1, stage="resolve_links")
    cap.fetch("https://example.test/b", row_id=1, stage="resolve_links")
    assert len([c for c in rsps.calls if c.request.url.endswith("/robots.txt")]) == 1


@pytest.mark.parametrize(
    ("robots_status", "expect_fetch"), [(404, True), (410, True), (403, False), (500, False)]
)
def test_robots_status_decides_conservatively(
    cap: Capture, rsps: responses.RequestsMock, robots_status: int, expect_fetch: bool
) -> None:
    rsps.get("https://example.test/robots.txt", status=robots_status, body=b"")
    rsps.get("https://example.test/a", body=PAGE)
    record = cap.fetch("https://example.test/a", row_id=1, stage="resolve_links")
    assert (record.tool != "robots_disallow") is expect_fetch


def test_api_calls_skip_robots(cap: Capture, rsps: responses.RequestsMock) -> None:
    rsps.get("https://example.test/api/q", json={"ok": True})
    record = cap.fetch("https://example.test/api/q", row_id=None, stage="resolve_links", kind="api")
    assert record.ok and not [c for c in rsps.calls if c.request.url.endswith("/robots.txt")]


# --- the HTTP cache, dedup and store_bytes ----------------------------------------------------


def test_a_cache_hit_still_records_a_row_but_not_a_second_warc_pair(
    cap: Capture, conn: sqlite3.Connection, config: Config, rsps: responses.RequestsMock
) -> None:
    _robots_ok(rsps)
    rsps.get("https://example.test/c", body=PAGE, headers={"Cache-Control": "max-age=3600"})
    first = cap.fetch("https://example.test/c", row_id=1, stage="resolve_links")
    second = cap.fetch("https://example.test/c", row_id=1, stage="resolve_links")
    assert (first.tool, second.tool) == ("requests", "requests-cache") and second.from_cache
    assert second.warc_path == first.warc_path  # the evidentiary copy is the original record
    assert len([c for c in rsps.calls if c.request.url.endswith("/c")]) == 1
    rows = [r for r in captures(conn) if r["url_requested"].endswith("/c")]
    assert len(rows) == 2 and rows[0]["content_sha256"] == rows[1]["content_sha256"]
    pages = [r for r in warc_records(config) if r["uri"] == "https://example.test/c"]
    assert [r["type"] for r in pages] == ["request", "response"]  # one pair, not two


def test_the_same_body_twice_is_one_file_and_two_rows(
    cap: Capture, conn: sqlite3.Connection, config: Config, rsps: responses.RequestsMock
) -> None:
    _robots_ok(rsps)
    rsps.get("https://example.test/one", body=PAGE)
    rsps.get("https://example.test/two", body=PAGE)
    a = cap.fetch("https://example.test/one", row_id=1, stage="resolve_links")
    b = cap.fetch("https://example.test/two", row_id=1, stage="resolve_links")
    assert a.content_sha256 == b.content_sha256 and a.capture_id != b.capture_id
    shard = config_store_path(a.content_sha256, config).parent
    assert [p.name for p in shard.iterdir() if p.name == a.content_sha256] == [a.content_sha256]
    assert len([r for r in captures(conn) if r["content_sha256"] == a.content_sha256]) == 2


def test_store_bytes_captures_a_body_that_arrived_another_way(
    cap: Capture, conn: sqlite3.Connection
) -> None:
    record = cap.store_bytes(
        b"%PDF-1.7 synthetic",
        row_id=1,
        stage="documents",
        url_requested="file://saved.pdf",
        tool="manual_import",
        manual_source="westlaw",
        content_type="application/pdf",
    )
    row = captures(conn)[-1]
    assert (row["manual_source"], row["tool"], row["warc_path"]) == (
        "westlaw",
        "manual_import",
        None,
    )
    assert cap.read_body(record) == b"%PDF-1.7 synthetic"


# --- secrets ----------------------------------------------------------------------------------


def test_secrets_never_reach_headers_urls_the_warc_or_the_http_cache(
    cap: Capture, conn: sqlite3.Connection, config: Config, rsps: responses.RequestsMock
) -> None:
    secret = "tok-0123456789abcdef"  # pragma: allowlist secret (synthetic test value)
    rsps.get("https://example.test/api/data", json={"ok": 1})
    record = cap.fetch(
        "https://example.test/api/data",
        row_id=None,
        stage="court",
        kind="api",
        headers={"Authorization": f"Bearer {secret}", "X-API-Key": secret},
        params={"api_key": secret, "q": "ok"},
    )
    assert record.ok
    row = captures(conn)[-1]
    stored = " ".join(str(v) for v in dict(row).values())
    assert secret not in stored and REDACTED in headers_of(row)["request"]["Authorization"]
    assert "q=ok" in row["url_requested"] and f"api_key={REDACTED}" in row["url_requested"]
    warcs = list(Path(config.paths.warc).rglob("*.warc.gz"))
    assert warcs
    for path in warcs:
        with gzip.open(path, "rb") as handle:
            assert secret.encode() not in handle.read(), "the WARC must hold no secret"
    for path in Path(config.paths.cache).glob("http_cache*"):
        assert secret.encode() not in path.read_bytes(), "the HTTP cache must hold no secret"
    assert secret in rsps.calls[-1].request.headers["Authorization"]  # but it really was sent


def test_redaction_helpers() -> None:
    assert (
        redact_url("https://x.test/a?api_key=S&k=v") == f"https://x.test/a?api_key={REDACTED}&k=v"
    )
    assert redact_url("https://x.test/a?key=S", api=True) == f"https://x.test/a?key={REDACTED}"
    assert redact_url("https://x.test/a?key=S") == "https://x.test/a?key=S"  # a page, not an API
    assert redact_headers({"Authorization": "Bearer S", "Accept": "x"}) == [
        ("Authorization", REDACTED),
        ("Accept", "x"),
    ]


# --- the Save Page Now queue ---------------------------------------------------------------


def test_successful_pages_are_queued_once_and_apis_and_failures_are_not(
    cap: Capture, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    _robots_ok(rsps)
    rsps.get("https://example.test/p", body=PAGE, headers={"Cache-Control": "max-age=3600"})
    rsps.get("https://example.test/missing", status=404)
    rsps.get("https://example.test/api/x", json={})
    cap.fetch("https://example.test/p", row_id=1, stage="resolve_links")
    cap.fetch("https://example.test/p", row_id=1, stage="resolve_links")  # cache hit
    cap.fetch("https://example.test/missing", row_id=1, stage="resolve_links")
    cap.fetch("https://example.test/api/x", row_id=None, stage="x", kind="api")
    queued = conn.execute("SELECT url, status FROM spn_queue").fetchall()
    assert queued == [("https://example.test/p", "pending")]


def test_the_network_stays_blocked_in_this_module() -> None:
    with socket.socket() as probe, pytest.raises(RuntimeError, match="network access is blocked"):
        probe.connect(("example.com", 80))


# --- review follow-ups: redirects, robots with parameters, secrets in other paths --------------


def test_a_redirect_into_a_disallowed_path_is_blocked_before_it_is_requested(
    cap: Capture, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    _robots_ok(rsps)
    rsps.get(
        "https://example.test/open",
        status=302,
        headers={"Location": "https://example.test/private/x"},
    )
    rsps.get("https://example.test/private/x", body=PAGE)
    record = cap.fetch("https://example.test/open", row_id=1, stage="resolve_links")
    assert (record.http_status, record.tool) == (None, "robots_disallow")
    assert record.url_requested == "https://example.test/open"
    assert not [c for c in rsps.calls if c.request.url.endswith("/private/x")]
    assert headers_of(captures(conn)[-1])["redirected_via"] == [
        {"url": "https://example.test/open", "status": 302}
    ]


def test_each_redirect_hop_checks_its_own_hosts_robots_and_drops_credentials_across_hosts(
    cap: Capture, config: Config, rsps: responses.RequestsMock
) -> None:
    _robots_ok(rsps)
    _robots_ok(rsps, "other.test")
    rsps.get(
        "https://example.test/go", status=301, headers={"Location": "https://other.test/landing"}
    )
    rsps.get("https://other.test/landing", body=PAGE)
    record = cap.fetch(
        "https://example.test/go",
        row_id=1,
        stage="resolve_links",
        headers={"Authorization": "Bearer hop-secret-1"},
    )
    assert record.url_final == "https://other.test/landing" and record.ok
    urls = [c.request.url for c in rsps.calls]
    assert "https://other.test/robots.txt" in urls  # the second host's rules were read
    landing = next(c for c in rsps.calls if c.request.url.endswith("/landing"))
    assert "Authorization" not in landing.request.headers  # not carried to a different host
    first = next(c for c in rsps.calls if c.request.url.endswith("/go"))
    assert first.request.headers["Authorization"] == "Bearer hop-secret-1"


def test_the_warc_holds_every_hop_so_the_original_url_replays(
    cap: Capture, config: Config, rsps: responses.RequestsMock
) -> None:
    _robots_ok(rsps)
    rsps.get(
        "https://example.test/old", status=301, headers={"Location": "https://example.test/new"}
    )
    rsps.get("https://example.test/new", body=PAGE)
    cap.fetch("https://example.test/old", row_id=1, stage="resolve_links")
    seen = [(r["uri"], r["status"]) for r in warc_records(config) if r["type"] == "response"]
    assert ("https://example.test/old", "301") in seen and (
        "https://example.test/new",
        "200",
    ) in seen


def test_a_redirect_loop_is_an_error(cap: Capture, rsps: responses.RequestsMock) -> None:
    rsps.get("https://other.test/robots.txt", status=404)  # (example.test has a daily cap here)
    rsps.get("https://other.test/a", status=302, headers={"Location": "https://other.test/b"})
    rsps.get("https://other.test/b", status=302, headers={"Location": "https://other.test/a"})
    with pytest.raises(CaptureError, match="too many redirects"):
        cap.fetch("https://other.test/a", row_id=1, stage="resolve_links")


def test_robots_rules_apply_to_the_query_string_parameters(
    cap: Capture, rsps: responses.RequestsMock
) -> None:
    rsps.get("https://example.test/robots.txt", body="User-agent: *\nDisallow: /search?q=\n")
    rsps.get("https://example.test/search", body=PAGE)
    record = cap.fetch(
        "https://example.test/search", row_id=1, stage="resolve_links", params={"q": "x"}
    )
    assert record.tool == "robots_disallow"
    assert not [
        c for c in rsps.calls if "/search" in c.request.url and "robots" not in c.request.url
    ]


def test_a_429_on_robots_txt_does_not_open_the_whole_site(
    cap: Capture, rsps: responses.RequestsMock
) -> None:
    rsps.get("https://example.test/robots.txt", status=429)
    rsps.get("https://example.test/a", body=PAGE)
    assert (
        cap.fetch("https://example.test/a", row_id=1, stage="resolve_links").tool
        == "robots_disallow"
    )


def test_store_bytes_does_not_keep_credentials(cap: Capture, conn: sqlite3.Connection) -> None:
    cap.store_bytes(
        b"downloaded",
        row_id=1,
        stage="documents",
        url_requested="https://example.test/f?token=abc123&key=k9&page=2",
        tool="download",
        headers={"Authorization": "Bearer abc123", "request": {"Cookie": "s=abc123"}, "n": 1},
    )
    row = captures(conn)[-1]
    stored = " ".join(str(v) for v in dict(row).values())
    assert "abc123" not in stored and "k9" not in stored and "page=2" in stored


def test_an_error_message_carries_no_credential_even_one_nobody_registered(
    cap: Capture, rsps: responses.RequestsMock
) -> None:
    rsps.get("https://example.test/api/x", body=requests.ConnectionError("reset for ?key=SEKRET77"))
    with pytest.raises(CaptureError) as excinfo:
        cap.fetch(
            "https://example.test/api/x",
            row_id=None,
            stage="x",
            kind="api",
            params={"key": "SEKRET77"},
        )
    assert "SEKRET77" not in str(excinfo.value)


def test_a_cookie_the_server_sets_is_not_kept_in_the_cache_or_the_row(
    cap: Capture, config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    _robots_ok(rsps)
    rsps.get(
        "https://example.test/login",
        body=PAGE,
        headers={"Set-Cookie": "session=COOKIEVALUE99; Path=/"},
    )
    cap.fetch("https://example.test/login", row_id=1, stage="resolve_links")
    cap.session.close()
    for path in Path(config.paths.cache).glob("http_cache*"):
        assert b"COOKIEVALUE99" not in path.read_bytes()
    assert "COOKIEVALUE99" not in " ".join(str(v) for c in captures(conn) for v in dict(c).values())


def test_the_capture_methods_delegate_to_the_wayback_and_perma_modules(
    cap: Capture, rsps: responses.RequestsMock
) -> None:
    rsps.get("https://web.archive.org/cdx/search/cdx", json=[["urlkey"]])
    assert cap.cdx("http://example.test/") == []
    result = cap.perma("https://example.test/cited")  # no secrets passed: a documented no-op
    assert result.perma_url is None and "PERMA_API_KEY" in result.reason


def test_every_capture_is_logged_without_credentials(
    cap: Capture, rsps: responses.RequestsMock, caplog: pytest.LogCaptureFixture
) -> None:
    _robots_ok(rsps)
    rsps.get("https://example.test/p", body=PAGE)
    with caplog.at_level("INFO", logger="efp.capture"):
        cap.fetch("https://example.test/p?api_key=LOGSECRET1", row_id=1, stage="resolve_links")
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "captured https://example.test/p" in text and "LOGSECRET1" not in text


def test_the_budget_is_taken_for_every_network_hop_on_its_own_domain(
    cap: Capture, rsps: responses.RequestsMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    taken: list[str] = []
    real = cap.limiter.acquire
    monkeypatch.setattr(
        cap.limiter, "acquire", lambda domain, **kw: (taken.append(domain), real(domain, **kw))[1]
    )
    _robots_ok(rsps)
    rsps.get("https://other.test/robots.txt", status=404)
    rsps.get(
        "https://example.test/go", status=301, headers={"Location": "https://other.test/there"}
    )
    rsps.get("https://other.test/there", body=PAGE)
    cap.fetch("https://example.test/go", row_id=1, stage="resolve_links")
    assert taken == ["example.test", "example.test", "other.test", "other.test"]
    # robots.txt and the first hop on example.test, then robots.txt and the page on other.test


def test_an_api_call_redirected_off_its_host_gets_that_hosts_robots_txt_checked(
    cap: Capture, rsps: responses.RequestsMock
) -> None:
    rsps.get(
        "https://example.test/api/q",
        status=302,
        headers={"Location": "https://other.test/private/x"},
    )
    rsps.get("https://other.test/robots.txt", body="User-agent: *\nDisallow: /private\n")
    rsps.get("https://other.test/private/x", json={"ok": True})
    record = cap.fetch("https://example.test/api/q", row_id=None, stage="x", kind="api")
    assert record.tool == "robots_disallow"
    assert not [c for c in rsps.calls if c.request.url.endswith("/private/x")]


def test_download_finalizes_a_complete_part_without_a_request(
    cap: Capture, rsps: responses.RequestsMock, tmp_path: Path
) -> None:
    dest = tmp_path / "bulk" / "f.csv"
    dest.parent.mkdir()
    (tmp_path / "bulk" / "f.csv.part").write_bytes(b"abcdef")  # killed before the rename
    record = cap.download("https://example.test/f.csv", dest, stage="bulk", expected_size=6)
    assert dest.read_bytes() == b"abcdef" and not (tmp_path / "bulk" / "f.csv.part").exists()
    assert record.http_status == 200 and not rsps.calls


def test_download_drops_an_oversized_part_and_restarts(
    cap: Capture, rsps: responses.RequestsMock, tmp_path: Path
) -> None:
    rsps.get("https://example.test/robots.txt", status=404)
    dest = tmp_path / "f.csv"
    (tmp_path / "f.csv.part").write_bytes(b"abcdefgh")
    rsps.get("https://example.test/f.csv", body=b"abcdef")
    cap.download("https://example.test/f.csv", dest, stage="bulk", expected_size=6)
    assert dest.read_bytes() == b"abcdef"


def test_download_416_drops_the_stale_part_and_restarts_from_zero(
    cap: Capture, rsps: responses.RequestsMock, tmp_path: Path
) -> None:
    rsps.get("https://example.test/robots.txt", status=404)
    dest = tmp_path / "f.csv"
    (tmp_path / "f.csv.part").write_bytes(b"abc")  # shorter than the file, but not a prefix of it
    (tmp_path / "f.csv.part.etag").write_text('"v1"')
    rsps.add(
        responses.GET,
        "https://example.test/f.csv",
        status=416,
        match=[responses.matchers.header_matcher({"Range": "bytes=3-"})],
    )
    rsps.get("https://example.test/f.csv", body=b"abcdef")
    cap.download("https://example.test/f.csv", dest, stage="bulk", expected_size=6)
    assert dest.read_bytes() == b"abcdef" and not (tmp_path / "f.csv.part").exists()
    gets = [c for c in rsps.calls if c.request.url.endswith("f.csv")]
    assert [c.request.headers.get("Range") for c in gets] == ["bytes=3-", None]


def test_download_wrong_size_does_not_leave_an_unresumable_part(
    cap: Capture, rsps: responses.RequestsMock, tmp_path: Path
) -> None:
    rsps.get("https://example.test/robots.txt", status=404)
    rsps.get("https://example.test/f.csv", body=b"abcdef")
    with pytest.raises(CaptureError, match="expected 9"):
        cap.download(
            "https://example.test/f.csv", tmp_path / "f.csv", stage="bulk", expected_size=9
        )
    assert not (tmp_path / "f.csv.part").exists()


def test_download_retries_a_503_then_succeeds(
    cap: Capture, rsps: responses.RequestsMock, tmp_path: Path
) -> None:
    rsps.get("https://example.test/robots.txt", status=404)
    rsps.get("https://example.test/f.csv", status=503)
    rsps.get("https://example.test/f.csv", body=b"abcdef")
    cap.download("https://example.test/f.csv", tmp_path / "f.csv", stage="bulk", expected_size=6)
    assert (tmp_path / "f.csv").read_bytes() == b"abcdef"
    assert len([c for c in rsps.calls if c.request.url.endswith("f.csv")]) == 2


def test_download_honors_robots_txt(
    cap: Capture, rsps: responses.RequestsMock, tmp_path: Path
) -> None:
    rsps.get("https://example.test/robots.txt", body="User-agent: *\nDisallow: /bulk/\n")
    with pytest.raises(CaptureError, match="robots"):
        cap.download("https://example.test/bulk/f.csv", tmp_path / "f.csv", stage="bulk")
    assert not (tmp_path / "f.csv.part").exists()
    assert not [c for c in rsps.calls if c.request.url.endswith("f.csv")]


def test_wait_limit_is_per_thread(cap: Capture) -> None:
    import threading

    seen: list[float | None] = []
    with cap.wait_limit(5):
        worker = threading.Thread(target=lambda: seen.append(cap.adapter.max_wait))
        worker.start()
        worker.join()
        assert cap.adapter.max_wait == 5
    assert seen == [None] and cap.adapter.max_wait is None


def test_download_resumes_after_the_connection_breaks_mid_stream(
    cap: Capture, rsps: responses.RequestsMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rsps.get("https://example.test/robots.txt", status=404)
    rsps.get("https://example.test/f.csv", body=b"abcdef", headers={"ETag": '"v1"'})
    rsps.get(
        "https://example.test/f.csv",
        body=b"def",
        status=206,
        headers={"Content-Range": "bytes 3-5/6", "ETag": '"v1"'},
    )
    real = requests.Response.iter_content
    state = {"broke": False}

    def flaky(self, chunk_size=1, decode_unicode=False):  # type: ignore[no-untyped-def]
        if self.url.endswith("f.csv") and not state["broke"]:
            state["broke"] = True
            yield b"abc"
            raise requests.exceptions.ChunkedEncodingError("connection reset")
        yield from real(self, chunk_size, decode_unicode)

    monkeypatch.setattr(requests.Response, "iter_content", flaky)
    dest = tmp_path / "f.csv"
    cap.download("https://example.test/f.csv", dest, stage="bulk", expected_size=6)
    assert dest.read_bytes() == b"abcdef"
    gets = [c for c in rsps.calls if c.request.url.endswith("f.csv")]
    assert [c.request.headers.get("Range") for c in gets] == [None, "bytes=3-"]


def test_download_that_keeps_breaking_ends_as_a_capture_error_with_the_part_kept(
    cap: Capture, rsps: responses.RequestsMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rsps.get("https://example.test/robots.txt", status=404)
    rsps.get("https://example.test/f.csv", body=b"abcdef")

    real = requests.Response.iter_content

    def broken(self, chunk_size=1, decode_unicode=False):  # type: ignore[no-untyped-def]
        if not self.url.endswith("f.csv"):
            yield from real(self, chunk_size, decode_unicode)
            return
        raise requests.exceptions.ConnectionError("reset")

    monkeypatch.setattr(requests.Response, "iter_content", broken)
    with pytest.raises(CaptureError, match="run the download again"):
        cap.download("https://example.test/f.csv", tmp_path / "f.csv", stage="bulk")


def test_a_resume_sends_if_range_and_restarts_when_the_file_was_replaced(
    cap: Capture, rsps: responses.RequestsMock, tmp_path: Path
) -> None:
    rsps.get("https://example.test/robots.txt", status=404)
    dest = tmp_path / "f.csv"
    (tmp_path / "f.csv.part").write_bytes(b"old")
    (tmp_path / "f.csv.part.etag").write_text('"v1"')
    # the server's file changed (a new ETag): it ignores the range and sends the new file whole
    rsps.get("https://example.test/f.csv", body=b"newnew", headers={"ETag": '"v2"'})
    cap.download("https://example.test/f.csv", dest, stage="bulk", expected_size=6)
    gets = [c for c in rsps.calls if c.request.url.endswith("f.csv")]
    assert gets[0].request.headers["If-Range"] == '"v1"'
    assert dest.read_bytes() == b"newnew" and not (tmp_path / "f.csv.part.etag").exists()


def test_a_robots_blocked_download_is_recorded_under_the_blocked_url(
    cap: Capture, rsps: responses.RequestsMock, tmp_path: Path
) -> None:
    rsps.get("https://example.test/robots.txt", body="User-agent: *\nDisallow: /bulk/\n")
    url = "https://example.test/bulk/f.csv"
    with pytest.raises(CaptureError, match="robots"):
        cap.download(url, tmp_path / "f.csv", stage="bulk")
    row = cap._conn.execute(
        "SELECT url_requested, tool FROM captures WHERE tool = 'robots_disallow'"
    ).fetchone()
    assert row == (url, "robots_disallow")  # the provenance row names the file, not the verb


def test_a_domain_with_a_daily_cap_of_zero_is_a_budget_error_and_nothing_is_sent(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    off = config.model_copy(
        update={
            "rate_limits": type(config.rate_limits).model_validate(
                {**config.rate_limits.model_dump(), "daily_caps": {"example.test": 0}}
            )
        }
    )
    insert_row(conn, 1)
    conn.commit()
    capture = make_capture(off, conn)
    try:
        with pytest.raises(CaptureBudgetError, match="switched off"):
            capture.fetch("https://example.test/api/q", row_id=None, stage="x", kind="api")
        assert not rsps.calls
    finally:
        capture.close()


def test_retry_false_does_not_sleep_on_retry_after_and_the_last_attempt_does_not_either(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    stamina.set_testing(True, attempts=1, cap=False)  # this file's fixture forces 4: undo for one
    insert_row(conn, 1)
    sleeps: list[float] = []
    cap = make_capture(config, conn, sleeps=sleeps)
    _robots_ok(rsps)
    rsps.get("https://example.test/busy", status=429, headers={"Retry-After": "250"})
    record = cap.fetch("https://example.test/busy", row_id=1, stage="x", kind="api", retry=False)
    assert record.http_status == 429  # returned at once...
    assert sleeps == []  # ...with no dead time spent on a retry that will not happen
    assert len([c for c in rsps.calls if c.request.url.endswith("/busy")]) == 1
    cap.close()


def test_the_final_attempt_of_a_retried_fetch_does_not_sleep_after_it(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    insert_row(conn, 1)
    sleeps: list[float] = []
    cap = make_capture(config, conn, sleeps=sleeps)
    _robots_ok(rsps)
    rsps.get("https://example.test/busy", status=429, headers={"Retry-After": "7"})
    record = cap.fetch("https://example.test/busy", row_id=1, stage="x", kind="api")
    attempts = config.rate_limits.retry_attempts + 1
    assert record.http_status == 429
    assert sleeps == [7.0] * (attempts - 1)  # between attempts only, never after the last
    cap.close()


def test_a_part_with_no_validator_is_not_resumed_into_a_possibly_different_file(
    cap: Capture, rsps: responses.RequestsMock, tmp_path: Path
) -> None:
    rsps.get("https://example.test/robots.txt", status=404)
    dest = tmp_path / "f.csv"
    (tmp_path / "f.csv.part").write_bytes(b"old")  # begun without an ETag or Last-Modified
    rsps.get("https://example.test/f.csv", body=b"newnew")
    cap.download("https://example.test/f.csv", dest, stage="bulk", expected_size=6)
    gets = [c for c in rsps.calls if c.request.url.endswith("f.csv")]
    assert [c.request.headers.get("Range") for c in gets] == [None]  # asked for the whole file
    assert dest.read_bytes() == b"newnew"


def test_a_last_modified_date_is_the_validator_when_there_is_no_strong_etag(
    cap: Capture, rsps: responses.RequestsMock, tmp_path: Path
) -> None:
    rsps.get("https://example.test/robots.txt", status=404)
    dest = tmp_path / "f.csv"
    (tmp_path / "f.csv.part").write_bytes(b"abc")
    stamp = "Wed, 01 Jul 2026 00:00:00 GMT"
    (tmp_path / "f.csv.part.etag").write_text(stamp)
    rsps.get(
        "https://example.test/f.csv",
        body=b"def",
        status=206,
        headers={"Content-Range": "bytes 3-5/6", "Last-Modified": stamp},
    )
    cap.download("https://example.test/f.csv", dest, stage="bulk", expected_size=6)
    gets = [c for c in rsps.calls if c.request.url.endswith("f.csv")]
    assert gets[0].request.headers["If-Range"] == stamp and gets[0].request.headers["Range"]
    assert dest.read_bytes() == b"abcdef"


def test_a_416_for_a_part_that_is_the_whole_file_finishes_it_without_restarting(
    cap: Capture, rsps: responses.RequestsMock, tmp_path: Path
) -> None:
    rsps.get("https://example.test/robots.txt", status=404)
    dest = tmp_path / "f.csv"
    (tmp_path / "f.csv.part").write_bytes(b"abcdef")  # complete; its size was not known beforehand
    (tmp_path / "f.csv.part.etag").write_text('"v1"')
    rsps.get(
        "https://example.test/f.csv",
        status=416,
        headers={"Content-Range": "bytes */6", "ETag": '"v1"'},
    )
    record = cap.download("https://example.test/f.csv", dest, stage="bulk")
    gets = [c for c in rsps.calls if c.request.url.endswith("f.csv")]
    assert len(gets) == 1 and dest.read_bytes() == b"abcdef"  # finished, not downloaded again
    assert record.http_status == 200 and not (tmp_path / "f.csv.part").exists()


def test_run_state_is_cleared_with_the_throttles_at_the_start_of_a_run(cap: Capture) -> None:
    cap.run_state["doj_title_works"] = False
    cap.mark_throttled("courtlistener")
    assert cap.once_per_run("x") and not cap.once_per_run("x")
    cap.clear_throttles()
    assert cap.run_state == {} and not cap.is_throttled("courtlistener") and cap.once_per_run("x")

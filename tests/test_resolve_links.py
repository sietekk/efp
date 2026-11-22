from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import requests
import responses
import stamina

import efp.resolve_links as component
from efp import runner as rn
from efp.config import Config, load_config
from efp.db import connect
from efp.resolve_links import resolve_row
from efp.resolve_links.modules import long_url_from_info_page
from tests.capturehelpers import captures
from tests.linkhelpers import (
    LONG_HTML,
    STUB_HTML,
    add_row,
    common_crawl_member,
    links_of,
    make_ctx,
    row_of,
)


@pytest.fixture(autouse=True)
def _no_retries() -> Iterator[None]:
    stamina.set_testing(True, attempts=1, cap=False)
    yield
    stamina.set_testing(False)


@pytest.fixture
def rsps() -> Iterator[responses.RequestsMock]:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield mock


FULL_CHAIN = (
    'modules = ["resolve.live", "resolve.bitly_info", "resolve.wayback_short", '
    '"resolve.wayback_target", "resolve.commoncrawl"]'
)


def _config(write_config: Callable[..., Path], **replace: str) -> Config:
    """The suite's config with the full five-module ladder (the fixture lists only one)."""
    return load_config(write_config({'modules = ["resolve.live"]': FULL_CHAIN, **replace}))


@pytest.fixture
def config(write_config: Callable[..., Path]) -> Config:
    return _config(write_config)


@pytest.fixture
def env(config: Config, conn: sqlite3.Connection) -> Iterator[tuple[Config, sqlite3.Connection]]:
    yield config, conn


def _resolve(
    config: Config, conn: sqlite3.Connection, row_id: int
) -> tuple[sqlite3.Row, dict[str, object]]:
    ctx, cap, registry = make_ctx(config, conn)
    result = resolve_row(row_of(conn, row_id), ctx, registry)
    cap.close()
    assert result.status == "done"
    (link, *_rest) = links_of(conn, row_id)
    return link, result.summary_json


# --- step 1: live -----------------------------------------------------------------------------


def test_a_live_page_is_live_and_its_text_is_captured_separately(
    env: tuple[Config, sqlite3.Connection], rsps: responses.RequestsMock
) -> None:
    config, conn = env
    add_row(conn, 1, source="https://news.test/story")
    rsps.get("https://news.test/story", body=LONG_HTML, content_type="text/html")
    link, summary = _resolve(config, conn, 1)
    assert (link["status"], link["expansion_method"]) == ("live", "live")
    assert link["url_expanded"] == "https://news.test/story" and link["resolved_at"]
    rows = captures(conn)
    body = next(r for r in rows if r["capture_id"] == link["capture_id"])
    assert body["tool"] == "requests" and body["content_sha256"]
    text = next(r for r in rows if r["tool"] == "trafilatura")
    assert json.loads(text["headers_json"])["parent_capture_id"] == link["capture_id"]
    assert (
        "county clerk"
        in Path(config.paths.store, text["body_path"].removeprefix("store/")).read_text()
    )
    assert summary["no_source"] is False


def test_a_shortener_redirect_chain_is_followed_live(
    env: tuple[Config, sqlite3.Connection], rsps: responses.RequestsMock
) -> None:
    config, conn = env
    add_row(conn, 1, source="herit.ag/abc123")
    rsps.get("https://herit.ag/abc123", status=301, headers={"Location": "https://bit.ly/zz"})
    rsps.get("https://bit.ly/zz", status=302, headers={"Location": "https://news.test/story"})
    rsps.get("https://news.test/story", body=LONG_HTML, content_type="text/html")
    link, _ = _resolve(config, conn, 1)
    assert link["url_raw"] == "herit.ag/abc123"
    assert (link["status"], link["expansion_method"]) == ("live", "live")
    assert link["url_expanded"] == "https://news.test/story"


def test_a_dead_live_page_is_captured_as_evidence_but_does_not_make_the_link_live(
    env: tuple[Config, sqlite3.Connection], rsps: responses.RequestsMock
) -> None:
    config, conn = env
    add_row(conn, 1, source="https://news.test/gone")
    rsps.get("https://news.test/gone", status=404, body=STUB_HTML)
    rsps.get("https://web.archive.org/cdx/search/cdx", json=[["urlkey"]])
    rsps.get("https://index.commoncrawl.org/collinfo.json", json=[])
    link, _ = _resolve(config, conn, 1)
    assert (link["status"], link["expansion_method"]) == ("dead", "live")
    assert link["url_expanded"] == "https://news.test/gone"
    evidence = next(r for r in captures(conn) if r["capture_id"] == link["capture_id"])
    assert evidence["http_status"] == 404  # the dead page is still the link's evidence


# --- steps 2-5: bitly info, wayback, common crawl ------------------------------------------------


def test_bitly_info_page_names_the_destination_when_live_fails(
    env: tuple[Config, sqlite3.Connection], rsps: responses.RequestsMock
) -> None:
    config, conn = env
    add_row(conn, 1, source="herit.ag/abc")
    rsps.get("https://herit.ag/abc", body=requests.ConnectionError("reset"))
    rsps.get("http://herit.ag/abc", body=requests.ConnectionError("reset"))
    rsps.get(
        "https://herit.ag/abc+",
        body='<html><script>{"long_url":"https:\\/\\/news.test\\/story"}</script></html>',
    )
    rsps.get("https://news.test/story", body=LONG_HTML, content_type="text/html")
    link, _ = _resolve(config, conn, 1)
    assert (link["status"], link["expansion_method"]) == ("live", "bitly_info")
    assert link["url_expanded"] == "https://news.test/story"


def test_a_shortener_redirect_captured_only_by_the_archive_leads_to_the_archived_target(
    env: tuple[Config, sqlite3.Connection], rsps: responses.RequestsMock
) -> None:
    config, conn = env
    add_row(conn, 1, year=2012, source="herit.ag/xyz")
    rsps.get("https://herit.ag/xyz", status=404, body=STUB_HTML)  # the live shortener is gone
    rsps.get("https://herit.ag/xyz+", status=404)
    cdx_header = ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"]
    rsps.get(
        "https://web.archive.org/cdx/search/cdx",
        json=[
            cdx_header,
            ["ag,herit)/xyz", "20120305010203", "http://herit.ag/xyz", "-", "302", "D", "1"],
        ],
        match=[
            responses.matchers.query_param_matcher({"filter": "statuscode:3.."}, strict_match=False)
        ],
    )
    rsps.get(
        "https://web.archive.org/web/20120305010203id_/http://herit.ag/xyz",
        status=302,
        headers={
            "Location": "https://web.archive.org/web/20120305010203id_/https://news.test/story",
            "x-archive-orig-location": "https://news.test/story",
        },
    )
    rsps.get("https://news.test/story", status=404, body=STUB_HTML)  # and the target is dead too
    rsps.get(
        "https://web.archive.org/cdx/search/cdx",
        json=[
            cdx_header,
            [
                "test,news)/story",
                "20120401000000",
                "https://news.test/story",
                "text/html",
                "200",
                "E",
                "9",
            ],
        ],
        match=[
            responses.matchers.query_param_matcher({"filter": "statuscode:200"}, strict_match=False)
        ],
    )
    rsps.get(
        "https://web.archive.org/web/20120401000000id_/https://news.test/story",
        body=LONG_HTML,
        content_type="text/html",
    )
    link, _ = _resolve(config, conn, 1)
    assert (link["status"], link["expansion_method"]) == ("archived", "wayback_short")
    assert link["url_expanded"] == "https://news.test/story"
    archived = next(r for r in captures(conn) if r["capture_id"] == link["capture_id"])
    assert archived["tool"] == "wayback" and archived["url_requested"] == "https://news.test/story"


def test_a_target_only_in_the_wayback_cdx_is_archived(
    env: tuple[Config, sqlite3.Connection], rsps: responses.RequestsMock
) -> None:
    config, conn = env
    add_row(conn, 1, year=2014, source="https://news.test/old-story")
    rsps.get("https://news.test/old-story", status=410, body=STUB_HTML)
    header = ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"]
    rsps.get(
        "https://web.archive.org/cdx/search/cdx",
        json=[
            header,
            ["k", "20100101000000", "https://news.test/old-story", "text/html", "200", "A", "1"],
            ["k", "20140601000000", "https://news.test/old-story", "text/html", "200", "B", "1"],
            ["k", "20200101000000", "https://news.test/old-story", "text/html", "200", "C", "1"],
        ],
    )
    rsps.get(
        "https://web.archive.org/web/20140601000000id_/https://news.test/old-story",
        body=LONG_HTML,
        content_type="text/html",
    )
    link, _ = _resolve(config, conn, 1)
    assert (link["status"], link["expansion_method"]) == ("archived", "wayback_target")
    requested = [c.request.url for c in rsps.calls if "/web/" in c.request.url]
    assert requested == [
        "https://web.archive.org/web/20140601000000id_/https://news.test/old-story"
    ]
    # the copy nearest the row's year (2014), not the newest and not the oldest


def test_common_crawl_is_the_second_chance(
    env: tuple[Config, sqlite3.Connection], rsps: responses.RequestsMock
) -> None:
    config, conn = env
    add_row(conn, 1, source="https://news.test/cc-story")
    rsps.get("https://news.test/cc-story", status=404, body=STUB_HTML)
    rsps.get("https://web.archive.org/cdx/search/cdx", json=[["urlkey"]])  # Wayback has nothing
    rsps.get(
        "https://index.commoncrawl.org/collinfo.json",
        json=[{"cdx-api": "https://index.commoncrawl.org/CC-MAIN-2026-1-index"}],
    )
    rsps.get(
        "https://index.commoncrawl.org/CC-MAIN-2026-1-index",
        body=json.dumps(
            {
                "urlkey": "k",
                "timestamp": "20260101000000",
                "url": "https://news.test/cc-story",
                "mime": "text/html",
                "status": "200",
                "digest": "D",
                "length": "100",
                "offset": "10",
                "filename": "crawl-data/x.warc.gz",
            }
        ),
    )
    member = common_crawl_member("https://news.test/cc-story", LONG_HTML)
    rsps.get("https://data.commoncrawl.org/crawl-data/x.warc.gz", body=member)
    link, _ = _resolve(config, conn, 1)
    assert (link["status"], link["expansion_method"]) == ("archived", "commoncrawl")
    page = next(r for r in captures(conn) if r["capture_id"] == link["capture_id"])
    assert page["tool"] == "commoncrawl-payload"
    assert json.loads(page["headers_json"])["parent_capture_id"]  # the raw WARC member is kept too


def test_nothing_anywhere_is_dead_for_a_plain_url_and_unresolved_for_a_shortener(
    env: tuple[Config, sqlite3.Connection], rsps: responses.RequestsMock
) -> None:
    config, conn = env
    add_row(conn, 1, source="https://news.test/lost")
    add_row(conn, 2, source="herit.ag/lost")
    rsps.get("https://news.test/lost", status=404, body=STUB_HTML)
    rsps.get("https://herit.ag/lost", status=404, body=STUB_HTML)
    rsps.get("https://herit.ag/lost+", status=404)
    rsps.get("https://web.archive.org/cdx/search/cdx", json=[["urlkey"]])
    rsps.get("https://index.commoncrawl.org/collinfo.json", json=[])
    plain, _ = _resolve(config, conn, 1)
    short, _ = _resolve(config, conn, 2)
    assert (plain["status"], plain["url_expanded"]) == ("dead", "https://news.test/lost")
    assert (short["status"], short["url_expanded"], short["expansion_method"]) == (
        "unresolved",
        None,
        "unresolved",
    )
    assert "never captured" in short["note"]


def test_a_module_the_config_switches_off_is_skipped_with_its_reason(
    write_config: Callable[..., Path], rsps: responses.RequestsMock
) -> None:
    config = _config(write_config, **{"commoncrawl_enabled = true": "commoncrawl_enabled = false"})
    conn = connect(config)
    add_row(conn, 1, source="https://news.test/x")
    rsps.get("https://news.test/x", status=404, body=STUB_HTML)
    rsps.get("https://web.archive.org/cdx/search/cdx", json=[["urlkey"]])
    ctx, cap, registry = make_ctx(config, conn)
    result = resolve_row(row_of(conn, 1), ctx, registry)
    cap.close()
    modules = result.summary_json["links"][0]["modules"]
    skipped = {m["name"]: m["reason"] for m in modules if m["status"] == "skipped"}
    assert "commoncrawl_enabled is false" in skipped["resolve.commoncrawl"]
    assert not [c for c in rsps.calls if "commoncrawl" in c.request.url]
    conn.close()


# --- rows with no URL, several URLs, idempotence ---------------------------------------------------


def test_a_row_with_no_url_is_recorded_as_such(
    env: tuple[Config, sqlite3.Connection], rsps: responses.RequestsMock
) -> None:
    config, conn = env
    add_row(conn, 1, source="Heritage research notes only")
    add_row(conn, 2, source=None)
    for row_id, cell in ((1, "Heritage research notes only"), (2, "")):
        link, summary = _resolve(config, conn, row_id)
        assert (link["url_raw"], link["status"], link["expansion_method"], link["note"]) == (
            cell,
            "unresolved",
            "unresolved",
            "no_url",
        )
        assert summary["no_source"] is True and link["resolved_at"]
    assert len(rsps.calls) == 0  # nothing to fetch


def test_every_url_in_a_cell_gets_its_own_link(
    env: tuple[Config, sqlite3.Connection], rsps: responses.RequestsMock
) -> None:
    config, conn = env
    add_row(conn, 1, source="https://news.test/a, https://news.test/b. https://news.test/a")
    rsps.get("https://news.test/a", body=LONG_HTML, content_type="text/html")
    rsps.get("https://news.test/b", body=LONG_HTML + "<!-- b -->", content_type="text/html")
    _resolve(config, conn, 1)
    assert [(r["url_raw"], r["status"]) for r in links_of(conn, 1)] == [
        ("https://news.test/a", "live"),
        ("https://news.test/b", "live"),
    ]


def test_a_second_run_fetches_nothing_and_a_reset_run_does_not_refetch_resolved_links(
    env: tuple[Config, sqlite3.Connection], rsps: responses.RequestsMock
) -> None:
    config, conn = env
    add_row(conn, 1, source="https://news.test/story")
    rsps.get("https://news.test/story", body=LONG_HTML, content_type="text/html")
    ctx, cap, registry = make_ctx(config, conn)
    first = resolve_row(row_of(conn, 1), ctx, registry)
    calls = len(rsps.calls)
    before = len(captures(conn))
    again = resolve_row(row_of(conn, 1), ctx, registry)  # same row, links already resolved
    assert len(rsps.calls) == calls and len(captures(conn)) == before
    assert again.summary_json["links"][0]["kept"] is True
    assert again.input_hash == first.input_hash
    # through the runner: done rows are not claimed again, and --reset re-reads without fetching
    options = rn.RunOptions("resolve_links")
    rn.run_stage(conn, config, options, ctx, component=component)  # runs the pending case_stage row
    assert (
        conn.execute("SELECT status FROM case_stage WHERE stage='resolve_links'").fetchone()[0]
        == "done"
    )
    n_calls, n_caps = len(rsps.calls), len(captures(conn))
    again_run = rn.run_stage(conn, config, options, ctx, component=component)
    assert again_run.claimed == 0
    reset_run = rn.run_stage(
        conn, config, rn.RunOptions("resolve_links", reset=True), ctx, component=component
    )
    assert reset_run.done == 1 and len(rsps.calls) == n_calls and len(captures(conn)) == n_caps
    cap.close()


def test_the_input_hash_changes_when_a_module_becomes_available(
    write_config: Callable[..., Path],
) -> None:
    on = _config(write_config)
    off = _config(write_config, **{"commoncrawl_enabled = true": "commoncrawl_enabled = false"})
    conn = connect(on)
    add_row(conn, 1, source="https://news.test/x")
    ctx_on, cap_on, registry = make_ctx(on, conn)
    ctx_off, cap_off, _ = make_ctx(off, conn)
    row = row_of(conn, 1)
    assert component.input_hash(row, ctx_on, registry) != component.input_hash(
        row, ctx_off, registry
    )
    cap_on.close()
    cap_off.close()
    conn.close()


# --- the Bitly info page parser --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        (
            '<script>{"long_url": "https://a.test/x?y=1&amp;z=2"}</script>',
            "https://a.test/x?y=1&z=2",
        ),
        ('<div data-long-url="https://a.test/p"></div>', "https://a.test/p"),
        ('<meta property="og:url" content="https://a.test/og">', "https://a.test/og"),
        ('<meta property="og:url" content="https://bit.ly/abc">', None),  # the page's own address
        ('<a href="https://www.facebook.com/share">share</a>', None),  # furniture, not an answer
        ("<html>no destination here</html>", None),
    ],
)
def test_the_bitly_info_page_is_read_only_when_it_says_where_the_link_goes(
    html: str, expected: str | None
) -> None:
    assert long_url_from_info_page(html) == expected


def test_the_chain_is_the_configured_one(config: Config) -> None:
    from efp import modules as chain

    spec = chain.chains_for(config, "resolve_links")[0]
    assert spec.policy == "first_hit"
    assert spec.modules == (
        "resolve.live",
        "resolve.bitly_info",
        "resolve.wayback_short",
        "resolve.wayback_target",
        "resolve.commoncrawl",
    )


# --- review follow-ups: moved pages, error pages, archived redirects, year windows -------------

CDX_HEADER = ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"]


def _cdx_rows(*rows: list[str]) -> list[list[str]]:
    return [CDX_HEADER, *rows]


def _cdx_urls(rsps: responses.RequestsMock) -> list[str]:
    from urllib.parse import parse_qs, urlsplit

    return [
        parse_qs(urlsplit(c.request.url).query)["url"][0]
        for c in rsps.calls
        if "/cdx/search/cdx" in c.request.url
    ]


def test_a_dead_page_that_redirects_to_the_homepage_does_not_send_the_archive_to_the_homepage(
    env: tuple[Config, sqlite3.Connection], rsps: responses.RequestsMock
) -> None:
    config, conn = env
    add_row(conn, 1, year=2012, source="https://news.test/old/story")
    rsps.get("https://news.test/old/story", status=301, headers={"Location": "https://news.test/"})
    rsps.get("https://news.test/", body=LONG_HTML, content_type="text/html")  # a real homepage
    rsps.get(
        "https://web.archive.org/cdx/search/cdx",
        json=_cdx_rows(
            ["k", "20120601000000", "https://news.test/old/story", "text/html", "200", "A", "1"]
        ),
    )
    rsps.get(
        "https://web.archive.org/web/20120601000000id_/https://news.test/old/story",
        body=LONG_HTML,
        content_type="text/html",
    )
    link, _ = _resolve(config, conn, 1)
    assert (link["status"], link["expansion_method"]) == ("archived", "wayback_target")
    assert link["url_expanded"] == "https://news.test/old/story"  # not the homepage
    assert _cdx_urls(rsps) == ["https://news.test/old/story"]


@pytest.mark.parametrize("landing", ["https://other.test/", "https://bitly.com/404"])
def test_a_shortener_that_lands_on_a_site_root_or_its_own_error_page_is_still_asked_of_the_archive(
    env: tuple[Config, sqlite3.Connection], rsps: responses.RequestsMock, landing: str
) -> None:
    config, conn = env
    add_row(conn, 1, source="herit.ag/dead1")
    rsps.get("https://herit.ag/dead1", status=301, headers={"Location": landing})
    rsps.get(landing, body=LONG_HTML, content_type="text/html")
    rsps.get("https://herit.ag/dead1+", status=404)
    rsps.get("https://web.archive.org/cdx/search/cdx", json=_cdx_rows())
    rsps.get("https://index.commoncrawl.org/collinfo.json", json=[])
    link, summary = _resolve(config, conn, 1)
    ran = {m["name"]: m["status"] for m in summary["links"][0]["modules"]}  # type: ignore[index]
    assert ran["resolve.wayback_short"] != "skipped"  # the destination was not "already known"
    assert link["status"] == "unresolved" and link["url_expanded"] is None


def test_a_long_article_that_mentions_error_phrases_is_still_an_article(
    env: tuple[Config, sqlite3.Connection], rsps: responses.RequestsMock
) -> None:
    config, conn = env
    add_row(conn, 1, source="https://news.test/story")
    body = LONG_HTML.replace(
        "</article>",
        "<p>The account suspended notice and the line index of / appear in the filing. "
        "The link does not exist, the defendant argued. " * 20 + "</p></article>",
    )
    rsps.get("https://news.test/story", body=body, content_type="text/html")
    link, _ = _resolve(config, conn, 1)
    assert link["status"] == "live"


def test_a_short_real_page_is_live_and_an_error_page_with_a_200_is_not(
    env: tuple[Config, sqlite3.Connection], rsps: responses.RequestsMock
) -> None:
    config, conn = env
    add_row(conn, 1, source="https://news.test/short")
    add_row(conn, 2, source="https://news.test/missing")
    short = (
        "<html><head><title>Statement from the county</title></head><body><p>"
        + "The office issued a brief statement on the plea. " * 7
        + "</p></body></html>"
    )
    rsps.get("https://news.test/short", body=short, content_type="text/html")
    rsps.get(
        "https://news.test/missing",
        body="<html><head><title>Page Not Found</title></head><body><p>"
        + "Sorry, the page you requested could not be located here. " * 6
        + "</p></body></html>",
        content_type="text/html",
    )
    rsps.get("https://web.archive.org/cdx/search/cdx", json=_cdx_rows())
    rsps.get("https://index.commoncrawl.org/collinfo.json", json=[])
    assert _resolve(config, conn, 1)[0]["status"] == "live"
    assert _resolve(config, conn, 2)[0]["status"] == "dead"


def test_an_archived_redirect_that_only_upgrades_http_to_https_is_not_an_expansion(
    env: tuple[Config, sqlite3.Connection], rsps: responses.RequestsMock
) -> None:
    config, conn = env
    add_row(conn, 1, year=2012, source="herit.ag/xyz")
    rsps.get("https://herit.ag/xyz", status=404, body=STUB_HTML)
    rsps.get("https://herit.ag/xyz+", status=404)
    rsps.get(
        "https://web.archive.org/cdx/search/cdx",
        json=_cdx_rows(
            ["k", "20110101000000", "http://herit.ag/xyz", "-", "301", "D", "1"],
            ["k", "20120101000000", "http://herit.ag/xyz", "-", "302", "E", "1"],
        ),
        match=[
            responses.matchers.query_param_matcher({"filter": "statuscode:3.."}, strict_match=False)
        ],
    )
    rsps.get(
        "https://web.archive.org/web/20120101000000id_/http://herit.ag/xyz",
        status=301,
        headers={"x-archive-orig-location": "https://herit.ag/xyz"},  # just http -> https
    )
    rsps.get(
        "https://web.archive.org/web/20110101000000id_/http://herit.ag/xyz",
        status=302,
        headers={"x-archive-orig-location": "https://news.test/real"},
    )
    rsps.get("https://news.test/real", status=404, body=STUB_HTML)
    rsps.get(
        "https://web.archive.org/cdx/search/cdx",
        json=_cdx_rows(
            ["k", "20120201000000", "https://news.test/real", "text/html", "200", "F", "1"]
        ),
        match=[
            responses.matchers.query_param_matcher({"filter": "statuscode:200"}, strict_match=False)
        ],
    )
    rsps.get(
        "https://web.archive.org/web/20120201000000id_/https://news.test/real",
        body=LONG_HTML,
        content_type="text/html",
    )
    link, _ = _resolve(config, conn, 1)
    assert link["url_expanded"] == "https://news.test/real" and link["status"] == "archived"


def test_the_archive_is_asked_near_the_rows_year_before_it_is_asked_for_everything(
    env: tuple[Config, sqlite3.Connection], rsps: responses.RequestsMock
) -> None:
    config, conn = env
    add_row(conn, 1, year=2011, source="https://news.test/old")
    rsps.get("https://news.test/old", status=404, body=STUB_HTML)
    rsps.get(
        "https://web.archive.org/cdx/search/cdx",
        json=_cdx_rows(
            ["k", "20110801000000", "https://news.test/old", "text/html", "200", "A", "1"]
        ),
    )
    rsps.get(
        "https://web.archive.org/web/20110801000000id_/https://news.test/old",
        body=LONG_HTML,
        content_type="text/html",
    )
    _resolve(config, conn, 1)
    query = next(c.request.url for c in rsps.calls if "/cdx/search/cdx" in c.request.url)
    assert "from=2009" in query and "to=2014" in query


def test_a_bare_url_falls_back_to_http_when_https_fails(
    env: tuple[Config, sqlite3.Connection], rsps: responses.RequestsMock
) -> None:
    config, conn = env
    add_row(conn, 1, source="www.news.com/story/2012/a.html")
    rsps.get("https://www.news.com/story/2012/a.html", body=requests.ConnectionError("tls"))
    rsps.get("http://www.news.com/story/2012/a.html", body=LONG_HTML, content_type="text/html")
    link, _ = _resolve(config, conn, 1)
    assert (
        link["status"] == "live" and link["url_expanded"] == "http://www.news.com/story/2012/a.html"
    )


def test_a_reset_run_reattempts_a_dead_link_and_finds_what_a_newly_enabled_source_offers(
    env: tuple[Config, sqlite3.Connection], rsps: responses.RequestsMock
) -> None:
    config, conn = env
    add_row(conn, 1, source="https://news.test/later")
    rsps.get("https://news.test/later", status=404, body=STUB_HTML)
    rsps.get("https://web.archive.org/cdx/search/cdx", json=_cdx_rows())
    rsps.get("https://index.commoncrawl.org/collinfo.json", json=[])
    ctx, cap, registry = make_ctx(config, conn)
    assert resolve_row(row_of(conn, 1), ctx, registry).summary_json["links"][0]["status"] == "dead"
    cap.session.cache.clear()  # (the HTTP cache would otherwise serve the earlier empty answer)
    rsps.replace(
        responses.GET,
        "https://web.archive.org/cdx/search/cdx",
        json=_cdx_rows(
            ["k", "20120101000000", "https://news.test/later", "text/html", "200", "A", "1"]
        ),
    )
    rsps.get(
        "https://web.archive.org/web/20120101000000id_/https://news.test/later",
        body=LONG_HTML,
        content_type="text/html",
    )
    again = resolve_row(row_of(conn, 1), ctx, registry)
    cap.close()
    assert again.summary_json["links"][0]["status"] == "archived"
    assert links_of(conn, 1)[0]["status"] == "archived"


def test_every_link_has_a_capture_or_a_logged_reason(
    env: tuple[Config, sqlite3.Connection], rsps: responses.RequestsMock
) -> None:
    config, conn = env
    add_row(conn, 1, source="https://news.test/never")  # the connection fails outright
    add_row(conn, 2, source="https://news.test/gone")
    add_row(conn, 3, source="Heritage notes")
    rsps.get("https://news.test/never", body=requests.ConnectionError("refused"))
    rsps.get("https://news.test/gone", status=404, body=STUB_HTML)
    rsps.get(
        "https://web.archive.org/cdx/search/cdx", body=requests.ConnectionError("archive down")
    )
    rsps.get("https://index.commoncrawl.org/collinfo.json", body=requests.ConnectionError("down"))
    for row_id in (1, 2, 3):
        _resolve(config, conn, row_id)
    bare = conn.execute(
        "SELECT count(*) FROM links WHERE capture_id IS NULL AND coalesce(note, '') = ''"
    ).fetchone()[0]
    assert bare == 0  # an attempt that produced no bytes still says why
    assert "no response" in links_of(conn, 1)[0]["note"]

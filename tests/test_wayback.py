from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator

import pytest
import responses
import stamina

from efp import wayback
from efp.capture import Capture
from efp.config import Config
from tests.capturehelpers import captures, make_capture
from tests.dbhelpers import insert_row


@pytest.fixture(autouse=True)
def _fast_retries() -> Iterator[None]:
    stamina.set_testing(True, attempts=2, cap=False)
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


CDX_ANSWER = [
    ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"],
    [
        "test,example)/page",
        "20120305010203",
        "http://example.test/page",
        "text/html",
        "200",
        "SHA1X",
        "1234",
    ],
    [
        "test,example)/page",
        "20190101000000",
        "http://example.test/page",
        "text/html",
        "200",
        "SHA2Y",
        "999",
    ],
]


def test_cdx_lists_captures_and_records_the_call_like_any_fetch(
    cap: Capture, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    rsps.get("https://web.archive.org/cdx/search/cdx", json=CDX_ANSWER)
    found = wayback.cdx(cap, "http://example.test/page", from_year=2010, to_year=2020, limit=5)
    assert [r.timestamp for r in found] == ["20120305010203", "20190101000000"]
    assert found[0].replay_url == (
        "https://web.archive.org/web/20120305010203id_/http://example.test/page"
    )
    query = rsps.calls[0].request.url
    assert "output=json" in query and "from=2010" in query and "to=2020" in query
    assert "limit=-5" in query and "matchType=exact" in query  # newest first, bounded
    assert captures(conn)[-1]["stage"] == "resolve_links"  # recorded, not an invisible call


def test_cdx_with_no_captures_is_an_empty_list(cap: Capture, rsps: responses.RequestsMock) -> None:
    rsps.get("https://web.archive.org/cdx/search/cdx", json=[])
    assert wayback.cdx(cap, "http://nothing.test/") == []


def test_cdx_rejects_a_bad_match_type(cap: Capture) -> None:
    with pytest.raises(ValueError, match="match"):
        wayback.cdx(cap, "http://example.test/", match="regex")


def test_availability(cap: Capture, rsps: responses.RequestsMock) -> None:
    rsps.get(
        "https://archive.org/wayback/available",
        json={
            "archived_snapshots": {
                "closest": {
                    "available": True,
                    "url": "http://web.archive.org/web/2012/http://example.test/",
                    "timestamp": "2012",
                    "status": "200",
                }
            }
        },
    )
    got = wayback.availability(cap, "http://example.test/", timestamp="2012")
    assert got.available and got.timestamp == "2012"
    rsps.replace(
        responses.GET, "https://archive.org/wayback/available", json={"archived_snapshots": {}}
    )
    assert not wayback.availability(cap, "http://other.test/").available


def test_fetch_archived_records_the_original_url_and_the_wayback_tool(
    cap: Capture, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    rsps.get(
        "https://web.archive.org/web/20120305010203id_/http://example.test/page",
        body=b"<html>archived</html>",
    )
    row = wayback.CdxRow(
        "wayback", "k", "20120305010203", "http://example.test/page", "text/html", "200", "d", "1"
    )
    record = wayback.fetch_archived(cap, row, row_id=1, stage="resolve_links")
    assert record.tool == "wayback" and record.url_requested == "http://example.test/page"
    assert record.url_final.endswith("id_/http://example.test/page")
    assert cap.read_body(record) == b"<html>archived</html>"
    assert captures(conn)[-1]["tool"] == "wayback"


def test_common_crawl_has_the_same_interface(cap: Capture, rsps: responses.RequestsMock) -> None:
    rsps.get(
        wayback.COMMONCRAWL_INDEXES,
        json=[
            {
                "id": "CC-MAIN-2026-1",
                "cdx-api": "https://index.commoncrawl.org/CC-MAIN-2026-1-index",
            }
        ],
    )
    line = json.dumps(
        {
            "urlkey": "k",
            "timestamp": "20260101000000",
            "url": "http://example.test/page",
            "mime": "text/html",
            "status": "200",
            "digest": "D",
            "length": "100",
            "offset": "5000",
            "filename": "crawl-data/CC-MAIN-2026-1/segments/x.warc.gz",
        }
    )
    rsps.get("https://index.commoncrawl.org/CC-MAIN-2026-1-index", body=line + "\n")
    rsps.get(
        "https://data.commoncrawl.org/crawl-data/CC-MAIN-2026-1/segments/x.warc.gz", body=b"RANGE"
    )
    found = wayback.cdx(cap, "http://example.test/page", source="commoncrawl", limit=3)
    assert len(found) == 1 and found[0].source == "commoncrawl" and found[0].offset == 5000
    record = wayback.fetch_archived(cap, found[0], row_id=1, stage="resolve_links")
    assert record.tool == "commoncrawl" and record.url_requested == "http://example.test/page"
    assert rsps.calls[-1].request.headers["Range"] == "bytes=5000-5099"


def test_common_crawl_no_captures_is_not_an_error(
    cap: Capture, rsps: responses.RequestsMock
) -> None:
    rsps.get(
        wayback.COMMONCRAWL_INDEXES, json=[{"cdx-api": "https://index.commoncrawl.org/CC-1-index"}]
    )
    rsps.get(
        "https://index.commoncrawl.org/CC-1-index",
        status=404,
        body=b'{"message":"No Captures found"}',
    )
    assert wayback.cdx(cap, "http://nothing.test/", source="commoncrawl") == []


def test_the_list_of_common_crawl_indexes_is_fetched_once_per_client(
    cap: Capture, rsps: responses.RequestsMock
) -> None:
    rsps.get(
        wayback.COMMONCRAWL_INDEXES, json=[{"cdx-api": "https://index.commoncrawl.org/CC-1-index"}]
    )
    rsps.get("https://index.commoncrawl.org/CC-1-index", status=404, body=b"{}")
    wayback.cdx(cap, "http://a.test/1", source="commoncrawl")
    wayback.cdx(cap, "http://a.test/2", source="commoncrawl")
    listings = [c for c in rsps.calls if c.request.url == wayback.COMMONCRAWL_INDEXES]
    assert len(listings) == 1

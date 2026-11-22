from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import responses
import stamina

from efp.config import Config
from efp.court import doj
from efp.court.names import parse_name
from tests.courthelpers import (
    ARTICLE,
    add_court_row,
    court_config,
    doj_page,
    process,
    release,
    release_html,
)

API = doj.API
PR_URL = "https://www.justice.gov/usao-ct/pr/example-release"
TARGET_BODY = (
    "Alex Example, 47, of Hartford, Connecticut, pleaded guilty to voting twice in the 2012 "
    "general election. Case No. 3:13-cr-00045 is pending sentencing. " + ARTICLE
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


@pytest.fixture
def config(write_config: Callable[..., Path]) -> Config:
    return court_config(write_config)


def _site(rsps: responses.RequestsMock, page_html: str | None = None) -> None:
    rsps.get("https://www.justice.gov/robots.txt", status=404)
    rsps.get(PR_URL, body=page_html or release_html(TARGET_BODY), content_type="text/html")


def _modules(summary: dict[str, object]) -> dict[str, str]:
    return {m["name"]: m["status"] for m in summary["modules"]}  # type: ignore[index]


# --- parsing and matching --------------------------------------------------------------------


def test_dates_are_read_from_a_unix_timestamp_or_an_iso_string() -> None:
    assert doj.parse_date("1394000000").year == 2014  # type: ignore[union-attr]
    assert doj.parse_date("2014-03-05T10:00:00Z").year == 2014  # type: ignore[union-attr]
    assert doj.parse_date("") is None and doj.parse_date("not a date") is None


def test_a_server_that_ignored_the_title_is_detected() -> None:
    page = doj.parse_releases(
        doj_page(release("Something about budgets", "x"), release("Another topic", "y", uuid="u2")),
        1,
    )
    assert not doj.honors_title(page, "Example")
    named = doj.parse_releases(doj_page(release("Alex Example pleads guilty", "x")), 1)
    assert doj.honors_title(named, "Example") and doj.honors_title([], "Example")


def _match(title: str, body: str, *, date: str = "2014-03-05", state: str = "CT", slack: int = 1):  # type: ignore[no-untyped-def]
    rel = doj.parse_releases(doj_page(release(title, body, date=date)), 1)[0]
    return doj.match_release(
        parse_name("Alex Example"), rel, row_year=2014, row_state=state, slack=slack
    )


def test_a_release_must_name_the_subject_in_full_and_mention_an_election_charge() -> None:
    ok = _match("Alex Example pleads guilty to voter fraud", "Connecticut man voted twice.")
    assert ok is not None and ok.state_corroborated
    assert _match("Example pleads guilty to voter fraud", "He voted twice.") is None  # surname only
    assert (
        _match("Alex Example pleads guilty to tax fraud", "He filed false returns.") is None
    )  # no election term
    assert (
        _match("Alex Example pleads guilty to voter fraud", "x", date="2010-01-01") is None
    )  # outside the years


def test_an_uncorroborated_state_is_recorded_not_assumed() -> None:
    match = _match(
        "Alex Example pleads guilty to voter fraud", "He voted twice in Ohio.", state="CT"
    )
    assert match is not None and not match.state_corroborated


# --- the strategy end to end -------------------------------------------------------------------


def test_a_release_with_a_case_number_is_a_located_record_and_stops_the_chain(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    add_court_row(conn, 1, details="Voted twice in 2012")
    _site(rsps)
    rsps.get(API, json=doj_page(release("Alex Example pleads guilty to voter fraud", TARGET_BODY)))
    result = process(config, conn, 1)
    summary = result.summary_json
    assert summary["court_status"] == "press_release_only"
    assert _modules(summary)["court.doj"] == "hit"
    assert "court.courtlistener_bulk" not in _modules(summary)  # the chain stopped at DOJ
    record = conn.execute(
        "SELECT source_kind, jurisdiction, case_number, document_kind, strategy FROM court_records"
    ).fetchone()
    assert record == ("doj_release", "US", "3:13-cr-00045", "press_release", "court.doj")
    kinds = {r[0] for r in conn.execute("SELECT kind FROM identifiers")}
    assert {"case_number", "age", "city"} <= kinds
    span = conn.execute("SELECT span FROM identifiers WHERE kind = 'age'").fetchone()[0]
    text_capture = conn.execute(
        "SELECT body_path FROM captures WHERE tool = 'trafilatura' LIMIT 1"
    ).fetchone()
    assert text_capture is not None and span.startswith(",")


def test_a_release_without_a_case_number_is_recorded_but_the_chain_goes_on(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    add_court_row(conn, 1)
    body = (
        "Alex Example, 47, of Hartford, Connecticut, admitted voting twice in the election. "
        + ARTICLE
    )
    _site(rsps, release_html(body))
    rsps.get(API, json=doj_page(release("Alex Example admits voter fraud", body)))
    summary = process(config, conn, 1).summary_json
    assert _modules(summary)["court.doj"] == "miss"  # located, but no case number
    assert _modules(summary)["court.courtlistener_bulk"] in ("skipped", "miss")  # the chain went on
    assert summary["court_status"] == "press_release_only"
    assert conn.execute("SELECT case_number FROM court_records").fetchone() == (None,)


def test_no_matching_release_completes_as_not_found_never_failed(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    add_court_row(conn, 1)
    rsps.get(API, json=doj_page(release("Alex Example sentenced for tax evasion", "tax return")))
    result = process(config, conn, 1)
    assert result.status == "done" and result.summary_json["court_status"] == "not_found"
    assert conn.execute("SELECT count(*) FROM court_records").fetchone()[0] == 0


def test_the_title_keyword_query_is_sent_and_pages_are_captured_and_filtered_locally(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    add_court_row(conn, 1)
    _site(rsps)
    rsps.get(
        API,
        json=doj_page(
            release("Alex Example pleads guilty to voter fraud", TARGET_BODY),
            release(
                "Alex Example pleads to a 2005 voter charge",
                TARGET_BODY,
                date="2005-01-01",
                uuid="u2",
            ),
        ),
    )
    process(config, conn, 1)
    query = next(c.request.url for c in rsps.calls if "press_releases" in c.request.url)
    assert "parameters%5Btitle%5D=Example" in query and "pagesize=50" in query
    assert (
        conn.execute(
            "SELECT count(*) FROM captures WHERE url_requested LIKE '%press_releases%'"
        ).fetchone()[0]
        >= 1
    )
    assert (
        conn.execute("SELECT count(*) FROM court_records").fetchone()[0] == 1
    )  # the 2005 one is filtered out


def test_when_the_server_ignores_the_keyword_the_feed_is_harvested_and_searched_locally(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    add_court_row(conn, 1)
    _site(rsps)
    page0 = doj_page(
        release("Budget announcement", "money", date="2016-01-01", uuid="a"),
        release("Another unrelated item", "money", date="2015-06-01", uuid="b"),
    )
    page1 = doj_page(
        release(
            "Alex Example pleads guilty to voter fraud", TARGET_BODY, date="2014-03-05", uuid="c"
        ),
        release("Something else", "x", date="2014-02-01", uuid="d"),
    )
    page2 = doj_page(
        release("Old news", "x", date="2012-06-01", uuid="e")
    )  # older than the window: stop
    for number, payload in enumerate((page0, page1, page2)):
        rsps.get(
            API,
            json=payload,
            match=[
                responses.matchers.query_param_matcher({"page": str(number)}, strict_match=False)
            ],
        )
    result = process(config, conn, 1)
    assert result.summary_json["flags"]["doj"]["found_by"] == "harvest"  # type: ignore[index]
    assert result.summary_json["court_status"] == "press_release_only"
    pages = sorted(
        {
            c.request.url.split("page=")[1].split("&")[0]
            for c in rsps.calls
            if "press_releases" in c.request.url
        }
    )
    assert pages == ["0", "1", "2"]  # paged newest to oldest and stopped once past the window


def test_the_harvest_resumes_and_is_shared_between_rows(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    add_court_row(conn, 1)
    add_court_row(conn, 2, name="Pat Example", sha="2" * 64)
    _site(rsps)
    rsps.get(API, json=doj_page(release("Budget announcement", "x", date="2012-06-01", uuid="a")))
    process(config, conn, 1)
    first = len([c for c in rsps.calls if "press_releases" in c.request.url])
    process(config, conn, 2)
    second = len([c for c in rsps.calls if "press_releases" in c.request.url])
    # the second row costs at most its own title probe: the harvest is not repeated
    assert second - first <= 1


def test_pre_2013_rows_do_not_use_the_press_release_api(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    add_court_row(conn, 1, year=2008)
    summary = process(config, conn, 1).summary_json
    assert "coverage starts" in _reason(summary, "court.doj")
    assert not [c for c in rsps.calls if "press_releases" in c.request.url]


def _reason(summary: dict[str, object], name: str) -> str:
    return next(m["reason"] for m in summary["modules"] if m["name"] == name)  # type: ignore[index, union-attr]


def test_an_api_failure_is_a_note_and_the_row_still_completes(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    add_court_row(conn, 1)
    rsps.get(API, status=503)
    result = process(config, conn, 1)
    assert result.status == "done" and result.summary_json["court_status"] == "not_found"
    assert any("DOJ API" in n for n in result.summary_json["notes"])  # type: ignore[union-attr]


def test_the_captured_page_is_kept_and_a_robots_block_falls_back_to_the_api_body(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    add_court_row(conn, 1)
    rsps.get("https://www.justice.gov/robots.txt", body="User-agent: *\nDisallow: /usao-ct\n")
    rsps.get(API, json=doj_page(release("Alex Example pleads guilty to voter fraud", TARGET_BODY)))
    result = process(config, conn, 1)
    assert result.summary_json["court_status"] == "press_release_only"
    basis = json.loads(
        json.dumps(result.summary_json["located"])  # type: ignore[arg-type]
    )
    assert basis and not [c for c in rsps.calls if c.request.url == PR_URL]
    tool = conn.execute("SELECT tool FROM captures WHERE tool IN ('doj_release_text')").fetchone()
    assert tool is not None  # the body the API returned was kept as the text capture

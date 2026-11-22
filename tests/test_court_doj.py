from __future__ import annotations

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
    make_ctx,
    process,
    release,
    release_html,
    row_of,
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
    assert doj.honors_title(named, "Example")
    assert not doj.honors_title([], "Example")  # an empty page is no evidence either way


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
    rsps.get(
        API,
        json=doj_page(
            release("Alex Example sentenced for tax evasion", "tax return"),
            release("Old news", "x", date="2005-01-01", uuid="old"),  # the feed reaches the window
        ),
    )
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


def test_an_api_failure_is_a_deferred_error_not_a_negative_and_is_retried(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    add_court_row(conn, 1)
    rsps.get(API, status=503)
    result = process(config, conn, 1)
    assert result.status == "done" and result.summary_json["court_status"] == "deferred_error"
    assert any("DOJ API" in n for n in result.summary_json["notes"])  # type: ignore[union-attr]
    assert result.input_hash and result.input_hash.endswith(":error")


def test_one_keyword_missing_from_the_titles_does_not_disable_the_title_search(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    rsps.get(API, json=doj_page(release("Budget announcement", "x", uuid="a")))
    ctx, cap, _ = make_ctx(config, conn)
    try:
        states = []
        for keyword in ("Alpha", "Beta", "Gamma"):
            client = doj.DojClient(ctx)
            try:
                client.search_title(keyword, None, 2013)
                states.append(client.title_search_works)
            finally:
                client.close()
        # undecided after one and two misses, off only after doj.TITLE_PROBES of them
        assert doj.TITLE_PROBES == 3 and states == [None, None, False]
        again = doj.DojClient(ctx)
        assert again.title_search_works is False  # remembered for the run
        again.close()
    finally:
        cap.close()


def test_a_server_that_honors_the_title_is_remembered_at_once(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    rsps.get(API, json=doj_page(release("Alex Example pleads guilty", "x", uuid="a")))
    ctx, cap, _ = make_ctx(config, conn)
    try:
        client = doj.DojClient(ctx)
        client.search_title("Example", None, 2013)
        assert client.title_search_works is True and doj.DojClient(ctx).title_search_works is True
        client.close()
    finally:
        cap.close()


def test_the_doj_index_connection_is_closed_after_each_row(
    config: Config,
    conn: sqlite3.Connection,
    rsps: responses.RequestsMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[doj.DojIndex] = []
    original = doj.DojIndex.__init__

    def tracking(self: doj.DojIndex, path: Path) -> None:
        original(self, path)
        opened.append(self)

    monkeypatch.setattr(doj.DojIndex, "__init__", tracking)
    add_court_row(conn, 1)
    _site(rsps)
    rsps.get(API, json=doj_page())
    process(config, conn, 1)
    assert opened
    for index in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            index.count()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("CT", "CT"),
        (" ct ", "CT"),
        ("Connecticut", "CT"),
        ("connecticut", "CT"),
        ("D.C.", "DC"),
        ("District of Columbia", "DC"),
        ("", None),
        (None, None),
        ("N/A", None),
        ("Unknown", None),
        ("Multiple", None),
        ("US", None),
        ("XX", None),
        (7, None),
    ],
)
def test_a_state_value_is_used_only_when_it_names_a_state(
    raw: object, expected: str | None
) -> None:
    assert doj.normalize_state(raw) == expected


def test_a_junk_state_does_not_credit_a_release_with_the_state() -> None:
    assert doj.mentions_state("Hartford, CT woman charged", "Connecticut")
    assert not doj.mentions_state("Hartford, CT woman charged", "N/A")


def test_a_release_whose_state_is_not_corroborated_is_recorded_but_does_not_stop_the_chain(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    add_court_row(conn, 1, state="CT")
    body = (
        "Alex Example, 47, of Dayton, Ohio, pleaded guilty to voting twice. "
        "Case No. 3:13-cr-00045 is pending. " + ARTICLE
    )  # an Ohio release for a Connecticut row: a different person, perhaps
    _site(rsps, release_html(body))
    rsps.get(API, json=doj_page(release("Alex Example pleads guilty to voter fraud", body)))
    summary = process(config, conn, 1).summary_json
    assert _modules(summary)["court.doj"] == "miss"  # a weak match never ends the search
    assert "court.courtlistener_bulk" in _modules(summary)  # so the later strategies still ran
    located = summary["located"][0]  # type: ignore[index]
    assert located["weak"] is True and summary["court_status"] == "candidate_only"
    weak_row = conn.execute("SELECT weak, basis_json FROM court_records").fetchone()
    assert weak_row[0] == 1 and "state_corroborated" in weak_row[1]  # persisted for readers


def test_title_misses_are_consecutive_and_a_proven_server_is_never_switched_off(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    def answer(request):  # type: ignore[no-untyped-def]
        import json as _json
        from urllib.parse import parse_qs, urlsplit

        keyword = parse_qs(urlsplit(request.url).query).get(doj.TITLE_PARAM, [""])[0]
        title = f"{keyword} pleads guilty" if keyword.startswith("Hit") else "Budget announcement"
        return (200, {}, _json.dumps(doj_page(release(title, "x", uuid=keyword))))

    rsps.add_callback(responses.GET, API, callback=answer, content_type="application/json")
    ctx, cap, _ = make_ctx(config, conn)
    try:
        for keyword in ("Miss1", "Miss2", "Hit1", "Miss3", "Miss4", "Miss5", "Miss6"):
            client = doj.DojClient(ctx)
            try:
                client.search_title(keyword, None, 2013)
                if keyword == "Hit1":
                    assert client.title_search_works is True
            finally:
                client.close()
        # two misses, a success (which resets the count), then four more misses: still on,
        # because a server that has honored the parameter is not switched off by quiet names
        assert doj.DojClient(ctx).title_search_works is True
    finally:
        cap.close()


def test_a_capped_harvest_that_does_not_reach_the_window_is_a_deferred_coverage_not_not_found(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    capped = config.model_copy(
        update={"court": config.court.model_copy(update={"doj_harvest_max_pages": 2})}
    )
    add_court_row(conn, 1)  # a 2014 row; the feed below only ever shows 2020 releases
    _site(rsps)
    rsps.get(API, json=doj_page(release("Budget announcement", "x", date="2020-01-01", uuid="a")))
    result = process(capped, conn, 1)
    summary = result.summary_json
    assert summary["court_status"] == "deferred_coverage"  # not a clean not_found
    assert summary["flags"]["doj_coverage_gap"] is True  # type: ignore[index]
    assert any("harvest reaches back only to 2020" in n for n in summary["notes"])  # type: ignore[union-attr]
    assert result.input_hash and result.input_hash.endswith(":coverage")


def test_a_title_search_that_fills_its_pages_before_the_window_falls_back_to_the_harvest(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    small = config.model_copy(
        update={
            "court": config.court.model_copy(update={"doj_pagesize": 2, "doj_harvest_max_pages": 2})
        }
    )
    add_court_row(conn, 1)  # a 2014 row; every release the feed shows is from 2020
    _site(rsps)
    newer = doj_page(
        release("Example sentenced", "x", date="2020-01-01", uuid="a"),
        release("Example pleads", "x", date="2020-02-01", uuid="b"),
    )
    rsps.get(API, json=newer)
    result = process(small, conn, 1)
    summary = result.summary_json
    assert summary["flags"]["doj"]["found_by"] == "title_search+harvest"  # type: ignore[index]
    assert summary["court_status"] == "deferred_coverage"  # not a proven absence
    assert result.input_hash and result.input_hash.endswith(":coverage")


def test_the_harvest_reads_only_releases_that_mention_the_surname(
    config: Config, conn: sqlite3.Connection
) -> None:
    ctx, cap, _ = make_ctx(config, conn)
    try:
        client = doj.DojClient(ctx)
        client.index.add(
            [
                doj.parse_releases(
                    doj_page(
                        release("Alex Example pleads", "voter fraud", uuid="a", date="2014-01-01"),
                        release("Budget news", "nothing", uuid="b", date="2014-02-01"),
                        release("Other", "Mr. EXAMPLE was named", uuid="c", date="2014-03-01"),
                    ),
                    None,
                )[i]
                for i in range(3)
            ]
        )
        names = {r.uuid for r in client.index.between(2013, 2015, ["example"])}
        assert names == {"a", "c"}  # the unrelated release is never built, so never parsed
        assert len(client.index.between(2013, 2015)) == 3
        client.close()
    finally:
        cap.close()


def test_an_empty_title_result_is_not_taken_as_proof_the_server_honors_the_title(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    add_court_row(conn, 1)
    _site(rsps)

    # the title keyword returns nothing, but the same name sits in a release body on the feed
    def feed(request):  # type: ignore[no-untyped-def]
        import json as _json

        if doj.TITLE_PARAM in request.url or "title" in request.url:
            return (200, {}, _json.dumps(doj_page()))
        page = doj_page(
            release("Tax news", TARGET_BODY, date="2014-03-05", uuid="body-only"),
            release("Old news", "x", date="2010-01-01", uuid="old"),
        )
        return (200, {}, _json.dumps(page))

    rsps.add_callback(responses.GET, API, callback=feed, content_type="application/json")
    result = process(config, conn, 1)
    summary = result.summary_json
    assert summary["flags"]["doj"]["found_by"] == "harvest"  # type: ignore[index]
    assert summary["court_status"] == "press_release_only"  # found in the body via the harvest


def test_reaching_another_rows_window_is_not_coverage_of_an_earlier_year(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    small = config.model_copy(
        update={"court": config.court.model_copy(update={"doj_harvest_max_pages": 1})}
    )
    ctx, cap, _ = make_ctx(small, conn)
    rsps.get(API, json=doj_page(release("Old", "x", date="2017-01-01", uuid="a")))
    try:
        client = doj.DojClient(ctx)
        client.harvest(2018, None)  # a later row's window is reached on the last allowed page
        assert client.covered(2018) and not client.covered(2010)  # an earlier row is not covered
        client.close()
    finally:
        cap.close()


def test_a_same_surname_title_hit_is_not_authoritative_when_the_subject_is_named_in_a_body(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    add_court_row(conn, 1)
    _site(rsps)

    def feed(request):  # type: ignore[no-untyped-def]
        import json as _json

        if "title" in request.url:  # the title search finds only someone else with the surname
            page = doj_page(release("Pat Example sentenced for tax evasion", "tax", uuid="other"))
        else:
            page = doj_page(
                release("Voter fraud case", TARGET_BODY, uuid="subject"),
                release("Old news", "x", date="2005-01-01", uuid="old"),
            )
        return (200, {}, _json.dumps(page))

    rsps.add_callback(responses.GET, API, callback=feed, content_type="application/json")
    summary = process(config, conn, 1).summary_json
    assert summary["flags"]["doj"]["found_by"] == "title_search+harvest"  # type: ignore[index]
    assert summary["court_status"] == "press_release_only"  # the body-only release was found


def test_the_fresh_end_of_the_feed_is_refreshed_once_per_run_and_changes_the_inputs(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    from efp import court as component

    old = doj_page(
        release("Older news", "x", date="2014-01-10", uuid="a"),
        release("Oldest news", "x", date="2013-12-01", uuid="b"),
    )
    new = doj_page(
        release("Brand new release", "x", date="2014-06-01", uuid="c"),
        release("Older news", "x", date="2014-01-10", uuid="a"),  # back at what the index holds
    )
    ctx, cap, registry = make_ctx(config, conn)
    try:
        rsps.get(API, json=old)
        first = doj.DojClient(ctx)
        first.harvest(2013, None)  # the initial walk
        first.close()
        before = doj.newest_indexed(config)
        add_court_row(conn, 1)
        hash_before = component.input_hash(row_of(conn, 1), ctx, registry)
        assert before is not None and before.startswith("2014-01-10")
        rsps.replace(responses.GET, API, json=new)  # a release was published since
        cap.clear_throttles()  # a new run
        again = doj.DojClient(ctx)
        assert again.refresh_newest(None) >= 1
        assert again.refresh_newest(None) == 0  # once per run
        assert {r.uuid for r in again.index.between(2014, 2014)} >= {"a", "c"}
        again.close()
        after = doj.newest_indexed(config)
        assert after is not None and after.startswith("2014-06-01")
        assert component.input_hash(row_of(conn, 1), ctx, registry) != hash_before  # recheck
        # only rows the DOJ module can apply to, and whose window the feed has not passed, depend
        # on the fresh end: a pre-2013 row, a state row and a row from long before are unchanged
        from tests.courthelpers import add_court_row as add

        add(conn, 2, year=2000)
        add(conn, 3, details="Charged in Superior Court by the state", sha="3" * 64)
        add(conn, 4, year=2010, sha="4" * 64)  # window ended 2011: the feed is far past it
        unchanged = {n: component.input_hash(row_of(conn, n), ctx, registry) for n in (2, 3, 4)}
        rsps.replace(
            responses.GET,
            API,
            json=doj_page(release("Later news", "x", date="2014-09-01", uuid="d")),
        )
        cap.clear_throttles()
        later = doj.DojClient(ctx)
        later.refresh_newest(None)
        later.close()
        assert {n: component.input_hash(row_of(conn, n), ctx, registry) for n in (2, 3, 4)} == (
            unchanged
        )
        assert component.input_hash(row_of(conn, 1), ctx, registry) != hash_before
    finally:
        cap.close()

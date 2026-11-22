"""Build 08: the state AG and DA press-release harvester (framework, states, module, matrix)."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterator
from datetime import date
from pathlib import Path

import pytest
import responses
import stamina

import efp.court as component
from efp.capture import CaptureError
from efp.config import Config
from efp.court.adapters import ag
from efp.court.adapters.ag import Release, adapters, sync
from efp.court.adapters.ag import ca as ca_mod
from efp.court.adapters.ag import il as il_mod
from efp.court.adapters.ag import oh as oh_mod
from efp.court.adapters.ag import tx as tx_mod
from efp.court.adapters.ag.base import SiteBlocked, parse_date
from efp.court.adapters.ag.module import CourtStateAg, match_release_text
from efp.court.located import Located
from efp.court.names import parse_name
from efp.resolve_links import text as page_text
from tests.courthelpers import add_court_row, court_config, make_ctx, process, row_of

CASSETTES = Path(__file__).parent / "cassettes" / "ag"
PARSERS: dict[str, tuple[Callable[[str, str], list[Release]], str]] = {
    "tx": (tx_mod.parse_listing, "https://www.texasattorneygeneral.gov"),
    "ca": (ca_mod.parse_listing, "https://oag.ca.gov"),
    "oh": (oh_mod.parse_listing, "https://www.ohioattorneygeneral.gov"),
    "il": (il_mod.parse_listing, "https://illinoisattorneygeneral.gov"),
}
TX = "https://www.texasattorneygeneral.gov"


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


def tx_page(*items: tuple[str, str, str]) -> str:
    """A Texas-shaped listing page: (slug, title, 'Month DD, YYYY')."""
    blocks = "".join(
        f'<div class="m-b-3">\n<h4 class="m-b-1 h4-sans"><a href="/news/releases/{slug}">{title}</a>'
        f'</h4>\n<p class="m-b-0"> {title} summary. </p>\n<p class="meta m-b-0">{day}  | Press '
        "Release\n</p>\n</div>\n"
        for slug, title, day in items
    )
    pager = '<nav class="pager"><a href="?page=9" rel="next">next</a></nav>'
    return f"<html><body>{blocks}{pager}</body></html>"


def serve_tx(rsps: responses.RequestsMock, pages: list[str]) -> None:
    """Serve the pages; the last one has a pager with no next link (the end of the archive)."""
    pages = [*pages[:-1], pages[-1].replace(' rel="next"', "")]
    for number, body in enumerate(pages):
        rsps.get(
            f"{TX}/news/releases",
            body=body,
            match=[responses.matchers.query_param_matcher({"page": str(number)})],
        )


# --- the contract: each module against its recorded cassette ---------------------------------------


@pytest.mark.parametrize("state", sorted(PARSERS))
def test_each_state_cassette_parses_to_dated_releases_and_the_release_text_extracts(
    state: str,
) -> None:
    parse, portal = PARSERS[state]
    folder = CASSETTES / state
    found = parse((folder / "index.html").read_text(encoding="utf-8"), portal)
    assert len(found) >= 3  # the trimmed listing keeps a handful of items
    for release in found:
        assert release.url.startswith(portal + "/") and release.title and release.office == "AG"
        assert release.date is not None and release.date.year >= 2024
    meta = json.loads((folder / "meta.json").read_text())
    assert meta["release_url"] in {r.url for r in found}  # the recorded release is one of them
    text, _ = page_text.extract((folder / "release.html").read_bytes(), meta["release_url"])
    assert text and len(text) > 200  # the release page's main text comes out


def test_dates_in_the_forms_the_listings_use() -> None:
    assert parse_date("October 09, 2026") == date(2026, 10, 9)
    assert parse_date("11/20/2026") == date(2026, 11, 20)
    assert parse_date("2026-11-20 1:45 PM") == date(2026, 11, 20)
    assert parse_date("sometime") is None and parse_date("") is None


# --- the registry and the protocol -----------------------------------------------------------------


def test_the_ten_first_pass_states_are_registered_with_a_mode_and_every_manual_has_a_reason() -> (
    None
):
    found = adapters()
    assert set(found) == {"MN", "TX", "FL", "NC", "CA", "WI", "OH", "KY", "IN", "IL"}
    automated = {s for s, c in found.items() if c.mode == "automated"}
    assert automated == {"TX", "CA", "OH", "IL"}
    for state, cls in found.items():
        assert cls.state == state and cls.portal.startswith("https://")
        if cls.mode == "manual":
            assert cls.reason and cls.search_mode == "none"
        else:
            assert cls.search_mode in {"search_form", "archive_listing", "rss", "sitemap"}


def test_the_release_dataclass_and_protocol_are_the_specs() -> None:
    names = [f for f in Release.__dataclass_fields__]
    assert names == ["url", "title", "date", "office", "snippet"]
    release = Release("https://x/y", "t", None, "AG", "")
    with pytest.raises(AttributeError):
        release.title = "changed"  # type: ignore[misc]  # frozen
    assert {"search", "fetch"} <= set(dir(adapters()["TX"]))
    assert "state" in ag.AGScraper.__annotations__


# --- the framework: harvest, cache, freshness, reach, fallbacks ------------------------------------


def test_the_harvest_pages_newest_first_resumes_and_a_state_index_is_fetched_once(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    serve_tx(
        rsps,
        [
            tx_page(("a", "Election news 2026", "October 09, 2026")),
            tx_page(("b", "Voter fraud 2014", "March 05, 2014")),
            tx_page(("c", "Old release", "June 01, 2009")),
        ],
    )
    ctx, cap, _ = make_ctx(config, conn)
    try:
        tx = adapters()["TX"](ctx)
        assert tx.harvest(2013, None) == 3  # newest first, until older than 2013
        assert tx.index.count() == 3 and tx.covered(2013)
        assert "reaches back only to 2009" in (tx.out_of_reach(2005) or "")  # the archive ended
        assert tx.harvest(2013, None) == 0  # nothing more is needed
        first = tx.search("Alex Example", 2014, window=1)  # the run's one freshness check
        fetched = len([c for c in rsps.calls if "/news/releases" in c.request.url])
        second = tx.search("Alex Example", 2014, window=1)
        assert (
            [r.url.rsplit("/", 1)[-1] for r in first]
            == ["b"]
            == [r.url.rsplit("/", 1)[-1] for r in second]
        )
        assert len([c for c in rsps.calls if "/news/releases" in c.request.url]) == fetched
        tx.close()
    finally:
        cap.close()


def test_a_page_budget_inside_a_row_stops_and_the_rest_resumes_where_it_left_off(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    serve_tx(
        rsps,
        [tx_page((f"r{n}", f"Release {n}", f"January 0{n + 1}, 20{25 - n}")) for n in range(6)],
        # the sixth page has a pager with no next link: the archive ends there
    )
    ctx, cap, _ = make_ctx(config, conn)
    try:
        tx = adapters()["TX"](ctx)
        assert tx.harvest(2010, None) == config.court.ag_harvest_pages_per_row == 3
        assert not tx.covered(2010)
        assert tx.harvest(2010, None, max_pages=10) == 3  # pages 3-5, not page 0 again
        assert tx.index.count() == 6
        tx.close()
    finally:
        cap.close()


def test_releases_published_since_the_harvest_began_are_picked_up_once_per_run(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    serve_tx(
        rsps, [tx_page(("a", "Old news", "May 01, 2026")), tx_page(("z", "Oldest", "May 01, 2010"))]
    )
    ctx, cap, _ = make_ctx(config, conn)
    try:
        tx = adapters()["TX"](ctx)
        tx.harvest(2011, None)
        rsps.replace(
            responses.GET,
            f"{TX}/news/releases",
            body=tx_page(("new", "Brand new", "June 01, 2026"), ("a", "Old news", "May 01, 2026")),
            match=[responses.matchers.query_param_matcher({"page": "0"})],
        )
        cap.clear_throttles()  # a new run
        assert tx.refresh_newest() >= 1 and tx.refresh_newest() == 0  # once per run
        assert tx.index.has(f"{TX}/news/releases/new") and tx.index.newest() == "2026-06-01"
        tx.close()
    finally:
        cap.close()


def test_an_archive_that_runs_out_before_the_window_is_out_of_reach_not_a_negative(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    folder = CASSETTES / "il"
    rsps.get(
        "https://illinoisattorneygeneral.gov/news-room/",
        body=(folder / "index.html").read_text(encoding="utf-8"),
    )
    ctx, cap, _ = make_ctx(config, conn)
    try:
        il = adapters()["IL"](ctx)
        il.harvest(2000, None)  # one page holds the whole archive
        assert il.covered(2000)  # exhausted: nothing more can be fetched
        reason = il.out_of_reach(2010)
        assert reason is not None and "reaches back only to 202" in reason
        assert il.out_of_reach(2026) is None
        il.close()
    finally:
        cap.close()


def test_a_gone_listing_falls_back_to_the_wayback_copy(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    rsps.get(f"{TX}/news/releases", status=404)
    import re

    rsps.add(
        responses.GET,
        re.compile(
            r"https://web\.archive\.org/web/\d+id_/https://www\.texasattorneygeneral\.gov/.*"
        ),
        body=tx_page(("w", "From the archive", "April 01, 2012")),
    )
    ctx, cap, _ = make_ctx(config, conn)
    try:
        tx = adapters()["TX"](ctx)
        releases, _next = tx.list_page("0")
        assert [r.title for r in releases] == ["From the archive"]
        tx.close()
    finally:
        cap.close()


def test_repeated_refusals_in_one_run_record_the_state_as_manual_one_refusal_does_not(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    for n in (1, 2, 3, 4):
        add_court_row(
            conn, n, state="TX", details="Pleaded guilty in a Texas state court", sha=f"{n:064d}"
        )
    rsps.get(f"{TX}/news/releases", status=403)
    ctx, cap, registry = make_ctx(config, conn)
    try:
        first = component.process_row(row_of(conn, 1), ctx, registry)
        assert first.summary_json["court_status"] == "deferred_error"  # retried, not a negative
        assert sync.recorded(conn, "TX") is None  # one refusal records nothing
        component.process_row(row_of(conn, 2), ctx, registry)
        assert sync.recorded(conn, "TX") is None
        component.process_row(row_of(conn, 3), ctx, registry)  # the third refusal in this run
        mode, reason = sync.recorded(conn, "TX") or ("", "")
        assert mode == "manual" and "403" in (reason or "")
        fourth = component.process_row(row_of(conn, 4), ctx, registry)
        skipped = {m["name"]: m for m in fourth.summary_json["modules"]}  # type: ignore[index, union-attr]
        assert "adapter is manual" in skipped["court.state_ag"]["reason"]
    finally:
        cap.close()


def test_a_manual_state_is_skipped_with_its_reason_and_the_chain_continues(
    config: Config, conn: sqlite3.Connection
) -> None:
    add_court_row(conn, 1, state="WI", details="Pleaded guilty in a county court of the state")
    summary = process(config, conn, 1).summary_json
    reason = next(m["reason"] for m in summary["modules"] if m["name"] == "court.state_ag")  # type: ignore[index]
    assert "WI AG adapter is manual" in reason and "PDF" in reason
    assert any(m["name"] == "court.state_docket" for m in summary["modules"])  # type: ignore[index]


# --- matching --------------------------------------------------------------------------------------


def _release(title: str, day: date | None) -> Release:
    return Release("https://x/y", title, day, "AG", "")


def test_the_matching_rule_needs_the_name_an_election_charge_and_the_window() -> None:
    parts = parse_name("Alex Example")
    when = date(2014, 3, 5)
    text = "Alex Example, 47, of Austin, was indicted for illegal voting in the 2012 election."
    assert match_release_text(
        parts, _release("Austin man indicted", when), text, row_year=2014, slack=1
    )
    # a surname alone is not the subject
    assert not match_release_text(
        parts,
        _release("x", when),
        "Pat Example was indicted for voter fraud",
        row_year=2014,
        slack=1,
    )
    # no election charge
    assert not match_release_text(
        parts,
        _release("x", when),
        "Alex Example was indicted for tax evasion",
        row_year=2014,
        slack=1,
    )
    # outside the window, and no date at all
    assert not match_release_text(
        parts, _release("x", date(2020, 1, 1)), text, row_year=2014, slack=1
    )
    assert not match_release_text(parts, _release("x", None), text, row_year=2014, slack=1)
    # an initial and the surname is a match, flagged as weaker
    initials = "A. Example, 47, of Austin, was indicted for illegal voting in the 2012 election."
    hit = match_release_text(
        parse_name("A. Example"), _release("x", when), initials, row_year=2014, slack=1
    )
    assert hit is not None and hit[2] is True


# --- the module end to end -------------------------------------------------------------------------


SUBJECT_TEXT = (
    "AUSTIN - Alex Example, 47, of Austin, was indicted today for illegal voting in the 2012 "
    "general election, Attorney General announced. Case No. 3:13-cr-00045 is pending in the "
    "district court. " + "The investigation continues and the office thanks the county clerk. " * 6
)


def _release_page(text: str) -> str:
    return f"<html><body><article><h1>Austin man indicted</h1><p>{text}</p></article></body></html>"


def test_a_state_release_naming_the_subject_becomes_a_court_record_with_identifiers(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    add_court_row(conn, 1, state="TX", details="Charged in a Texas state court", year=2014)
    serve_tx(
        rsps,
        [
            tx_page(
                ("subject", "Austin man indicted for illegal voting", "March 05, 2014"),
                ("other", "Consumer protection settlement", "March 06, 2014"),
            ),
            tx_page(("older", "Old", "January 01, 2010")),
        ],
    )
    rsps.get(f"{TX}/news/releases/subject", body=_release_page(SUBJECT_TEXT))
    summary = process(config, conn, 1).summary_json
    assert summary["court_status"] == "press_release_only"  # a release, no docket
    rows = conn.execute(
        "SELECT source_kind, jurisdiction, strategy, case_number, weak FROM court_records"
    ).fetchall()
    assert rows == [("state_ag_release", "TX", "court.state_ag", "3:13-cr-00045", 0)]
    assert (
        conn.execute("SELECT count(*) FROM identifiers WHERE kind = 'case_number'").fetchone()[0]
        >= 1
    )
    assert summary["flags"]["state_ag"]["state"] == "TX"  # type: ignore[index]


def test_a_row_the_index_does_not_yet_reach_is_a_coverage_gap_not_a_not_found(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    add_court_row(conn, 1, state="TX", details="Charged in a Texas state court", year=2005)
    serve_tx(
        rsps, [tx_page((f"r{n}", f"Election {n}", "May 01, 2026")) for n in range(3)] + [tx_page()]
    )
    result = process(config, conn, 1)
    assert result.summary_json["court_status"] == "deferred_coverage"  # type: ignore[index]
    assert result.input_hash and result.input_hash.endswith(":coverage")


def test_the_index_freshness_only_changes_the_hash_of_rows_it_can_affect(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    add_court_row(conn, 1, state="TX", details="Charged in a Texas state court", year=2014)
    add_court_row(
        conn, 2, state="CT", details="Charged in a Connecticut court", year=2014, sha="2" * 64
    )
    ctx, cap, registry = make_ctx(config, conn)
    try:
        serve_tx(
            rsps,
            [
                tx_page(("a", "Voting 2014", "March 05, 2014")),
                tx_page(("b", "Old", "May 01, 2010")),
            ],
        )
        before = {n: component.input_hash(row_of(conn, n), ctx, registry) for n in (1, 2)}
        tx = adapters()["TX"](ctx)
        tx.harvest(2011, None)
        tx.close()
        after = {n: component.input_hash(row_of(conn, n), ctx, registry) for n in (1, 2)}
        assert after[1] != before[1]  # a TX row sees the index move
        assert after[2] == before[2]  # a CT row (no adapter) does not
    finally:
        cap.close()


# --- the matrix ------------------------------------------------------------------------------------


def test_the_coverage_matrix_sums_to_the_datasets_rows_and_explains_every_manual(
    config: Config, conn: sqlite3.Connection
) -> None:
    from efp import coverage

    for row_id, state in enumerate(["TX", "TX", "FL", "WI", "ZZ-unknown", "CT", "CT", "CT"], 1):
        add_court_row(conn, row_id, state=state, sha=f"{row_id:064d}")
    component._upsert_record(
        conn,
        1,
        Located(
            "state_ag_release",
            "TX",
            None,
            None,
            "press_release",
            "u",
            "court.state_ag",
            None,
            basis={"release_uuid": "u"},
        ),
    )
    sync.seed(conn)
    cov = coverage.build(conn, config)
    assert cov.total_rows == 8 == sum(s.rows for s in cov.states)
    by_state = {s.state: s for s in cov.states}
    assert by_state["TX"].mode == "automated" and by_state["TX"].located == {"court.state_ag": 1}
    assert by_state["FL"].mode == "manual" and "403" in by_state["FL"].reason
    assert by_state["CT"].mode == "none" and by_state["CT"].rows == 3
    text = coverage.render_md(cov)
    assert "| **Total** | **8**" in text and "## Why a state is not automated" in text
    assert "**FL** (manual, 1 rows)" in text and "**WI** (manual, 1 rows)" in text


def test_a_failed_smoke_test_is_recorded_as_broken_and_shows_in_the_matrix(
    config: Config, conn: sqlite3.Connection
) -> None:
    from efp import coverage

    add_court_row(conn, 1, state="OH")
    sync.seed(conn)
    sync.mark(conn, "OH", "broken", "live smoke: the first page parsed to no dated release")
    cov = coverage.build(conn, config)
    oh = next(s for s in cov.states if s.state == "OH")
    assert oh.mode == "broken" and "no dated release" in coverage.render_md(cov)


def test_a_stale_cassette_would_break_the_state_module_not_pass_silently(tmp_path: Path) -> None:
    parse, portal = PARSERS["il"]
    assert parse("<html>a redesigned page with no items</html>", portal) == []
    assert tx_mod.parse_listing("<html></html>", TX) == []
    assert ca_mod.parse_listing("<html></html>", "x") == [] and oh_mod.parse_listing("", "x") == []
    assert isinstance(SiteBlocked("x"), Exception) and tmp_path.exists()


# --- review findings: failures are not "finished", hashes, candidates, freshness, probes -------------


def _oh_month(
    rsps: responses.RequestsMock, month: str, *items: tuple[str, str, str], status: int = 200
) -> None:
    body = "".join(
        f'<div class="ohio-news"><h2 class="h3"><a href="/Media/News-Releases/{month}/{slug}">{title}</a>'
        f'</h2><div class="news-summary"><p>{title} summary</p></div>'
        f'<div class="news-date">{day}</div></div>'
        for slug, title, day in items
    )
    rsps.get(
        f"https://www.ohioattorneygeneral.gov/Media/News-Releases/{month}", body=body, status=status
    )


@pytest.mark.parametrize("status", [429, 500, 503])
def test_a_listing_that_fails_to_load_is_an_error_not_the_end_of_the_archive(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock, status: int
) -> None:
    rsps.get(f"{TX}/news/releases", status=status)
    ctx, cap, _ = make_ctx(config, conn)
    try:
        tx = adapters()["TX"](ctx)
        with pytest.raises(CaptureError, match=str(status)):
            tx.harvest(2013, None)
        assert not tx.index.started()  # nothing was recorded as walked or finished
        _cursor, _oldest, done, _pages = tx.index.progress()
        assert done is False and tx.covered(2013) is False
        tx.close()
    finally:
        cap.close()


def test_a_month_that_fails_is_not_skipped_but_a_month_with_no_page_is_empty(
    config: Config,
    conn: sqlite3.Connection,
    rsps: responses.RequestsMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oh_mod.Ohio, "first_cursor", lambda self: "2026-03")
    _oh_month(rsps, "March-2026", status=500)
    _oh_month(rsps, "February-2026", status=404)
    _oh_month(rsps, "January-2026", status=404)
    ctx, cap, _ = make_ctx(config, conn)
    try:
        oh = adapters()["OH"](ctx)
        with pytest.raises(CaptureError):
            oh.list_page("2026-03")
        releases, following = oh.list_page("2026-02")  # a month with no page: no releases
        assert releases == [] and following == "2026-01"
        assert oh.list_page("2026-01")[1] == "2025-12"  # the year rolls over
        oh.close()
    finally:
        cap.close()


def test_a_changed_recorded_mode_changes_the_hash_so_recheck_revisits_the_row(
    config: Config, conn: sqlite3.Connection
) -> None:
    add_court_row(conn, 1, state="TX", details="Charged in a Texas state court", year=2014)
    ctx, cap, registry = make_ctx(config, conn)
    try:
        original = component.input_hash(row_of(conn, 1), ctx, registry)
        sync.mark(conn, "TX", "manual", "HTTP 403")
        blocked = component.input_hash(row_of(conn, 1), ctx, registry)
        assert blocked != original
        sync.write(
            conn,
            "TX",
            mode="automated",
            portal=adapters()["TX"].portal,
            adapter_module="x",
            reason=None,
        )
        assert component.input_hash(row_of(conn, 1), ctx, registry) == original  # automated again
    finally:
        cap.close()


def test_the_candidate_cap_reads_the_subjects_release_first_and_says_when_it_truncated(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    capped = config.model_copy(
        update={"court": config.court.model_copy(update={"ag_max_candidates": 2})}
    )
    add_court_row(conn, 1, state="TX", details="Charged in a Texas state court", year=2014)
    generic = [(f"g{n}", f"Election integrity update {n}", "March 05, 2014") for n in range(4)]
    serve_tx(
        rsps,
        [
            tx_page(
                *generic, ("subject", "Example indicted for illegal voting", "January 02, 2013")
            ),
            tx_page(("older", "Old", "January 01, 2010")),
        ],
    )
    rsps.get(f"{TX}/news/releases/subject", body=_release_page(SUBJECT_TEXT))
    rsps.get(f"{TX}/news/releases/g0", body=_release_page("Nothing about anyone."))
    rsps.get(f"{TX}/news/releases/g1", body=_release_page("Nothing about anyone."))
    summary = process(capped, conn, 1).summary_json
    assert summary["flags"]["ag_candidates_truncated"] == 5  # type: ignore[index]
    assert any("read 2" in n for n in summary["notes"])  # type: ignore[union-attr]
    kinds = conn.execute("SELECT source_kind FROM court_records").fetchall()
    assert kinds == [("state_ag_release",)]  # the surname release was read despite the cap


def test_the_freshness_walk_goes_on_until_a_known_release_even_past_the_row_page_budget(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    serve_tx(rsps, [tx_page(("known", "Known", "May 01, 2010"))])
    ctx, cap, _ = make_ctx(config, conn)
    try:
        tx = adapters()["TX"](ctx)
        tx.harvest(2013, None)  # the index holds "known"
        for number in range(4):  # four pages of new releases (more than the 3-page row budget)
            rsps.replace(
                responses.GET,
                f"{TX}/news/releases",
                body=tx_page((f"n{number}", f"New {number}", f"June 0{number + 1}, 2026")),
                match=[responses.matchers.query_param_matcher({"page": str(number)})],
            ) if number == 0 else rsps.get(
                f"{TX}/news/releases",
                body=tx_page((f"n{number}", f"New {number}", f"June 0{number + 1}, 2026")),
                match=[responses.matchers.query_param_matcher({"page": str(number)})],
            )
        rsps.get(
            f"{TX}/news/releases",
            body=tx_page(("known", "Known", "May 01, 2010")),
            match=[responses.matchers.query_param_matcher({"page": "4"})],
        )
        cap.clear_throttles()
        assert tx.refresh_newest() == 5 > config.court.ag_harvest_pages_per_row
        assert all(tx.index.has(f"{TX}/news/releases/n{n}") for n in range(4))
        tx.close()
    finally:
        cap.close()


def test_an_empty_month_does_not_end_the_freshness_walk(
    config: Config,
    conn: sqlite3.Connection,
    rsps: responses.RequestsMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oh_mod.Ohio, "first_cursor", lambda self: "2026-11")
    _oh_month(rsps, "November-2026", status=404)  # the month has no page yet
    _oh_month(
        rsps,
        "October-2026",
        ("new", "New October release", "10/20/2026"),
        ("old", "Old release", "10/02/2026"),
    )
    ctx, cap, _ = make_ctx(config, conn)
    try:
        oh = adapters()["OH"](ctx)
        oh.index.add(
            [
                Release(
                    "https://www.ohioattorneygeneral.gov/Media/News-Releases/October-2026/old",
                    "Old release",
                    date(2026, 10, 2),
                    "AG",
                    "",
                )
            ]
        )
        oh.index.save_progress("2026-09", "2026-10-02", False, 1)
        assert oh.refresh_newest() == 2
        assert oh.index.has(
            "https://www.ohioattorneygeneral.gov/Media/News-Releases/October-2026/new"
        )
        oh.close()
    finally:
        cap.close()


def test_a_403_on_one_release_page_is_that_releases_failure_not_a_verdict_on_the_state(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    add_court_row(conn, 1, state="TX", details="Charged in a Texas state court", year=2014)
    serve_tx(
        rsps,
        [
            tx_page(("r", "Voter fraud charge", "March 05, 2014")),
            tx_page(("o", "Old", "May 01, 2010")),
        ],
    )
    rsps.get(f"{TX}/news/releases/r", status=403)
    result = process(config, conn, 1)
    assert result.summary_json["court_status"] == "deferred_error"  # not a clean not_found
    assert sync.recorded(conn, "TX") is None  # the state is not marked manual by one page


def test_a_probe_is_never_the_cached_page_and_skips_an_empty_first_month(
    config: Config,
    conn: sqlite3.Connection,
    rsps: responses.RequestsMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serve_tx(rsps, [tx_page(("a", "Anything", "May 01, 2026"))])
    ctx, cap, _ = make_ctx(config, conn)
    try:
        tx = adapters()["TX"](ctx)
        tx.list_page("0")  # cached from here on
        before = len(rsps.calls)
        assert tx.probe() and len(rsps.calls) == before + 1  # a live request, not the cache
        tx.close()
    finally:
        cap.close()
    monkeypatch.setattr(oh_mod.Ohio, "first_cursor", lambda self: "2026-11")
    _oh_month(rsps, "November-2026", status=404)
    _oh_month(rsps, "October-2026", ("x", "A release", "10/20/2026"))
    ctx2, cap2, _ = make_ctx(config, conn)
    try:
        oh = adapters()["OH"](ctx2)
        assert [r.title for r in oh.probe()] == ["A release"]  # an empty first month is not broken
        oh.close()
    finally:
        cap2.close()


def test_the_charge_must_be_in_the_title_or_the_opening_not_elsewhere_on_the_page() -> None:
    parts = parse_name("Alex Example")
    when = date(2014, 3, 5)
    far = (
        "Alex Example was sentenced for theft. "
        + "Filler about the case. " * 80
        + "Register to vote today."
    )
    assert not match_release_text(
        parts, _release("Austin man sentenced", when), far, row_year=2014, slack=1
    )


def test_a_da_release_is_a_da_record_and_several_matches_are_weak(
    config: Config,
    conn: sqlite3.Connection,
    rsps: responses.RequestsMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = tx_mod.parse_listing
    monkeypatch.setattr(
        tx_mod,
        "parse_listing",
        lambda page, portal: [
            Release(r.url, r.title, r.date, "DA:Travis", r.snippet) for r in real(page, portal)
        ],
    )
    add_court_row(conn, 1, state="TX", details="Charged in a Texas state court", year=2014)
    serve_tx(
        rsps,
        [
            tx_page(
                ("one", "Example indicted for illegal voting", "March 05, 2014"),
                ("two", "Example pleads in illegal voting case", "April 05, 2014"),
            ),
            tx_page(("o", "Old", "May 01, 2010")),
        ],
    )
    rsps.get(f"{TX}/news/releases/one", body=_release_page(SUBJECT_TEXT))
    rsps.get(
        f"{TX}/news/releases/two",
        body=_release_page(SUBJECT_TEXT.replace("indicted today", "pleaded")),
    )
    summary = process(config, conn, 1).summary_json
    rows = conn.execute("SELECT source_kind, weak FROM court_records ORDER BY record_id").fetchall()
    assert rows == [("da_release", 1), ("da_release", 1)]  # two releases: neither settles it
    assert summary["court_status"] == "candidate_only"


def test_the_module_is_available_when_a_state_is_automated_and_not_otherwise(
    config: Config, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, cap, _ = make_ctx(config, conn)
    try:
        assert CourtStateAg().available(ctx).ok
        for cls in adapters().values():
            monkeypatch.setattr(cls, "mode", "manual")
        assert not CourtStateAg().available(ctx).ok
    finally:
        cap.close()


def test_recorded_listing_urls_match_the_cassettes(
    config: Config, conn: sqlite3.Connection
) -> None:
    ctx, cap, _ = make_ctx(config, conn)
    try:
        for state in ("TX", "CA", "IL"):
            meta = json.loads((CASSETTES / state.lower() / "meta.json").read_text())
            adapter = adapters()[state](ctx)
            assert adapter.listing_url(adapter.first_cursor()) == meta["listing_url"]
            adapter.close()
    finally:
        cap.close()


def test_a_page_that_parses_to_nothing_is_never_recorded_as_a_finished_archive(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    rsps.get(f"{TX}/news/releases", body="<html><body>Checking your browser...</body></html>")
    ctx, cap, _ = make_ctx(config, conn)
    try:
        tx = adapters()["TX"](ctx)
        with pytest.raises(CaptureError, match="parsed to no releases"):
            tx.harvest(2013, None)
        assert not tx.index.started() and tx.covered(2013) is False  # nothing recorded as done
        tx.close()
    finally:
        cap.close()


def test_a_walk_of_empty_months_with_nothing_indexed_raises_instead_of_finishing(
    config: Config,
    conn: sqlite3.Connection,
    rsps: responses.RequestsMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import re

    monkeypatch.setattr(oh_mod.Ohio, "first_cursor", lambda self: "2026-11")
    rsps.add(
        responses.GET,
        re.compile(r"https://www\.ohioattorneygeneral\.gov/Media/News-Releases/.*"),
        status=404,
    )
    ctx, cap, _ = make_ctx(config, conn)
    try:
        oh = adapters()["OH"](ctx)
        with pytest.raises(CaptureError, match="nothing is indexed"):
            oh.harvest(2000, None, max_pages=100)
        assert oh.index.count() == 0 and oh.index.progress()[2] is False  # never "done"
        assert oh.refresh_newest() == 0  # and an empty index has nothing to refresh
        oh.close()
    finally:
        cap.close()


def test_a_throttle_or_a_refusal_during_a_smoke_test_is_reported_not_recorded(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    import re

    from efp.livecheck import _ag_live

    rsps.get(f"{TX}/news/releases", status=503)  # transient
    rsps.get("https://illinoisattorneygeneral.gov/news-room/", status=403)  # one refusal
    rsps.get(
        "https://oag.ca.gov/media/news", body="<html>a challenge page</html>"
    )  # parses to nothing
    rsps.add(
        responses.GET,
        re.compile(r"https://www\.ohioattorneygeneral\.gov/Media/News-Releases/.*"),
        status=503,
    )
    _ctx, cap, _ = make_ctx(config, conn)
    try:
        checks = {c.name: c for c in _ag_live(cap, config, conn)}
    finally:
        cap.close()
    assert not any(c.ok for c in checks.values())
    assert sync.recorded(conn, "TX") is None  # a 503 is retried, not recorded
    assert sync.recorded(conn, "IL") is None  # one refusal is reported, not recorded
    assert sync.recorded(conn, "OH") is None
    mode, reason = sync.recorded(conn, "CA") or ("", "")
    assert mode == "broken" and "parsed to no releases" in (
        reason or ""
    )  # loaded, parsed to nothing
    assert "transient" in checks["live: state ag TX"].detail


def test_a_mid_walk_empty_page_is_an_error_a_404_is_the_end_and_a_pager_page_is_the_last(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    rsps.get(
        f"{TX}/news/releases",
        body=tx_page(("a", "Voting 2026", "May 01, 2026")),
        match=[responses.matchers.query_param_matcher({"page": "0"})],
    )
    rsps.get(
        f"{TX}/news/releases",
        body="<html><body>Checking your browser before accessing...</body></html>",
        match=[responses.matchers.query_param_matcher({"page": "1"})],
    )
    ctx, cap, _ = make_ctx(config, conn)
    try:
        tx = adapters()["TX"](ctx)
        with pytest.raises(CaptureError, match="parsed to no releases"):
            tx.harvest(2000, None, max_pages=10)
        assert tx.index.count() == 1  # page 0 is kept...
        cursor, _oldest, done, _pages = tx.index.progress()
        assert done is False and cursor == "1"  # ...and the walk can resume at the bad page
        assert not tx.covered(2000)  # so rows are not told the archive is exhausted
        tx.close()
    finally:
        cap.close()


def test_a_404_page_is_the_end_of_the_archive_and_a_layout_without_a_pager_is_an_error(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    rsps.get(
        f"{TX}/news/releases",
        body=tx_page(("a", "Voting 2026", "May 01, 2026")),
        match=[responses.matchers.query_param_matcher({"page": "0"})],
    )
    rsps.get(
        f"{TX}/news/releases",
        status=404,
        match=[responses.matchers.query_param_matcher({"page": "1"})],
    )
    rsps.add(
        responses.GET, re.compile(r"https://web\.archive\.org/.*"), status=404
    )  # no copy either
    ctx, cap, _ = make_ctx(config, conn)
    try:
        tx = adapters()["TX"](ctx)
        tx.harvest(2000, None, max_pages=10)
        assert tx.index.progress()[2] is True  # a 404 past the last page is a positive end
        tx.close()
    finally:
        cap.close()
    no_pager = "".join(
        '<div class="m-b-3"><h4><a href="/news/releases/x">Voting</a></h4><p>s</p>'
        '<p class="meta m-b-0">May 01, 2026 | Press Release</p></div>'
        for _ in range(1)
    )
    page = f"<html><body>{no_pager}</body></html>"
    rsps.replace(
        responses.GET,
        f"{TX}/news/releases",
        body=page,
        match=[responses.matchers.query_param_matcher({"page": "0"})],
    )
    ctx2, cap2, _ = make_ctx(config, conn)
    try:
        fresh = adapters()["CA"](ctx2)  # a state whose listing has releases but no pager at all
        rsps.get("https://oag.ca.gov/media/news", body=ca_page_without_pager())
        with pytest.raises(CaptureError, match="no pager"):
            fresh.list_page("0")
        fresh.close()
    finally:
        cap2.close()


def ca_page_without_pager() -> str:
    return (
        '<div class="views-row views-row-1"><a href="/news/press-releases/x">A settlement</a>'
        '<span content="2026-05-01T00:00:00-08:00"></span></div>'
    )

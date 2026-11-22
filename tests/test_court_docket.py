"""Build 09: the portal probe, the docket adapters, the module, the assisted queue and the matrix."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import responses
import stamina

import efp.court as component
from efp.capture import CaptureError
from efp.config import Config
from efp.court.adapters.ag import sync
from efp.court.adapters.docket import DocketHit, adapters
from efp.court.adapters.docket import probe as dp
from efp.court.adapters.docket import wi as wi_mod
from efp.court.adapters.docket.module import CourtStateDocket
from efp.court.adapters.docket.query import build_query
from efp.court.names import parse_name
from tests.capturehelpers import make_capture
from tests.courthelpers import add_court_row, court_config, make_ctx, process, row_of

CASSETTES = Path(__file__).parent / "cassettes" / "docket" / "wi"
WI_SEARCH = wi_mod.SEARCH
STATE_COURT = "Charged in a Wisconsin state court"


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


def cleared(config: Config, *states: str) -> Config:
    return config.model_copy(
        update={"court": config.court.model_copy(update={"docket_terms_cleared": list(states)})}
    )


def record_portal(conn: sqlite3.Connection, state: str, mode: str, reason: str = "") -> None:
    sync.write(
        conn,
        state,
        kind="docket",
        mode=mode,
        portal=f"https://portal.{state.lower()}.example/",
        adapter_module="x",
        reason=reason or None,
    )


# --- what the entry page shows ---------------------------------------------------------------------


def test_the_entry_page_is_read_for_challenge_login_fee_form_script_platform_and_terms() -> None:
    base = "https://portal.example/search"
    assert dp.analyse("<h1>Just a moment...</h1> cf-challenge", 200, base).challenge
    assert dp.analyse('<form><input type="password" name="pw"></form>', 200, base).account_wall
    assert dp.analyse("<p>A subscription is required: $25 per search</p>", 200, base).fee_wall
    form = dp.analyse('<form><input name="lastName"><input name="firstName"></form>', 200, base)
    assert form.search_form and not form.account_wall  # a login beside a search is not a wall
    spa = "<html><body><div id='root'></div><script src=a.js></script><script src=b.js></script></body></html>"
    assert dp.analyse(spa, 200, base).js_only
    assert (
        dp.analyse("<a href='https://x.test/odyssey/Portal/Home'>x</a>", 200, base).fingerprint
        == "odyssey"
    )
    terms = dp.analyse('<a href="/terms">Terms of Use</a>', 200, base)
    assert terms.terms_url == "https://portal.example/terms"
    blocked = dp.analyse("", 403, base)
    assert blocked.blocked and not blocked.search_form


SEED = dp.Seed("ZZ", "Portal", "https://portal.example/", "other", "statewide")


@pytest.mark.parametrize(
    ("seed", "facts", "adapter", "cleared_state", "mode", "wants"),
    [
        (
            dp.Seed("ZZ", "p", "u", "none", "county:X", "clerk by phone"),
            dp.Facts(),
            False,
            False,
            "manual",
            "no-online-search",
        ),
        (SEED, dp.Facts(status=404), False, False, "manual", "unreachable"),
        (SEED, dp.Facts(status=403, blocked=True), False, False, "assisted", "blocked"),
        (SEED, dp.Facts(status=200, account_wall=True), False, False, "assisted", "account"),
        (SEED, dp.Facts(status=200, fee_wall=True), False, False, "assisted", "fee"),
        (SEED, dp.Facts(status=200, challenge=True), True, True, "assisted", "challenge"),
        (
            SEED,
            dp.Facts(status=200, search_form=True),
            True,
            False,
            "assisted",
            "terms-not-cleared",
        ),
        (SEED, dp.Facts(status=200, search_form=True), False, False, "assisted", "no-adapter"),
        (SEED, dp.Facts(status=200, search_form=True), True, True, "automated", ""),
        (SEED, dp.Facts(status=200, js_only=True), False, False, "assisted", "js-only"),
        (SEED, dp.Facts(status=200, js_only=True), True, False, "assisted", "terms-not-cleared"),
        (SEED, dp.Facts(status=200, js_only=True), True, True, "automated", "js-only"),
        (SEED, dp.Facts(status=200), False, False, "assisted", "search-not-found"),
    ],
)
def test_the_mode_rules_classify_a_portal_with_its_reason(
    seed: dp.Seed, facts: dp.Facts, adapter: bool, cleared_state: bool, mode: str, wants: str
) -> None:
    verdict = dp.classify(seed, facts, adapter_exists=adapter, cleared=cleared_state)
    assert verdict is not None and verdict[0] == mode and verdict[1] == wants and verdict[2]


def test_a_transient_failure_classifies_nothing_so_the_portal_is_probed_again() -> None:
    assert dp.classify(SEED, dp.Facts(transient=True), adapter_exists=True, cleared=True) is None


def test_nothing_is_automated_unless_the_state_is_cleared_and_an_adapter_exists() -> None:
    open_page = dp.Facts(status=200, search_form=True)
    for adapter, cleared_state in ((False, False), (True, False), (False, True)):
        verdict = dp.classify(SEED, open_page, adapter_exists=adapter, cleared=cleared_state)
        assert verdict is not None and verdict[0] != "automated"


def test_the_state_records_its_most_automatable_portal_statewide_before_county() -> None:
    def verdict(state: str, scope: str, mode: str) -> dp.Probed:
        seed = dp.Seed(state, f"{scope}-{mode}", "u", "other", scope)
        return dp.Probed(seed, dp.Facts(), dp.Verdict(seed, dp.Facts(), mode, "w", "r"))

    best = dp.best_per_state(
        [
            verdict("TX", "county:Harris", "assisted"),
            verdict("TX", "statewide", "assisted"),
            verdict("FL", "county:A", "manual"),
            verdict("FL", "county:B", "assisted"),
        ]
    )
    assert best["TX"].seed.scope == "statewide" and best["FL"].seed.scope == "county:B"


def test_the_seed_list_covers_the_twenty_states_and_a_snapshot_row_has_a_reason() -> None:
    states = {s.state for s in dp.load_seeds()}
    assert states == set(
        [
            "MN",
            "TX",
            "FL",
            "NC",
            "CA",
            "WI",
            "OH",
            "KY",
            "IN",
            "IL",
            "NJ",
            "MS",
            "AL",
            "AZ",
            "PA",
            "WV",
            "VA",
            "MO",
            "IA",
            "NH",
        ]
    )
    rows = dp.snapshot_rows([dp.Probed(SEED, dp.Facts(transient=True, error="timeout"))])
    assert "not judged" in rows[0]["reason"] and rows[0]["mode"] == ""


def test_the_probe_loads_the_entry_point_once_and_never_posts_a_search(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    rsps.get("https://portal.example/", body="<html><form><input name='lastName'></form></html>")
    ctx, cap, _ = make_ctx(config, conn)
    try:
        facts = dp.probe_seed(ctx, SEED)
    finally:
        cap.close()
    assert facts.search_form and facts.status == 200
    assert [c.request.method for c in rsps.calls] == ["GET"] and len(rsps.calls) == 1


def test_a_503_at_the_entry_point_is_transient_not_a_verdict(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    rsps.get("https://portal.example/", status=503)
    ctx, cap, _ = make_ctx(config, conn)
    try:
        facts = dp.probe_seed(ctx, SEED)
    finally:
        cap.close()
    assert facts.transient and dp.classify(SEED, facts, adapter_exists=False, cleared=False) is None


# --- the Wisconsin adapter on synthetic cassettes --------------------------------------------------


def test_the_wisconsin_answer_parses_hits_no_hits_and_refuses_an_unknown_shape() -> None:
    hits = wi_mod.parse_cases(json.loads((CASSETTES / "hits.json").read_text()))
    assert [h.case_number for h in hits] == ["2014CF000123", "2014CF000456"]
    assert hits[0].court == "Dane County Circuit Court" and hits[0].filed_date is not None
    assert wi_mod.parse_cases(json.loads((CASSETTES / "nohits.json").read_text())) == []
    with pytest.raises(CaptureError, match="unrecognised shape"):
        wi_mod.parse_cases({"unexpected": True})


def test_the_wisconsin_adapter_posts_a_json_search_and_a_portal_error_page_raises(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    rsps.post(
        WI_SEARCH, body=(CASSETTES / "hits.json").read_text(), content_type="application/json"
    )
    ctx, cap, _ = make_ctx(config, conn)
    try:
        adapter = adapters()["WI"](ctx)
        found = adapter.search("Alex Example", 2014, window=1)
        sent = json.loads(rsps.calls[0].request.body)
        assert sent["lastName"] == "Example" and sent["firstName"] == "Alex"  # plain values
        assert sent["filingDate"]["end"] == "2015-12-31"
        assert len(found) == 2
        rsps.replace(responses.POST, WI_SEARCH, body=(CASSETTES / "error.html").read_text())
        with pytest.raises(CaptureError, match="not JSON"):
            adapter.search("Alex Example", 2014, window=1)
    finally:
        cap.close()


# --- the module -------------------------------------------------------------------------------------


def test_a_state_with_no_probed_portal_is_skipped_with_the_reason_and_the_chain_goes_on(
    config: Config, conn: sqlite3.Connection
) -> None:
    add_court_row(conn, 1, state="WI", details=STATE_COURT)
    summary = process(config, conn, 1).summary_json
    reason = next(m["reason"] for m in summary["modules"] if m["name"] == "court.state_docket")  # type: ignore[index]
    assert "no docket portal probed for WI" in reason and summary["court_status"] == "not_found"


def test_an_assisted_portal_gives_a_gated_row_with_the_same_query_every_time(
    config: Config, conn: sqlite3.Connection
) -> None:
    add_court_row(conn, 1, state="WI", details=STATE_COURT, year=2014)
    record_portal(conn, "WI", "assisted", "challenge: a challenge page stands in front")
    first = process(config, conn, 1)
    second = process(config, conn, 1)
    query = first.summary_json["assisted"]  # type: ignore[index]
    assert query == second.summary_json["assisted"]  # deterministic
    assert query["name"] == "Example, Alex" and query["filed_from"] == "2006-01-01"
    assert query["after"] == "efp import --row 1 --source portal <file>"
    assert first.status == "done"  # not needs_review: later stages are not held
    assert first.summary_json["court_status"] == "gated_portal"  # type: ignore[index]


def test_a_manual_portal_records_the_reason_and_only_an_account_or_fee_gates_the_row(
    config: Config, conn: sqlite3.Connection
) -> None:
    add_court_row(conn, 1, state="WI", details=STATE_COURT)
    record_portal(conn, "WI", "manual", "no-online-search: county clerk by phone")
    quiet = process(config, conn, 1).summary_json
    assert quiet["court_status"] == "not_found" and not quiet["assisted"]  # type: ignore[index]
    record_portal(conn, "WI", "manual", "account: a subscription is required")
    assert process(config, conn, 1).summary_json["court_status"] == "gated_portal"  # type: ignore[index]


def test_an_automated_portal_the_operator_has_not_cleared_is_treated_as_assisted(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    add_court_row(conn, 1, state="WI", details=STATE_COURT)
    record_portal(conn, "WI", "automated")
    result = process(config, conn, 1)  # WI is not in court.docket_terms_cleared
    assert result.summary_json["court_status"] == "gated_portal"  # type: ignore[index]
    assert not [c for c in rsps.calls if "wicourts" in c.request.url]  # no search was made


def test_a_cleared_automated_portal_finds_a_docket_with_a_case_number_and_identifiers(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    cfg = cleared(config, "WI")
    add_court_row(conn, 1, state="WI", details=STATE_COURT, year=2014)
    record_portal(conn, "WI", "automated")
    hits = json.loads((CASSETTES / "hits.json").read_text())
    hits["result"]["cases"] = hits["result"]["cases"][:1]
    rsps.post(WI_SEARCH, json=hits)
    rsps.get(
        "https://wcca.wicourts.gov/caseDetail.html",
        body="<html>Case 2014CF000123</html>",
        match=[
            responses.matchers.query_param_matcher({"caseNo": "2014CF000123", "countyNo": "13"})
        ],
    )
    summary = process(cfg, conn, 1).summary_json
    assert summary["court_status"] == "primary_record"  # type: ignore[index]
    row = conn.execute(
        "SELECT source_kind, jurisdiction, strategy, case_number, weak FROM court_records"
    ).fetchone()
    assert row == ("state_docket", "WI", "court.state_docket", "2014CF000123", 0)


def test_several_matching_dockets_are_candidates_and_a_portal_error_defers_the_row(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    cfg = cleared(config, "WI")
    add_court_row(conn, 1, state="WI", details=STATE_COURT, year=2014)
    record_portal(conn, "WI", "automated")
    twice = {
        "result": {
            "cases": [
                {
                    "caseNo": f"2014CF00012{n}",
                    "caption": "State v. Alex Example",
                    "countyNo": n,
                    "countyName": "Dane",
                    "filingDate": "2014-03-05",
                }
                for n in (3, 4)
            ]
        }
    }
    rsps.post(WI_SEARCH, json=twice)
    rsps.get("https://wcca.wicourts.gov/caseDetail.html", body="ok")
    assert process(cfg, conn, 1).summary_json["court_status"] == "candidate_only"  # type: ignore[index]
    rsps.replace(responses.POST, WI_SEARCH, body="<html>down</html>")
    add_court_row(conn, 2, state="WI", details=STATE_COURT, year=2014, sha="2" * 64)
    failed = process(cfg, conn, 2).summary_json
    assert failed["court_status"] == "deferred_error"  # type: ignore[index]


def test_repeated_refusals_in_one_run_record_the_portal_as_assisted_one_does_not(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    cfg = cleared(config, "WI")
    for n in (1, 2, 3):
        add_court_row(conn, n, state="WI", details=STATE_COURT, sha=f"{n:064d}")
    record_portal(conn, "WI", "automated")
    rsps.post(WI_SEARCH, status=403)
    ctx, cap, registry = make_ctx(cfg, conn)
    try:
        component.process_row(row_of(conn, 1), ctx, registry)
        assert sync.recorded(conn, "WI", "docket") == ("automated", None)
        component.process_row(row_of(conn, 2), ctx, registry)
        component.process_row(row_of(conn, 3), ctx, registry)
        mode, reason = sync.recorded(conn, "WI", "docket") or ("", "")
        assert mode == "assisted" and "refusals" in (reason or "")
    finally:
        cap.close()


def test_the_hash_changes_with_the_docket_mode_and_the_cleared_list_for_that_state_only(
    config: Config, conn: sqlite3.Connection
) -> None:
    add_court_row(conn, 1, state="WI", details=STATE_COURT)
    add_court_row(conn, 2, state="CT", details="Charged in a Connecticut state court", sha="2" * 64)
    ctx, cap, registry = make_ctx(config, conn)
    ctx_cleared, cap2, _ = make_ctx(cleared(config, "WI"), conn)
    try:
        before = {n: component.input_hash(row_of(conn, n), ctx, registry) for n in (1, 2)}
        record_portal(conn, "WI", "assisted", "challenge: x")
        after = {n: component.input_hash(row_of(conn, n), ctx, registry) for n in (1, 2)}
        assert after[1] != before[1] and after[2] == before[2]
        assert component.input_hash(row_of(conn, 1), ctx_cleared, registry) != after[1]
    finally:
        cap.close()
        cap2.close()


def test_gated_portal_is_a_status_only_when_nothing_else_was_found() -> None:
    assert component.court_status([], gated_portal=True) == "gated_portal"
    assert component.court_status([], gated_portal=True, errored=True) == "deferred_error"
    assert component.court_status([], gated_portal=False) == "not_found"


def test_the_assisted_query_is_built_from_the_row_alone() -> None:
    parts = parse_name("Alex Q. Example")
    one = build_query(
        portal_url="https://p/", platform="x", parts=parts, year=2014, after=3, row_id=9
    )
    assert one == build_query(
        portal_url="https://p/", platform="x", parts=parts, year=2014, after=3, row_id=9
    )
    assert one["name"] == "Example, Alex Q" and one["filed_to"] == "2017-12-31"


# --- the browser capture ----------------------------------------------------------------------------


def test_the_browser_capture_is_deferred_when_docker_is_not_there_and_stores_a_wacz_when_it_is(
    config: Config, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.dbhelpers import insert_row

    insert_row(conn, 1)
    conn.commit()
    cap = make_capture(config, conn)
    try:
        monkeypatch.setattr("shutil.which", lambda _name: None)
        with pytest.raises(CaptureError, match="docker is not available"):
            cap.browser("https://example.test/search", row_id=1, stage="court")
        monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/docker")

        def fake_run(args: list[str], **_kw: Any) -> subprocess.CompletedProcess[bytes]:
            if args[1] in ("info", "image"):
                return subprocess.CompletedProcess(args, 0, b"", b"")
            mount = args[args.index("-v") + 1].split(":")[0]
            target = Path(mount) / "collections" / "efp"
            target.mkdir(parents=True)
            (target / "efp.wacz").write_bytes(b"PK-synthetic-wacz")
            return subprocess.CompletedProcess(args, 0, b"", b"")

        monkeypatch.setattr(subprocess, "run", fake_run)
        record = cap.browser("https://example.test/search", row_id=1, stage="court")
        row = conn.execute(
            "SELECT tool, wacz_path, content_type FROM captures WHERE capture_id = ?",
            (record.capture_id,),
        ).fetchone()
        assert row[0] == "browsertrix" and row[1] and row[2] == "application/wacz"
        monkeypatch.setattr(
            subprocess, "run", lambda args, **_k: subprocess.CompletedProcess(args, 1, b"", b"")
        )
        with pytest.raises(CaptureError, match="docker is not running"):
            cap.browser("https://example.test/other", row_id=1, stage="court")
    finally:
        cap.close()


# --- the assisted queue and the matrix --------------------------------------------------------------


@pytest.fixture
def cli_config(config: Config, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("EFP_CONFIG", str(Path(config.paths.data_dir).parent / "config.toml"))
    return config


def _invoke(*args: str) -> Any:
    from typer.testing import CliRunner

    from efp.cli import app

    return CliRunner().invoke(app, list(args))


def test_review_assisted_lists_rows_with_their_query_and_writes_the_csv(
    cli_config: Config, conn: sqlite3.Connection
) -> None:
    import csv

    add_court_row(conn, 1, state="WI", details=STATE_COURT, year=2014)
    record_portal(conn, "WI", "assisted", "challenge: x")
    summary = process(cli_config, conn, 1).summary_json
    conn.execute(
        "UPDATE case_stage SET summary_json = ? WHERE row_id = 1 AND stage = 'court'",
        (json.dumps(summary),),
    )
    conn.commit()
    result = _invoke("review", "court", "--assisted")
    assert result.exit_code == 0, result.output
    assert (
        'search "Example, Alex"' in result.output
        and "efp import --row 1 --source portal" in result.output
    )
    queue = Path(cli_config.paths.data_dir) / "queues" / "assisted_dockets.csv"
    rows = list(csv.DictReader(queue.open()))
    assert rows[0]["row_id"] == "1" and rows[0]["name"] == "Example, Alex"
    assert _invoke("review", "identity", "--assisted").exit_code == 2  # the court stage only


def test_the_matrix_carries_the_docket_columns_and_the_status_distribution_by_state(
    config: Config, conn: sqlite3.Connection
) -> None:
    from efp import coverage

    add_court_row(conn, 1, state="WI", details=STATE_COURT)
    add_court_row(conn, 2, state="WI", details=STATE_COURT, sha="2" * 64)
    record_portal(conn, "WI", "assisted", "terms-not-cleared: adapter exists, terms unread")
    for row_id, status in ((1, "gated_portal"), (2, "not_found")):
        conn.execute(
            "UPDATE case_stage SET status = 'done', summary_json = ? "
            "WHERE row_id = ? AND stage = 'court'",
            (json.dumps({"court_status": status}), row_id),
        )
    conn.commit()
    cov = coverage.build(conn, config)
    wi = next(d for d in cov.dockets if d.state == "WI")
    assert wi.rows == 2 and wi.mode == "assisted" and wi.wants == "terms-not-cleared" and wi.adapter
    assert wi.statuses == {"gated_portal": 1, "not_found": 1}
    text = coverage.render_md(cov)
    assert "## Docket portals" in text and "| WI | 2 | assisted | terms-not-cleared | yes |" in text
    assert "### Docket portal reasons" in text


def test_the_module_is_always_available_and_named() -> None:
    assert CourtStateDocket().name == "court.state_docket"
    assert DocketHit("c", "n", "cap", None, "u").document_urls == ()


# --- review findings: county portals, the gated list, provenance, the browser, heuristics --------------


def test_a_county_only_state_uses_the_county_the_row_names_and_never_one_county_for_the_state(
    config: Config, conn: sqlite3.Connection
) -> None:
    add_court_row(conn, 1, state="TX", details="Charged in Harris County state court", year=2014)
    add_court_row(
        conn, 2, state="TX", details="Charged in a Texas state court", year=2014, sha="2" * 64
    )
    add_court_row(
        conn, 3, state="TX", details="Charged in Bexar County state court", year=2014, sha="3" * 64
    )
    record_portal(conn, "TX", "assisted", "account: the entry point shows a login")
    sync.write(
        conn, "TX", mode="manual", portal=None, adapter_module=None, reason="x"
    )  # AG: no network
    named = process(config, conn, 1).summary_json
    assert named["court_status"] == "gated_portal"  # type: ignore[index]
    assert named["assisted"]["portal_url"] == "https://www.hcdistrictclerk.com/"  # type: ignore[index]
    assert named["assisted"]["platform"] == "county_clerks"  # type: ignore[index]  # not the "wants"
    for row_id in (2, 3):  # no county named, or one that was not probed
        other = process(config, conn, row_id).summary_json
        # not searched and not absent: a coverage gap, never a recorded negative
        assert other["court_status"] == "deferred_coverage" and not other["assisted"]  # type: ignore[index]
        assert any("county" in n for n in other["notes"])  # type: ignore[union-attr]


def test_the_gated_list_has_every_portal_probed_not_one_per_state(
    cli_config: Config,
    conn: sqlite3.Connection,
    rsps: responses.RequestsMock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import csv

    seeds = tmp_path / "portals.csv"
    seeds.write_text(
        "state,portal,url,platform,scope,note\n"
        "FL,Miami,https://miami.example/,county_clerks,county:Miami,\n"
        "FL,Broward,https://broward.example/,county_clerks,county:Broward,\n"
        "TX,Dallas,https://dallas.example/,county_clerks,county:Dallas,\n"
    )
    monkeypatch.setattr(dp, "seeds_path", lambda: seeds)
    rsps.get("https://miami.example/", body="<html></html>")  # nothing visible
    rsps.get("https://broward.example/", body="<p>subscription required</p>")  # fee wording
    rsps.get("https://dallas.example/", status=404)
    result = _invoke("court", "docket", "probe")
    assert result.exit_code == 0, result.output
    listed = list(
        csv.DictReader((Path(cli_config.paths.data_dir) / "queues" / "portal_accounts.csv").open())
    )
    assert {r["portal"] for r in listed} == {"Miami", "Broward", "Dallas"}
    assert {r["wants"] for r in listed if r["portal"] == "Broward"} == {"fee"}


def test_a_needs_js_adapter_goes_through_the_browser_capture_and_its_hit_is_kept(
    config: Config,
    conn: sqlite3.Connection,
    rsps: responses.RequestsMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from efp.capture import Capture

    cfg = cleared(config, "WI")
    add_court_row(conn, 1, state="WI", details=STATE_COURT, year=2014)
    record_portal(conn, "WI", "automated")
    one = json.loads((CASSETTES / "hits.json").read_text())
    one["result"]["cases"] = one["result"]["cases"][:1]
    rsps.post(WI_SEARCH, json=one)
    monkeypatch.setattr(wi_mod.Wisconsin, "needs_js", True)
    seen: list[str] = []

    def browser(self: Capture, url: str, *, row_id: int | None, stage: str) -> Any:
        seen.append(url)  # a WACZ has no HTTP status; its tool is the success
        return self.store_bytes(
            b"wacz", row_id=row_id, stage=stage, url_requested=url, tool="browsertrix"
        )

    monkeypatch.setattr(Capture, "browser", browser)
    summary = process(cfg, conn, 1).summary_json
    assert seen and summary["court_status"] == "primary_record"  # type: ignore[index]


def test_a_hit_rests_on_the_search_answer_with_the_court_in_its_identity(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    cfg = cleared(config, "WI")
    add_court_row(conn, 1, state="WI", details=STATE_COURT, year=2014)
    record_portal(conn, "WI", "automated")
    one = json.loads((CASSETTES / "hits.json").read_text())
    one["result"]["cases"] = one["result"]["cases"][:1]
    rsps.post(WI_SEARCH, json=one)
    rsps.get("https://wcca.wicourts.gov/caseDetail.html", body="<html>detail</html>")
    process(cfg, conn, 1)
    capture_id, basis = conn.execute("SELECT capture_id, basis_json FROM court_records").fetchone()
    posted = conn.execute(
        "SELECT capture_id FROM captures WHERE url_requested LIKE '%advancedCaseSearch%'"
    ).fetchone()[0]
    assert capture_id == posted  # the fields came from the search answer, not the detail page
    detail = json.loads(basis)
    assert "Dane County" in detail["release_uuid"] and detail["detail_capture"] != posted


def test_the_browser_gets_the_project_user_agent_and_a_timed_out_container_is_removed(
    config: Config, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.dbhelpers import insert_row

    insert_row(conn, 1)
    conn.commit()
    cap = make_capture(config, conn)
    calls: list[list[str]] = []
    monkeypatch.setattr("shutil.which", lambda _n: "/usr/bin/docker")

    def fake_run(args: list[str], **_kw: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        if args[1] == "info":
            return subprocess.CompletedProcess(args, 0, b"", b"")
        if args[1] == "run":
            raise subprocess.TimeoutExpired(args, 1)
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    try:
        with pytest.raises(CaptureError, match="timed out"):
            cap.browser("https://example.test/search", row_id=1, stage="court")
    finally:
        cap.close()
    crawl = next(c for c in calls if c[1] == "run")
    assert crawl[crawl.index("--userAgent") + 1] == config.project.user_agent
    assert any(c[1:3] == ["rm", "-f"] for c in calls)  # the container is removed by name


def test_the_probe_heuristics_ignore_a_stray_price_and_a_script_handler_link() -> None:
    base = "https://portal.example/"
    assert not dp.analyse("<p>Parking is $5 at the garage.</p>", 200, base).fee_wall
    assert dp.analyse("<p>$25 per search</p>", 200, base).fee_wall
    page = '<a href="javascript:void(0)">Terms of Use</a><a href="/real-terms">Terms</a>'
    assert dp.analyse(page, 200, base).terms_url == "https://portal.example/real-terms"


def test_the_cleared_list_is_case_insensitive(write_config: Callable[..., Path]) -> None:
    from efp.config import load_config

    cfg = load_config(
        write_config({"docket_terms_cleared = []": 'docket_terms_cleared = [" wi "]'})
    )
    assert cfg.court.docket_terms_cleared == ["WI"]


def test_reprobing_one_state_keeps_every_other_states_rows_in_the_gated_list(
    cli_config: Config,
    conn: sqlite3.Connection,
    rsps: responses.RequestsMock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import csv

    seeds = tmp_path / "portals.csv"
    seeds.write_text(
        "state,portal,url,platform,scope,note\n"
        "FL,Broward,https://broward.example/,county_clerks,county:Broward,\n"
        "TX,Dallas,https://dallas.example/,county_clerks,county:Dallas,\n"
    )
    monkeypatch.setattr(dp, "seeds_path", lambda: seeds)
    rsps.get("https://broward.example/", body="<p>subscription required</p>")
    rsps.get("https://dallas.example/", status=404)
    assert _invoke("court", "docket", "probe").exit_code == 0
    queue = Path(cli_config.paths.data_dir) / "queues" / "portal_accounts.csv"
    assert {r["state"] for r in csv.DictReader(queue.open())} == {"FL", "TX"}
    seeds.write_text(  # the FL portal's address was corrected: a fresh page, not the cached one
        "state,portal,url,platform,scope,note\n"
        "FL,Broward,https://broward2.example/,county_clerks,county:Broward,\n"
        "TX,Dallas,https://dallas.example/,county_clerks,county:Dallas,\n"
    )
    rsps.get("https://broward2.example/", body="<html></html>")
    assert _invoke("court", "docket", "probe", "--state", "FL").exit_code == 0
    rows = list(csv.DictReader(queue.open()))
    assert {r["state"] for r in rows} == {"FL", "TX"}  # TX was not erased
    assert [r["wants"] for r in rows if r["state"] == "FL"] == ["search-not-found"]  # FL refreshed


def _docker(monkeypatch: pytest.MonkeyPatch, calls: list[list[str]], big: int = 0) -> None:
    monkeypatch.setattr("shutil.which", lambda _n: "/usr/bin/docker")

    def fake_run(args: list[str], **_kw: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        if args[1] == "image":
            return subprocess.CompletedProcess(args, 0, b"sha256:abc123def456\n", b"")
        if args[1] == "run":
            target = Path(args[args.index("-v") + 1].split(":")[0]) / "collections" / "efp"
            target.mkdir(parents=True)
            (target / "efp.wacz").write_bytes(b"PK" + b"x" * big)
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)


def test_an_earlier_wacz_is_reused_not_fetched_again_and_the_image_is_recorded(
    config: Config, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.dbhelpers import insert_row

    insert_row(conn, 1)
    conn.commit()
    cap = make_capture(config, conn)
    calls: list[list[str]] = []
    _docker(monkeypatch, calls)
    try:
        first = cap.browser("https://example.test/search", row_id=1, stage="court")
        crawls = len([c for c in calls if c[1] == "run"])
        again = cap.browser("https://example.test/search", row_id=1, stage="court")
        assert len([c for c in calls if c[1] == "run"]) == crawls == 1  # no second crawl
        assert again.capture_id != first.capture_id and again.from_cache  # but a row says so
        assert again.content_sha256 == first.content_sha256
        row = conn.execute(
            "SELECT tool_version, headers_json FROM captures WHERE capture_id = ?",
            (first.capture_id,),
        ).fetchone()
        headers = json.loads(row[1])
        assert (
            row[0] == "test" and headers["image_id"] == "sha256:abc123def456"
        )  # the tag, not efp's
        cap.browser("https://example.test/search", row_id=1, stage="court", refresh=True)
        assert len([c for c in calls if c[1] == "run"]) == 2  # refresh asks again
    finally:
        cap.close()


def test_an_unpinned_browser_image_and_an_oversized_wacz_are_refused(
    config: Config, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.dbhelpers import insert_row

    insert_row(conn, 1)
    conn.commit()
    latest = config.model_copy(
        update={"court": config.court.model_copy(update={"docket_browser_image": "x/y:latest"})}
    )
    cap = make_capture(latest, conn)
    try:
        with pytest.raises(CaptureError, match="not pinned"):
            cap.browser("https://example.test/s", row_id=1, stage="court")
    finally:
        cap.close()
    cap2 = make_capture(config, conn)
    _docker(monkeypatch, [], big=64)
    monkeypatch.setattr("efp.capture.MAX_WACZ_BYTES", 10)
    try:
        with pytest.raises(CaptureError, match="larger than"):
            cap2.browser("https://example.test/s", row_id=1, stage="court")
    finally:
        cap2.close()


# --- review: a common name is capped, a truncated answer is a candidate, a transient keeps the mode ------


def _many(n: int) -> dict[str, Any]:
    return {
        "result": {
            "cases": [
                {
                    "caseNo": f"2014CF{i:06d}",
                    "caption": "State of Wisconsin vs. Alex Example",
                    "countyName": "Dane",
                    "countyNo": i,
                    "filingDate": "2014-03-05",
                }
                for i in range(n)
            ]
        }
    }


def test_a_common_name_records_at_most_the_cap_from_the_search_answer_and_fetches_nothing_more(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    cfg = cleared(config, "WI")  # the fixture's docket_max_hits is 3
    add_court_row(conn, 1, state="WI", details=STATE_COURT, year=2014)
    record_portal(conn, "WI", "automated")
    rsps.post(WI_SEARCH, json=_many(12))
    summary = process(cfg, conn, 1).summary_json
    assert not [c for c in rsps.calls if "caseDetail" in c.request.url]  # no detail page, no PDF
    assert conn.execute("SELECT count(*) FROM court_records").fetchone()[0] == 3
    assert conn.execute("SELECT count(*) FROM court_records WHERE weak = 0").fetchone()[0] == 0
    assert summary["court_status"] == "candidate_only"  # type: ignore[index]
    flags = summary["flags"]["state_docket"]  # type: ignore[index]
    assert flags["matched"] == 12 and flags["over_cap"] is True
    assert any("a common name" in n for n in summary["notes"])  # type: ignore[union-attr]


def test_an_answer_that_says_it_has_more_is_never_treated_as_complete(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    cfg = cleared(config, "WI")
    add_court_row(conn, 1, state="WI", details=STATE_COURT, year=2014)
    record_portal(conn, "WI", "automated")
    one = _many(1)
    one["result"]["totalCount"] = 40  # more cases than came back; no pagination is implemented
    rsps.post(WI_SEARCH, json=one)
    rsps.get("https://wcca.wicourts.gov/caseDetail.html", body="<html>detail</html>")
    summary = process(cfg, conn, 1).summary_json
    assert summary["court_status"] == "candidate_only"  # one hit, but possibly not the only one
    assert summary["flags"]["state_docket"]["truncated"] is True  # type: ignore[index]
    assert wi_mod.result_truncated({"result": {"cases": [], "hasMore": True}})
    assert not wi_mod.result_truncated({"result": {"cases": [{}], "totalCount": 1}})


def test_a_transient_failure_of_a_states_portal_keeps_the_recorded_mode(
    cli_config: Config,
    conn: sqlite3.Connection,
    rsps: responses.RequestsMock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seeds = tmp_path / "portals.csv"
    seeds.write_text(
        "state,portal,url,platform,scope,note\n"
        "FL,Statewide,https://statewide.example/,other,statewide,\n"
        "FL,Broward,https://broward.example/,county_clerks,county:Broward,\n"
    )
    monkeypatch.setattr(dp, "seeds_path", lambda: seeds)
    record_portal(conn, "FL", "assisted", "challenge: previously recorded")
    rsps.get("https://statewide.example/", status=503)  # transient: not judged
    rsps.get("https://broward.example/", status=404)  # would win by default and read manual
    result = _invoke("court", "docket", "probe")
    assert result.exit_code == 0, result.output
    assert "a portal was not judged; the recorded mode is kept" in result.output
    assert sync.recorded(conn, "FL", "docket") == ("assisted", "challenge: previously recorded")


def test_a_surname_only_row_is_assisted_and_no_search_is_sent_to_the_portal(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    cfg = cleared(config, "WI")
    add_court_row(conn, 1, state="WI", details=STATE_COURT, year=2014, name="Example")
    record_portal(conn, "WI", "automated")
    summary = process(cfg, conn, 1).summary_json
    assert not [c for c in rsps.calls if "wicourts" in c.request.url]  # no empty exact match sent
    assert summary["court_status"] == "gated_portal" and summary["assisted"]["name"] == "Example"  # type: ignore[index]
    assert any("no given name" in n for n in summary["notes"])  # type: ignore[union-attr]
    ctx, cap, _ = make_ctx(cfg, conn)
    try:
        assert adapters()["WI"](ctx).search("Example", 2014) == []  # and the adapter refuses too
    finally:
        cap.close()


def test_a_probe_does_not_promote_a_portal_that_refused_automated_searches_unless_forced(
    cli_config: Config,
    conn: sqlite3.Connection,
    rsps: responses.RequestsMock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seeds = tmp_path / "portals.csv"
    seeds.write_text(
        "state,portal,url,platform,scope,note\nWI,WCCA,https://wcca.example/,wcca,statewide,\n"
    )
    monkeypatch.setattr(dp, "seeds_path", lambda: seeds)
    cfg_path = Path(cli_config.paths.data_dir).parent / "config.toml"
    cfg_path.write_text(
        cfg_path.read_text().replace("docket_terms_cleared = []", 'docket_terms_cleared = ["WI"]')
    )
    record_portal(conn, "WI", "assisted", "blocked: HTTP 403 (3 refusals in one run)")
    rsps.get("https://wcca.example/", body="<form><input name='lastName'></form>")
    result = _invoke("court", "docket", "probe")
    assert "kept assisted" in result.output
    assert (sync.recorded(conn, "WI", "docket") or ("",))[0] == "assisted"  # not flipped back
    assert _invoke("court", "docket", "probe", "--force").exit_code == 0
    assert (sync.recorded(conn, "WI", "docket") or ("",))[
        0
    ] == "automated"  # the operator re-cleared


def test_the_image_tag_is_read_from_the_name_not_from_a_digest() -> None:
    from efp.capture import image_tag

    assert image_tag("webrecorder/browsertrix-crawler:1.13.2") == "1.13.2"
    assert image_tag("registry:5000/bt:1.2@sha256:abc") == "1.2"
    assert image_tag("repo/name@sha256:abcdef") == ""  # digest only: no tag to record
    assert image_tag("repo/name") == ""


def test_a_county_only_state_with_an_account_portal_keeps_its_row_gated_without_a_county(
    config: Config, conn: sqlite3.Connection
) -> None:
    sync.write(
        conn, "TX", mode="manual", portal=None, adapter_module=None, reason="x"
    )  # AG: offline
    add_court_row(conn, 1, state="TX", details="Charged in a Texas state court", year=2014)
    record_portal(conn, "TX", "manual", "account: a login is required")
    summary = process(config, conn, 1).summary_json
    assert summary["court_status"] == "gated_portal"  # type: ignore[index]  # not deferred_coverage


def test_a_truncated_answer_with_no_matching_hit_is_a_gap_with_a_query_never_not_found(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    cfg = cleared(config, "WI")
    add_court_row(conn, 1, state="WI", details=STATE_COURT, year=2014)
    record_portal(conn, "WI", "automated")
    partial = {
        "result": {
            "cases": [
                {
                    "caseNo": "2014CF000001",
                    "caption": "State v. Someone Else",
                    "countyNo": 1,
                    "countyName": "Dane",
                    "filingDate": "2014-03-05",
                }
            ],
            "hasMore": True,
        }
    }
    rsps.post(WI_SEARCH, json=partial)
    result = process(cfg, conn, 1)
    summary = result.summary_json
    assert summary["court_status"] == "deferred_coverage"  # type: ignore[index]  # not not_found
    assert summary["assisted"]["name"] == "Example, Alex"  # type: ignore[index]  # the operator can finish it
    assert result.input_hash and result.input_hash.endswith(":coverage")


def test_a_gated_docket_portal_keeps_the_row_waiting_even_when_a_press_release_was_found() -> None:
    from efp.court.located import Located

    release = Located(
        "doj_release", "US", None, None, "press_release", "u", "court.doj", None, weak=False
    )
    assert component.court_status([release]) == "press_release_only"
    assert component.court_status([release], gated_portal=True) == "gated_portal"
    docket = Located("state_docket", "WI", "c", "n", "docket", "u", "court.state_docket", None)
    assert component.court_status([release, docket], gated_portal=True) == "primary_record"
    assert component.court_status([release], gated_portal=True, errored=True) == "deferred_error"


def test_portal_supplied_values_are_quoted_in_the_detail_url() -> None:
    hits = wi_mod.parse_cases(
        {"result": {"cases": [{"caseNo": "20&14#CF 1", "caption": "c", "countyNo": "1&x=2"}]}}
    )
    assert hits[0].url == (
        "https://wcca.wicourts.gov/caseDetail.html?caseNo=20%2614%23CF%201&countyNo=1%26x%3D2"
    )


def test_the_docket_text_capture_is_marked_as_derived_from_the_search_answer(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    cfg = cleared(config, "WI")
    add_court_row(conn, 1, state="WI", details=STATE_COURT, year=2014)
    record_portal(conn, "WI", "automated")
    one = json.loads((CASSETTES / "hits.json").read_text())
    one["result"]["cases"] = one["result"]["cases"][:1]
    rsps.post(WI_SEARCH, json=one)
    rsps.get("https://wcca.wicourts.gov/caseDetail.html", body="<html>detail</html>")
    process(cfg, conn, 1)
    posted = conn.execute(
        "SELECT capture_id FROM captures WHERE url_requested LIKE '%advancedCaseSearch%'"
    ).fetchone()[0]
    headers = json.loads(
        conn.execute(
            "SELECT headers_json FROM captures WHERE tool = 'state_docket_row'"
        ).fetchone()[0]
    )
    assert headers["derived"] is True and headers["derived_from_capture_id"] == posted


def test_a_portal_the_seed_marks_human_check_is_assisted_even_with_an_adapter_and_clearance() -> (
    None
):
    seed = dp.Seed("WI", "WCCA", "u", "wcca", "statewide", "human-check: gated by a CAPTCHA")
    open_page = dp.Facts(status=200, search_form=True)
    verdict = dp.classify(seed, open_page, adapter_exists=True, cleared=True)
    assert verdict == ("assisted", "challenge", "gated by a CAPTCHA")  # never automated
    wi = next(s for s in dp.load_seeds() if s.state == "WI")
    assert wi.note.startswith("human-check:")  # the committed seed list carries the finding


def test_an_unreachable_manual_portal_is_a_gap_and_only_a_verified_no_online_search_is_not_found(
    config: Config, conn: sqlite3.Connection
) -> None:
    add_court_row(conn, 1, state="WI", details=STATE_COURT)
    record_portal(conn, "WI", "manual", "unreachable: the entry point answered HTTP 404")
    gap = process(config, conn, 1)
    assert gap.summary_json["court_status"] == "deferred_coverage"  # type: ignore[index]
    assert gap.input_hash and gap.input_hash.endswith(
        ":coverage"
    )  # revisited when the seed is fixed
    record_portal(conn, "WI", "manual", "no-online-search: county clerk by phone")
    assert process(config, conn, 1).summary_json["court_status"] == "not_found"  # type: ignore[index]

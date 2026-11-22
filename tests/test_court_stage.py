from __future__ import annotations

import csv
import json
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import responses
import stamina

import efp.court as component
from efp import status as status_view
from efp.config import Config
from efp.court import bulk, courtlistener, pin, recap
from efp.court.located import CourtTask, Located
from efp.court.names import parse_name
from efp.modules import Availability, ModuleResult, Outcome, Registry
from efp.runner import make_context
from tests.capturehelpers import make_capture
from tests.courthelpers import (
    HOSTS,
    add_court_row,
    court_config,
    doj_page,
    make_ctx,
    process,
    release_html,
    row_of,
    write_bulk_files,
)

SEARCH = "https://www.courtlistener.com/api/rest/v4/search/"
PDF_URL = "https://storage.courtlistener.com/recap/gov.uscourts.ctd.1/gov.uscourts.ctd.1.1.0.pdf"


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


def _docket(**extra: Any) -> dict[str, Any]:
    item = {
        "docket_id": 4242,
        "docketNumber": "3:13-cr-00045",
        "caseName": "United States v. Example",
        "court": "District Court, D. Connecticut",
        "dateFiled": "2013-06-01",
        "dateTerminated": "2015-02-01",
        "docket_absolute_url": "/docket/4242/united-states-v-example/",
        "party": ["Alex Example"],
        "recap_documents": [],
    }
    item.update(extra)
    return item


def _index_without(config: Config) -> None:
    """A loaded bulk index (the API only runs once there is one: bulk before API)."""
    write = write_bulk_files(config.paths.bulk / "courtlistener")
    bulk.load_index(config, write, dataset_date="2026-09-30", manifest_captures={})


def _quiet_doj(rsps: responses.RequestsMock) -> None:
    rsps.get("https://www.justice.gov/api/v1/press_releases.json", json=doj_page())


def _by_param(rsps: responses.RequestsMock, key: str, payload: dict[str, Any]) -> None:
    rsps.get(
        SEARCH,
        json=payload,
        match=[
            responses.matchers.query_param_matcher({key: ""}, strict_match=False)
            if False
            else responses.matchers.query_string_matcher(None)
            if False
            else _has_param(key)
        ],
    )


def _has_param(key: str):  # type: ignore[no-untyped-def]
    def match(request):  # type: ignore[no-untyped-def]
        from urllib.parse import parse_qs, urlsplit

        ok = key in parse_qs(urlsplit(request.url).query)
        return ok, "" if ok else f"no {key} parameter"

    return match


def _modules(summary: dict[str, Any]) -> dict[str, str]:
    return {m["name"]: m["status"] for m in summary["modules"]}


# --- the REST API: only when the index misses, only for federal rows ------------------------------


def test_the_api_finds_a_docket_the_index_lacks_and_captures_the_filings_already_in_recap(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    _index_without(config)  # the index is loaded but does not contain this subject
    add_court_row(conn, 1, name="Quinn Absent", details="Charged in U.S. District Court")
    _quiet_doj(rsps)
    docs = [
        {
            "id": 9,
            "description": "Indictment",
            "filepath_local": PDF_URL.removeprefix("https://storage.courtlistener.com/"),
            "is_available": True,
        }
    ]
    mine = _docket(recap_documents=docs, party=["Quinn Absent"], caseName="United States v. Absent")
    rsps.get(SEARCH, json={"results": [mine]}, match=[_has_param("party_name")])
    rsps.get(SEARCH, json={"results": [mine]}, match=[_has_param("docket_id")])
    rsps.get("https://storage.courtlistener.com/robots.txt", status=404)
    rsps.get(PDF_URL, body=b"%PDF-1.7 synthetic indictment", content_type="application/pdf")
    result = process(config, conn, 1)
    summary = result.summary_json
    assert summary["court_status"] == "primary_record"
    assert _modules(summary)["court.courtlistener_bulk"] == "miss"  # the index lacks this subject
    assert _modules(summary)["court.courtlistener_api"] == "hit"
    kinds = conn.execute(
        "SELECT source_kind, document_kind, strategy FROM court_records ORDER BY record_id"
    ).fetchall()
    assert kinds == [
        ("federal_docket", "docket", "court.courtlistener_api"),
        ("federal_docket", "filing", "court.courtlistener_api"),
    ]
    pdf = conn.execute(
        "SELECT content_type, http_status FROM captures WHERE url_requested = ?", (PDF_URL,)
    ).fetchone()
    assert pdf == ("application/pdf", 200)  # the PDF is captured for build 10
    assert summary["pacer_queued"] == 0  # documents exist, so nothing to queue


def test_an_unknown_row_never_spends_the_api_budget(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    _index_without(config)
    add_court_row(conn, 1, name="Quinn Absent", details="Voted twice")  # no federal or state signal
    _quiet_doj(rsps)
    summary = process(config, conn, 1).summary_json
    assert "only rows classified federal" in _reason(summary, "court.courtlistener_api")
    assert not [c for c in rsps.calls if "courtlistener.com/api" in c.request.url]
    assert summary["classification"]["kind"] == "unknown"  # type: ignore[index]


def test_a_state_row_never_runs_a_federal_strategy(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    add_court_row(conn, 1, details="Pleaded guilty in Hartford Superior Court")
    summary = process(config, conn, 1).summary_json
    assert (
        summary["classification"]["kind"] == "state"
        and summary["federal_strategies_applied"] is False
    )  # type: ignore[index]
    assert "classified as a state case" in _reason(summary, "court.doj")
    assert "classified as a state case" in _reason(
        summary, "court.courtlistener_bulk"
    ) or "not loaded" in _reason(summary, "court.courtlistener_bulk")
    assert "not built until build 08" in _reason(summary, "court.state_ag")
    assert "not built until build 09" in _reason(summary, "court.state_docket")
    assert summary["court_status"] == "not_found" and not rsps.calls


def _reason(summary: dict[str, Any], name: str) -> str:
    return next(m["reason"] for m in summary["modules"] if m["name"] == name)


def test_a_spent_daily_budget_is_a_note_and_the_row_still_completes(
    write_config: Callable[..., Path],
    conn: sqlite3.Connection,
    rsps: responses.RequestsMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = court_config(
        write_config, **{'"example.test" = 10': '"example.test" = 10\n"www.courtlistener.com" = 1'}
    )
    from efp.db import connect

    conn = connect(config)
    _index_without(config)
    monkeypatch.setattr(courtlistener, "QUOTA_WAIT", 0.3)  # keep the test quick
    add_court_row(conn, 1, name="Quinn Absent", details="U.S. District Court")
    add_court_row(conn, 2, name="Pat Absent", details="U.S. District Court", sha="2" * 64)
    _quiet_doj(rsps)
    rsps.get(SEARCH, json={"results": []})
    first = process(config, conn, 1)
    second = process(config, conn, 2)
    assert first.status == "done" and second.status == "done"
    assert second.summary_json["flags"]["api_quota_exhausted"] is True  # type: ignore[index]
    assert any("daily budget spent" in n for n in second.summary_json["notes"])  # type: ignore[union-attr]
    assert len([c for c in rsps.calls if "courtlistener.com/api" in c.request.url]) == 1
    conn.close()


def test_the_api_token_is_never_stored(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock, env_file: Path
) -> None:
    _index_without(config)
    add_court_row(conn, 1, name="Quinn Absent", details="U.S. District Court")
    _quiet_doj(rsps)
    rsps.get(SEARCH, json={"results": [_docket(party=["Quinn Absent"], caseName="USA v. Absent")]})
    rsps.get("https://www.courtlistener.com/robots.txt", status=404)
    process(config, conn, 1)
    secret = "SENTINEL-courtlistener-5b1d9c"  # pragma: allowlist secret (synthetic test value)
    stored = " ".join(str(v) for row in conn.execute("SELECT * FROM captures") for v in row)
    assert secret not in stored
    assert secret in rsps.calls[-1].request.headers["Authorization"] or any(
        secret in c.request.headers.get("Authorization", "") for c in rsps.calls
    )


# --- RECAP and PACER are gated ---------------------------------------------------------------------


def test_a_docket_with_no_documents_in_recap_goes_to_the_pacer_queue_once(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    write = write_bulk_files(config.paths.bulk / "courtlistener")
    bulk.load_index(config, write, dataset_date="2026-09-30", manifest_captures={})
    add_court_row(conn, 1, details="U.S. District Court")
    _quiet_doj(rsps)
    rsps.get(SEARCH, json={"results": [_docket(docket_id=1)]})  # the docket exists; no filings
    result = process(config, conn, 1)
    assert result.summary_json["pacer_queued"] == 1
    queue = Path(config.court.pacer_queue)
    rows = list(csv.reader(queue.open(encoding="utf-8")))
    assert rows[0] == ["row_id", "court", "case_number", "docket_id", "missing"]
    assert rows[1][:4] == ["1", "District Court, D. Connecticut", "3:13-cr-00045", "1"]
    assert "RECAP" in rows[1][4]
    again = process(config, conn, 1)  # re-running does not list it twice
    assert again.summary_json["pacer_queued"] == 0 and len(list(csv.reader(queue.open()))) == 2


def test_documents_are_not_checked_for_a_row_that_is_not_positively_federal(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    write = write_bulk_files(config.paths.bulk / "courtlistener")
    bulk.load_index(config, write, dataset_date="2026-09-30", manifest_captures={})
    add_court_row(conn, 1)  # unknown classification
    _quiet_doj(rsps)
    summary = process(config, conn, 1).summary_json
    assert summary["court_status"] == "primary_record"
    assert not [c for c in rsps.calls if "courtlistener.com/api" in c.request.url]
    assert "not positively federal" in json.dumps(summary["recap"])
    with Path(config.court.pacer_queue).open() as handle:
        queued = list(csv.reader(handle))
    assert "not positively federal" in queued[1][4]


def test_recap_fetch_is_gated_and_never_calls_pacer(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    ctx, cap, registry = make_ctx(config, conn)
    module = registry.get("court.recap")
    assert module is not None
    # the test environment's env file already holds PACER credentials: they alone do not enable it
    assert recap.pacer_credentials(ctx) is not None
    off = module.available(ctx)
    assert not off.ok and "court.recap_enabled = false" in off.reason
    assert "never fetches from PACER" in off.reason
    cap.close()
    assert not [c for c in rsps.calls if "recap-fetch" in c.request.url]


def test_recap_fetch_needs_both_the_switch_and_credentials(
    write_config: Callable[..., Path], conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    on = court_config(write_config, **{"recap_enabled = false": "recap_enabled = true"})
    ctx, cap, registry = make_ctx(on, conn)
    module = registry.get("court.recap")
    assert module is not None
    assert module.available(ctx).ok  # the switch is on and the environment has credentials
    monkeypatch.setattr(recap, "pacer_credentials", lambda _ctx: None)
    assert "PACER_USER" in module.available(ctx).reason  # the switch is on, credentials are absent
    cap.close()


def test_the_pacer_queue_appends_without_repeating(config: Config) -> None:
    items = [
        recap.QueueItem(1, "ctd", "3:13-cr-1", 5, "no documents"),
        recap.QueueItem(2, "ohnd", "1:12-cr-2", None, "no documents"),
    ]
    assert recap.append_queue(config, items) == 2
    assert recap.append_queue(config, items) == 0
    assert recap.append_queue(config, []) == 0
    assert len(list(csv.reader(recap.queue_path(config).open()))) == 3


# --- the chain: order, stopping, completion --------------------------------------------------------


class Fake:
    stage = "court"

    def __init__(
        self, name: str, outcome: Outcome, calls: list[str], *, case: str | None = None
    ) -> None:
        self.name, self._outcome, self._calls, self._case = name, outcome, calls, case

    def available(self, ctx):  # type: ignore[no-untyped-def]
        return Availability.available()

    def applies(self, row, ctx):  # type: ignore[no-untyped-def]
        return True

    def run(self, row, ctx):  # type: ignore[no-untyped-def]
        self._calls.append(self.name)
        task: CourtTask = row
        task.cs.located.append(
            Located("doj_release", "US", None, self._case, "press_release", None, self.name, None)
        )
        return ModuleResult(self._outcome)


def test_the_chain_runs_in_config_order_and_stops_at_the_first_case_number(
    config: Config, conn: sqlite3.Connection
) -> None:
    add_court_row(conn, 1, details="U.S. District Court")
    calls: list[str] = []
    registry = Registry()
    names = [
        "court.doj",
        "court.pin_reports",
        "court.state_ag",
        "court.courtlistener_bulk",
        "court.courtlistener_api",
        "court.recap",
        "court.state_docket",
    ]
    plan = {
        "court.doj": (Outcome.miss, None),  # located, no case number: the chain goes on
        "court.pin_reports": (Outcome.miss, None),
        "court.state_ag": (Outcome.miss, None),
        "court.courtlistener_bulk": (Outcome.hit, "3:13-cr-00045"),  # the first case number
    }
    for name in names:
        outcome, case = plan.get(name, (Outcome.hit, "never reached"))
        registry.register(Fake(name, outcome, calls, case=case))
    ctx = make_context(config, conn)
    result = component.process_row(row_of(conn, 1), ctx, registry)
    assert calls == names[:4]  # nothing after the hit ran
    assert [m["name"] for m in result.summary_json["modules"]] == names[:4]
    stored = conn.execute("SELECT strategy FROM court_records ORDER BY record_id").fetchall()
    assert [r[0] for r in stored] == names[:4]  # strategy names the module that found each document


def test_a_row_nothing_resolves_completes_as_not_found(
    config: Config, conn: sqlite3.Connection
) -> None:
    add_court_row(conn, 1)
    registry = Registry()
    for name in [
        "court.doj",
        "court.pin_reports",
        "court.state_ag",
        "court.courtlistener_bulk",
        "court.courtlistener_api",
        "court.recap",
        "court.state_docket",
    ]:
        registry.register(_Silent(name))
    result = component.process_row(row_of(conn, 1), make_context(config, conn), registry)
    assert (result.status, result.summary_json["court_status"]) == ("done", "not_found")


class _Silent(Fake):
    def __init__(self, name: str) -> None:
        super().__init__(name, Outcome.miss, [])

    def run(self, row, ctx):  # type: ignore[no-untyped-def]
        return ModuleResult(Outcome.miss)


def test_court_status_values() -> None:
    docket = Located("federal_docket", None, None, "1:19-cr-1", "docket", None, "x", None)
    release = Located("doj_release", None, None, None, "press_release", None, "x", None)
    assert component.court_status([release, docket]) == "primary_record"
    assert component.court_status([release]) == "press_release_only"
    assert component.court_status([]) == "not_found"


def test_re_running_does_not_duplicate_records_or_identifiers(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    write = write_bulk_files(config.paths.bulk / "courtlistener")
    bulk.load_index(config, write, dataset_date="2026-09-30", manifest_captures={})
    add_court_row(conn, 1)
    _quiet_doj(rsps)
    process(config, conn, 1)
    counts = [
        conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        for t in ("court_records", "identifiers")
    ]
    again = process(config, conn, 1)
    assert [
        conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        for t in ("court_records", "identifiers")
    ] == counts
    assert again.status == "done" and counts[0] >= 1


def test_the_input_hash_changes_with_the_bulk_dataset_and_the_modules(
    config: Config, conn: sqlite3.Connection
) -> None:
    add_court_row(conn, 1)
    ctx, cap, registry = make_ctx(config, conn)
    row = row_of(conn, 1)
    before = component.input_hash(row, ctx, registry)
    assert component.input_hash(row, ctx, registry) == before
    write = write_bulk_files(config.paths.bulk / "courtlistener")
    bulk.load_index(config, write, dataset_date="2026-09-30", manifest_captures={})
    assert component.input_hash(row, ctx, registry) != before  # an index is now available
    cap.close()


# --- the status report --------------------------------------------------------------------------------


def test_status_reports_court_status_with_federal_and_state_apart(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    write = write_bulk_files(config.paths.bulk / "courtlistener")
    bulk.load_index(config, write, dataset_date="2026-09-30", manifest_captures={})
    add_court_row(conn, 1, details="U.S. District Court")  # a federal docket in the index
    add_court_row(conn, 2, name="Zed Nobody", details="Hartford Superior Court", sha="2" * 64)
    add_court_row(conn, 3, name="Yan Nobody", sha="3" * 64)
    _quiet_doj(rsps)
    rsps.get(SEARCH, json={"results": []})
    for row_id in (1, 2, 3):
        result = process(config, conn, row_id)
        conn.execute(
            "UPDATE case_stage SET status = 'done', summary_json = ? WHERE row_id = ? AND stage = 'court'",
            (json.dumps(result.summary_json), row_id),
        )
    conn.commit()
    report = status_view.build_report(conn, config)
    assert report.court["federal"] == {"primary_record": 1}
    assert report.court["state"] == {"not_found": 1} and report.court["unknown"] == {"not_found": 1}
    text = status_view.render_md(report)
    assert (
        "Court status (federal and state kept apart)" in text
        and "| federal | 1 | 0 | 0 | 0 |" in text
    )


# --- the PIN reports ---------------------------------------------------------------------------------------


def test_pin_reports_are_unavailable_without_urls_or_a_pdf_reader(
    config: Config,
    write_config: Callable[..., Path],
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, cap, registry = make_ctx(config, conn)
    module = registry.get("court.pin_reports")
    assert module is not None and "pin_report_urls is empty" in module.available(ctx).reason
    with_urls = court_config(
        write_config,
        **{"pin_report_urls = []": 'pin_report_urls = ["https://example.test/pin2010.pdf"]'},
    )
    monkeypatch.setattr(pin, "_backend", lambda: None)
    ctx2, cap2, _ = make_ctx(with_urls, conn)
    assert "pdfplumber or pypdf" in module.available(ctx2).reason
    cap.close()
    cap2.close()


def test_a_pre_2013_federal_row_matches_a_report_entry(
    write_config: Callable[..., Path],
    conn: sqlite3.Connection,
    rsps: responses.RequestsMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://example.test/pin2010.pdf"
    config = court_config(write_config, **{"pin_report_urls = []": f'pin_report_urls = ["{url}"]'})
    from efp.db import connect

    conn = connect(config)
    page = (
        "Introduction to the 2010 report.\n\n"
        "United States v. Alex Example, No. 3:08-cr-00045 (D. Conn.). The defendant, a former election "
        "worker, pleaded guilty to voting more than once in the District of Connecticut and was sentenced.\n\n"
        "United States v. Bob Other, No. 1:09-cr-00010 (S.D. Ohio). Another defendant."
    )
    monkeypatch.setattr(pin, "_backend", lambda: lambda pdf: [pdf.decode()])
    rsps.get("https://example.test/robots.txt", status=404)
    rsps.get(url, body=page.encode(), content_type="application/pdf")
    add_court_row(conn, 1, year=2010, details="U.S. District Court")
    rsps.get("https://www.justice.gov/api/v1/press_releases.json", json=doj_page())
    result = process(config, conn, 1)
    summary = result.summary_json
    assert _modules(summary)["court.pin_reports"] == "hit" and "court.doj" in _modules(summary)
    record = conn.execute(
        "SELECT source_kind, case_number, document_kind, strategy FROM court_records"
    ).fetchone()
    assert record == ("pin_report", "3:08-cr-00045", "report_entry", "court.pin_reports")
    assert "pin_report_text" in {r[0] for r in conn.execute("SELECT tool FROM captures")}
    assert not [c for c in rsps.calls if "press_releases" in c.request.url]  # pre-2013: no DOJ API
    conn.close()


def test_pin_paragraphs_and_index(tmp_path: Path) -> None:
    index = pin.PinIndex(tmp_path / "pin.sqlite")
    pages = [
        "Short.\n\nUnited States v. Alex Example, No. 3:08-cr-00045 (D. Conn.) pleaded guilty to voting.\n\nx"
    ]
    assert (
        index.index_pages("https://example.test/a.pdf", 1, pages) == 1
    )  # short blocks are dropped
    assert index.indexed("https://example.test/a.pdf")
    from efp.court.names import parse_name

    assert len(index.find(parse_name("Alex Example"))) == 1
    assert index.find(parse_name("Bob Example")) == []
    assert pin.paragraphs("a\nb c d e f g h i j k l m n o p q r s t u v w x y z 1 2 3 4") != []


def test_the_api_result_parser_tolerates_missing_fields() -> None:
    parsed = courtlistener.parse_dockets(
        {"results": [{"docket_id": "12", "caseName": "A v. B"}, "junk", {}]}
    )
    assert parsed[0].docket_id == 12 and parsed[0].docket_number == "" and len(parsed) == 2
    assert (
        courtlistener.parse_dockets(None) == []
        and courtlistener.parse_dockets({"results": None}) == []
    )


_ = (make_capture, HOSTS, release_html)


def test_the_runner_drives_the_court_component_and_a_second_run_is_a_no_op(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    from efp import runner as rn
    from efp.builtin_modules import register_builtin
    from tests.runnerhelpers import set_stage

    register_builtin()  # the CLI does this at startup; the component reads the global registry

    write = write_bulk_files(config.paths.bulk / "courtlistener")
    bulk.load_index(config, write, dataset_date="2026-09-30", manifest_captures={})
    add_court_row(conn, 1, details="U.S. District Court")
    add_court_row(conn, 2, name="Zed Nobody", sha="2" * 64)
    for row_id in (1, 2):
        conn.execute(
            "INSERT INTO case_stage (row_id, stage, status) VALUES (?, 'resolve_links', 'done')",
            (row_id,),
        )
    conn.commit()
    _quiet_doj(rsps)
    rsps.get(SEARCH, json={"results": []})
    ctx, cap, _registry = make_ctx(config, conn)
    first = rn.run_stage(conn, config, rn.RunOptions("court"), ctx, component=component)
    assert (first.claimed, first.done, first.failed) == (2, 2, 0)
    row = conn.execute(
        "SELECT status, input_hash, summary_json FROM case_stage WHERE row_id = 1 AND stage = 'court'"
    ).fetchone()
    assert row[0] == "done" and row[1] and json.loads(row[2])["court_status"] == "primary_record"
    assert rn.run_stage(conn, config, rn.RunOptions("court"), ctx, component=component).claimed == 0
    rechecked = rn.run_stage(
        conn, config, rn.RunOptions("court", recheck=True), ctx, component=component
    )
    assert rechecked.noop == 2 and rechecked.claimed == 0  # nothing about the inputs changed
    cap.close()
    _ = set_stage


def test_the_api_is_unavailable_until_the_bulk_index_is_loaded(
    config: Config, conn: sqlite3.Connection
) -> None:
    ctx, cap, registry = make_ctx(config, conn)
    module = registry.get("court.courtlistener_api")
    assert module is not None
    assert "bulk before API" in module.available(ctx).reason
    _index_without(config)
    assert module.available(ctx).ok  # the token is in the test environment
    cap.close()


def test_two_api_calls_in_a_row_both_get_their_turn_under_the_real_per_request_budget(
    write_config: Callable[..., Path], conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    """The wait for the budget must cover the gap between requests: a second call a moment after
    the first is a short wait, not "the daily quota is spent"."""
    from efp.capture import Capture
    from efp.db import connect
    from efp.ratelimit import DomainLimiter

    config = court_config(
        write_config,
        **{'"example.test" = 1.0': '"example.test" = 1.0\n"www.courtlistener.com" = 2.0'},
    )
    conn = connect(config)
    add_court_row(conn, 1)
    cap = Capture(config, conn, limiter=DomainLimiter(config))  # the real budget, not a fast one
    ctx = make_context(config, conn).model_copy(update={"capture": cap})
    rsps.get(SEARCH, json={"results": []})
    for name in ("Quinn Absent", "Pat Absent"):  # different queries, so the cache cannot answer
        # 2 requests a second: the second waits about half a second for its turn
        courtlistener.search_party(ctx, "tok", parse_name(name), state="CT", year=2014, row_id=1)
    cap.close()
    assert len([c for c in rsps.calls if "courtlistener.com/api" in c.request.url]) == 2

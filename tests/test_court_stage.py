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
    assert second.summary_json["court_status"] == "deferred_quota"  # type: ignore[index]
    assert second.input_hash and second.input_hash.endswith(":quota")  # --recheck revisits it
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
    # RECAP was never asked, so the docket is not "absent from RECAP": it is not queued to buy
    assert summary["pacer_queued"] == 0 and not Path(config.court.pacer_queue).exists()


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
    weak = Located("federal_docket", None, None, "1:19-cr-1", "docket", None, "x", None, weak=True)
    assert component.court_status([release, weak]) == "candidate_only"
    assert component.court_status([weak, docket]) == "primary_record"
    assert component.court_status([], quota_exhausted=True) == "deferred_quota"
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
    assert _modules(summary)["court.pin_reports"] == "miss"  # recorded, but a report entry is weak
    assert "court.courtlistener_bulk" in _modules(summary)  # so the chain went on
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


def test_pin_index_closes_its_connection(tmp_path: Path) -> None:
    index = pin.PinIndex(tmp_path / "pin.sqlite")
    index.close()
    with pytest.raises(sqlite3.ProgrammingError):
        index.count()


def test_the_pin_module_closes_its_index_after_every_row_even_on_an_error(
    write_config: Callable[..., Path],
    rsps: responses.RequestsMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://example.test/pin2010.pdf"
    config = court_config(write_config, **{"pin_report_urls = []": f'pin_report_urls = ["{url}"]'})
    from efp.db import connect

    conn = connect(config)
    opened: list[pin.PinIndex] = []
    original = pin.PinIndex.__init__

    def tracking(self: pin.PinIndex, path: Path) -> None:
        original(self, path)
        opened.append(self)

    monkeypatch.setattr(pin.PinIndex, "__init__", tracking)
    monkeypatch.setattr(pin, "_backend", lambda: lambda pdf: [pdf.decode()])
    rsps.get("https://example.test/robots.txt", status=404)
    rsps.get(url, body=b"Nothing relevant here at all, a long enough paragraph to be indexed ok.")
    add_court_row(conn, 1, year=2010, details="U.S. District Court")
    rsps.get("https://www.justice.gov/api/v1/press_releases.json", json=doj_page())
    process(config, conn, 1)
    assert opened
    for index in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            index.count()

    def boom(self: pin.PinIndex, parts: Any) -> Any:
        raise RuntimeError("index failure")

    monkeypatch.setattr(pin.PinIndex, "find", boom)
    add_court_row(conn, 2, year=2010, details="U.S. District Court", sha="2" * 64)
    opened.clear()
    summary = process(config, conn, 2).summary_json
    assert _modules(summary)["court.pin_reports"] == "error" and opened
    with pytest.raises(sqlite3.ProgrammingError):
        opened[-1].count()
    conn.close()


def test_recheck_revisits_a_row_that_was_deferred_for_the_api_quota_and_only_that_row(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    from efp import runner as rn
    from efp.builtin_modules import register_builtin

    register_builtin()
    _index_without(config)
    add_court_row(conn, 1, name="Quinn Absent", details="U.S. District Court")
    add_court_row(conn, 2, name="Pat Absent", details="U.S. District Court", sha="2" * 64)
    for row_id in (1, 2):
        conn.execute(
            "INSERT INTO case_stage (row_id, stage, status) VALUES (?, 'resolve_links', 'done')",
            (row_id,),
        )
    conn.commit()
    _quiet_doj(rsps)
    rsps.get(SEARCH, json={"results": []})
    ctx, cap, _registry = make_ctx(config, conn)
    rn.run_stage(conn, config, rn.RunOptions("court"), ctx, component=component)
    # pretend row 2's API lookup had been refused for the daily quota when it ran
    conn.execute(
        "UPDATE case_stage SET input_hash = input_hash || ':quota' WHERE row_id = 2 AND stage = 'court'"
    )
    conn.commit()
    again = rn.run_stage(
        conn, config, rn.RunOptions("court", recheck=True), ctx, component=component
    )
    cap.close()
    assert (again.noop, again.claimed) == (1, 1)  # row 1 unchanged; row 2 is tried again


# --- review round 2: errors, missing years, and the RECAP step's quota -----------------------------------


class _Raises(Fake):
    def run(self, row, ctx):  # type: ignore[no-untyped-def]
        raise RuntimeError("the module failed")


def test_a_module_that_raises_is_not_recorded_as_a_clean_not_found_and_is_retried(
    config: Config, conn: sqlite3.Connection
) -> None:
    add_court_row(conn, 1, details="U.S. District Court")
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
        registry.register(
            _Raises(name, Outcome.miss, []) if name == "court.courtlistener_bulk" else _Silent(name)
        )
    ctx = make_context(config, conn)
    result = component.process_row(row_of(conn, 1), ctx, registry)
    summary = result.summary_json
    assert summary["court_status"] == "deferred_error" and result.status == "done"
    assert any("the module failed" in e for e in summary["errors"])  # type: ignore[union-attr]
    assert result.input_hash and result.input_hash.endswith(":error")  # --recheck revisits it
    assert result.input_hash != component.input_hash(row_of(conn, 1), ctx, registry)


def test_an_error_after_something_was_found_keeps_the_finding_but_still_retries(
    config: Config, conn: sqlite3.Connection
) -> None:
    add_court_row(conn, 1)
    registry = Registry()
    calls: list[str] = []
    names = [
        "court.doj",
        "court.pin_reports",
        "court.state_ag",
        "court.courtlistener_bulk",
        "court.courtlistener_api",
        "court.recap",
        "court.state_docket",
    ]
    for name in names:
        if name == "court.doj":
            registry.register(Fake(name, Outcome.miss, calls, case=None))  # located, no case number
        elif name == "court.pin_reports":
            registry.register(_Raises(name, Outcome.miss, calls))
        else:
            registry.register(_Silent(name))
    result = component.process_row(row_of(conn, 1), make_context(config, conn), registry)
    # the release is recorded, but the search did not finish: not a finished press_release_only
    assert result.summary_json["court_status"] == "deferred_error"
    assert result.summary_json["located"]  # type: ignore[truthy-bool]
    assert result.input_hash and result.input_hash.endswith(":error")


def test_a_row_with_no_year_builds_no_invalid_dates_and_calls_nothing(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    from efp.court.names import parse_name
    from efp.types import Row

    write = write_bulk_files(config.paths.bulk / "courtlistener")
    bulk.load_index(config, write, dataset_date="2026-09-30", manifest_captures={})
    add_court_row(conn, 1, details="U.S. District Court")
    base = row_of(conn, 1)
    yearless = Row(row_id=1, year=None, state=base.state, source=base.source)
    ctx, cap, registry = make_ctx(config, conn)
    result = component.process_row(yearless, ctx, registry)
    cap.close()
    reasons = {m["name"]: m["reason"] for m in result.summary_json["modules"]}  # type: ignore[index, union-attr]
    for name in ("court.doj", "court.courtlistener_bulk", "court.courtlistener_api"):
        assert "no year" in reasons[name], name
    assert result.summary_json["court_status"] == "not_found" and not result.summary_json["errors"]
    assert not [c for c in rsps.calls if "courtlistener.com/api" in c.request.url]  # no quota spent
    assert bulk.search_dockets(config, parse_name("Alex Example"), state="CT", year=0) == []
    with pytest.raises(courtlistener.CaptureError, match="no year"):
        courtlistener.search_party(
            ctx, "t", parse_name("Alex Example"), state="CT", year=0, row_id=1
        )


def test_a_spent_budget_during_the_recap_check_is_not_hidden_or_queued_for_purchase(
    config: Config,
    conn: sqlite3.Connection,
    rsps: responses.RequestsMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write = write_bulk_files(config.paths.bulk / "courtlistener")
    bulk.load_index(config, write, dataset_date="2026-09-30", manifest_captures={})
    add_court_row(conn, 1, details="U.S. District Court")  # a confirmed docket in the bulk index
    _quiet_doj(rsps)
    real = courtlistener._search

    def spent(ctx, secret, params, row_id):  # type: ignore[no-untyped-def]
        if "docket_id" in params:
            raise courtlistener.CaptureBudgetError("budget not available within 5 s")
        return real(ctx, secret, params, row_id)

    monkeypatch.setattr(courtlistener, "_search", spent)
    result = process(config, conn, 1)
    summary = result.summary_json
    assert summary["court_status"] == "primary_record"  # the bulk docket is still the record
    assert summary["flags"]["api_quota_exhausted"] is True  # type: ignore[index]
    assert result.input_hash and ":quota" in result.input_hash and ":recap" in result.input_hash
    assert summary["pacer_queued"] == 0 and not Path(config.court.pacer_queue).exists()
    assert any("daily budget spent" in n for n in summary["notes"])  # type: ignore[union-attr]


def test_a_missing_token_during_the_recap_check_is_transient_and_not_queued(
    config: Config,
    conn: sqlite3.Connection,
    rsps: responses.RequestsMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write = write_bulk_files(config.paths.bulk / "courtlistener")
    bulk.load_index(config, write, dataset_date="2026-09-30", manifest_captures={})
    add_court_row(conn, 1, details="U.S. District Court")
    _quiet_doj(rsps)
    monkeypatch.setattr(courtlistener, "token", lambda _ctx: None)
    result = process(config, conn, 1)
    assert result.input_hash and result.input_hash.endswith(":recap")
    assert result.summary_json["pacer_queued"] == 0
    assert not Path(config.court.pacer_queue).exists()


def test_several_api_dockets_for_one_name_are_all_weak_and_none_is_looked_up_or_queued(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    _index_without(config)
    add_court_row(conn, 1, name="Quinn Absent", details="U.S. District Court")
    _quiet_doj(rsps)
    first = _docket(party=["Quinn Absent"], caseName="United States v. Absent")
    second = _docket(
        docket_id=4243,
        docketNumber="3:14-cr-00099",
        party=["Quinn Absent"],
        caseName="United States v. Absent",
    )
    rsps.get(SEARCH, json={"results": [first, second]}, match=[_has_param("party_name")])
    summary = process(config, conn, 1).summary_json
    assert summary["court_status"] == "candidate_only"
    assert [i["weak"] for i in summary["located"]] == [True, True]  # type: ignore[union-attr]
    assert not [c for c in rsps.calls if "docket_id" in c.request.url]  # no RECAP call
    assert summary["pacer_queued"] == 0


def test_an_initial_only_or_unplaced_api_docket_is_weak_and_one_confirmed_docket_is_not() -> None:
    from efp.court.located import docket_is_weak

    base = {"row_state": "CT", "court_state": "CT", "candidates": 1, "initial_only": False}
    assert docket_is_weak(**base) is False
    assert docket_is_weak(**{**base, "initial_only": True}) is True
    assert docket_is_weak(**{**base, "court_state": None}) is True
    assert docket_is_weak(**{**base, "candidates": 2}) is True
    assert docket_is_weak(**{**base, "row_state": None}) is True
    assert docket_is_weak(**base, score=2) is True


# --- review round 3: a record is identified by its document -----------------------------------------------


def _docket_item(*, weak: bool, docket_id: int = 4242, strategy: str = "court.courtlistener_bulk"):  # type: ignore[no-untyped-def]
    from efp.court.located import Located

    return Located(
        source_kind="federal_docket",
        jurisdiction="US",
        court="D. Conn.",
        case_number="3:14-cr-00045",
        document_kind="docket",
        url="https://www.courtlistener.com/docket/4242/",
        strategy=strategy,
        capture_id=None,
        basis={"weak_because": "two candidates"} if weak else {"score": 4},
        weak=weak,
        docket_id=docket_id,
    )


def test_a_docket_found_again_as_weak_updates_its_record_instead_of_staying_primary(
    config: Config, conn: sqlite3.Connection
) -> None:
    add_court_row(conn, 1)
    first = component._upsert_record(conn, 1, _docket_item(weak=False))
    second = component._upsert_record(conn, 1, _docket_item(weak=True))
    assert first == second  # the same docket is the same record
    weak, basis = conn.execute("SELECT weak, basis_json FROM court_records").fetchone()
    assert weak == 1 and "two candidates" in basis
    assert conn.execute("SELECT count(*) FROM court_records").fetchone()[0] == 1


def test_a_different_docket_is_a_different_record(config: Config, conn: sqlite3.Connection) -> None:
    add_court_row(conn, 1)
    component._upsert_record(conn, 1, _docket_item(weak=False, docket_id=1))
    component._upsert_record(conn, 1, _docket_item(weak=False, docket_id=2))
    assert conn.execute("SELECT count(*) FROM court_records").fetchone()[0] == 2


def test_a_record_the_strategy_no_longer_finds_is_marked_not_current_not_deleted(
    config: Config, conn: sqlite3.Connection
) -> None:
    add_court_row(conn, 1)
    gone = component._upsert_record(conn, 1, _docket_item(weak=False, docket_id=1))
    kept = component._upsert_record(conn, 1, _docket_item(weak=False, docket_id=2))
    other = component._upsert_record(
        conn, 1, _docket_item(weak=False, docket_id=3, strategy="court.courtlistener_api")
    )
    n = component._supersede(conn, 1, {"court.courtlistener_bulk"}, {kept})
    states = dict(conn.execute("SELECT record_id, current FROM court_records").fetchall())
    # only the strategy that ran this time and did not find it again is superseded
    assert n == 1 and states == {gone: 0, kept: 1, other: 1}
    again = component._upsert_record(conn, 1, _docket_item(weak=False, docket_id=1))
    assert (
        again == gone
        and conn.execute(
            "SELECT current FROM court_records WHERE record_id = ?", (gone,)
        ).fetchone()[0]
        == 1
    )  # found again: current again


def test_a_courtlistener_outage_is_a_deferred_error_not_a_clean_not_found(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    _index_without(config)
    add_court_row(conn, 1, name="Quinn Absent", details="U.S. District Court")
    _quiet_doj(rsps)
    rsps.get(SEARCH, status=503)
    result = process(config, conn, 1)
    assert result.summary_json["court_status"] == "deferred_error"
    assert any("CourtListener" in e for e in result.summary_json["errors"])  # type: ignore[union-attr]
    assert result.input_hash and result.input_hash.endswith(":error")


def test_a_spent_budget_or_an_outage_does_not_retire_earlier_api_dockets(
    config: Config,
    conn: sqlite3.Connection,
    rsps: responses.RequestsMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _index_without(config)
    add_court_row(conn, 1, name="Quinn Absent", details="U.S. District Court")
    _quiet_doj(rsps)
    component._upsert_record(conn, 1, _docket_item(weak=False, strategy="court.courtlistener_api"))
    conn.commit()

    def spent(ctx, secret, params, row_id):  # type: ignore[no-untyped-def]
        raise courtlistener.CaptureBudgetError("budget not available within 5 s")

    monkeypatch.setattr(courtlistener, "_search", spent)
    summary = process(config, conn, 1).summary_json
    assert summary["court_status"] == "deferred_quota"
    assert conn.execute("SELECT current FROM court_records").fetchone()[0] == 1
    monkeypatch.undo()
    rsps.get(SEARCH, status=503)  # an outage: the module errored, so it was not evaluated either
    process(config, conn, 1)
    assert conn.execute("SELECT current FROM court_records").fetchone()[0] == 1


def test_a_press_release_is_the_same_record_whatever_page_or_listing_it_was_read_from(
    config: Config, conn: sqlite3.Connection
) -> None:
    from efp.court.located import Located

    add_court_row(conn, 1)

    def release(uuid: str, url: str) -> Located:
        return Located(
            source_kind="doj_release",
            jurisdiction="US",
            court=None,
            case_number="3:13-cr-00045",
            document_kind="press_release",
            url=url,
            strategy="court.doj",
            capture_id=None,  # the page or the listing: it differs on every fetch
            basis={"release_uuid": uuid},
            weak=False,
        )

    first = component._upsert_record(conn, 1, release("u-1", "https://www.justice.gov/pr/a"))
    again = component._upsert_record(conn, 1, release("u-1", "https://www.justice.gov/pr/a?x=1"))
    other = component._upsert_record(conn, 1, release("u-2", "https://www.justice.gov/pr/b"))
    assert first == again and other != first
    assert component._supersede(conn, 1, {"court.doj"}, {again, other}) == 0  # nothing flipped
    assert conn.execute("SELECT count(*) FROM court_records WHERE current = 1").fetchone()[0] == 2


def test_a_pin_report_with_no_entries_or_that_cannot_be_read_is_not_fetched_for_every_row(
    config: Config, tmp_path: Path
) -> None:
    from efp.court import pin

    index = pin.PinIndex(tmp_path / "pin.sqlite")
    try:
        assert not index.indexed("https://example.test/scan.pdf")
        assert index.index_pages("https://example.test/scan.pdf", 1, ["", "  "]) == 0
        index.mark("https://example.test/scan.pdf", 1, 0, "no extractable text")
        assert index.indexed("https://example.test/scan.pdf")  # read once, never again
    finally:
        index.close()


def test_a_429_is_not_retried_and_the_rest_of_the_run_does_not_ask_again(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    _index_without(config)
    add_court_row(conn, 1, name="Quinn Absent", details="U.S. District Court")
    add_court_row(conn, 2, name="Pat Absent", details="U.S. District Court", sha="2" * 64)
    _quiet_doj(rsps)
    rsps.get(SEARCH, status=429, headers={"Retry-After": "250"})
    ctx, cap, registry = make_ctx(config, conn)
    first = component.process_row(row_of(conn, 1), ctx, registry)
    second = component.process_row(row_of(conn, 2), ctx, registry)
    cap.close()
    assert first.summary_json["court_status"] == second.summary_json["court_status"]
    assert first.summary_json["court_status"] == "deferred_quota"
    asked = [c for c in rsps.calls if "courtlistener.com/api" in c.request.url]
    assert len(asked) == 1  # one request, no retries, and nothing for the second row


def test_the_harvest_inside_a_row_stops_after_its_page_budget_and_leaves_a_gap(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    from efp.court import doj

    tight = config.model_copy(
        update={
            "court": config.court.model_copy(
                update={"doj_harvest_pages_per_row": 2, "doj_harvest_max_pages": 50}
            )
        }
    )
    page = {
        "metadata": {"resultset": {"count": 1, "pagesize": 50}},
        "results": [
            {
                "uuid": "n",
                "title": "News",
                "url": "https://www.justice.gov/n",
                "date": "1735689600",
                "body": "<p>x</p>",
            }
        ],
    }
    rsps.get("https://www.justice.gov/api/v1/press_releases.json", json=page)
    ctx, cap, _ = make_ctx(tight, conn)
    try:
        client = doj.DojClient(ctx)
        assert client.harvest(2010, None) == 2  # the row's budget, not the 50-page cap
        assert not client.covered(2010)
        assert client.harvest(2010, None, max_pages=10) == 10  # the explicit step goes on
        client.close()
    finally:
        cap.close()


def test_a_weak_record_yields_no_person_level_identifiers_only_its_case_number(
    config: Config, conn: sqlite3.Connection
) -> None:
    from efp.court.located import Located
    from efp.court.names import parse_name

    add_court_row(conn, 1)
    from tests.dbhelpers import insert_capture

    cap_id = 900
    insert_capture(conn, cap_id, 1, content_sha256="a" * 64)
    text = "Alex Example, 47, of Dayton, Ohio, pleaded guilty in Case No. 3:13-cr-00045."

    def item(weak: bool) -> Located:
        return Located(
            source_kind="doj_release", jurisdiction="US", court=None, case_number=None,
            document_kind="press_release", url="u", strategy="court.doj", capture_id=cap_id,
            text=text, text_capture_id=cap_id, weak=weak,
        )  # fmt: skip

    state = component.CaseState(component.classify("", "", []), parse_name("Alex Example"))
    state.located = [item(True)]
    weak_counts = component._insert_identifiers(conn, 1, state)
    assert set(weak_counts) <= {"case_number"}
    state.located = [item(False)]
    assert {"age", "city"} & set(component._insert_identifiers(conn, 1, state))


def test_a_weak_only_or_report_only_row_is_a_candidate_not_a_press_release_result() -> None:
    from efp.court.located import Located

    def item(kind: str, weak: bool) -> Located:
        return Located(
            source_kind=kind, jurisdiction="US", court=None, case_number=None,
            document_kind="x", url=None, strategy="s", capture_id=None, weak=weak,
        )  # fmt: skip

    assert component.court_status([item("pin_report", True)]) == "candidate_only"
    assert component.court_status([item("doj_release", True)]) == "candidate_only"
    assert component.court_status([item("doj_release", False)]) == "press_release_only"
    assert component.court_status([]) == "not_found"


def test_top_candidates_does_not_hide_an_ambiguity_from_the_weak_rule(
    config: Config, conn: sqlite3.Connection
) -> None:
    one = config.model_copy(update={"court": config.court.model_copy(update={"top_candidates": 1})})
    write = write_bulk_files(one.paths.bulk / "courtlistener")
    bulk.load_index(one, write, dataset_date="2026-09-30", manifest_captures={})
    con = bulk.duckdb.connect(str(bulk.index_path(one)))
    for n in (801, 802):  # two more dockets for the same name in the same court and years
        con.execute(
            f"INSERT INTO dockets VALUES ({n}, 'ctd', '3:14-cr-0{n}', 'United States v. Example', '2014-02-01', NULL, '{n}', '/d/{n}/')"
        )
        con.execute(f"INSERT INTO docket_parties VALUES ({n}, 9990)")
    con.execute("INSERT INTO parties VALUES (9990, 'Alex Example')")
    con.close()
    hits = bulk.search_dockets(one, parse_name("Alex Example"), state="CT", year=2014)
    assert len(hits) == 1  # truncated to top_candidates...
    assert len(bulk.search_dockets_all(one, parse_name("Alex Example"), state="CT", year=2014)) > 1
    add_court_row(conn, 1, details="U.S. District Court")
    # ...but the module still sees the ambiguity
    ctx, cap, registry = make_ctx(one, conn)
    task_row = row_of(conn, 1)
    result = component.process_row(task_row, ctx, registry)
    cap.close()
    docket = [i for i in result.summary_json["located"] if i["source_kind"] == "federal_docket"]  # type: ignore[union-attr]
    assert docket and all(i["weak"] for i in docket)
    assert result.summary_json["court_status"] == "candidate_only"


def test_the_views_hide_superseded_records_and_candidates(
    config: Config, conn: sqlite3.Connection
) -> None:
    add_court_row(conn, 1)
    confirmed = component._upsert_record(conn, 1, _docket_item(weak=False, docket_id=1))
    candidate = component._upsert_record(conn, 1, _docket_item(weak=True, docket_id=2))
    component._upsert_record(conn, 1, _docket_item(weak=False, docket_id=3))  # found no more
    component._supersede(conn, 1, {"court.courtlistener_bulk"}, {confirmed, candidate})
    current = {r[0] for r in conn.execute("SELECT docket_id FROM court_records_current")}
    sure = {r[0] for r in conn.execute("SELECT docket_id FROM court_records_confirmed")}
    assert current == {1, 2} and sure == {1}  # 3 was superseded; 2 is only a candidate


def test_a_throttle_is_a_typed_run_scoped_memory_that_a_new_run_clears(
    config: Config, conn: sqlite3.Connection
) -> None:
    from efp import runner as rn

    ctx, cap, _ = make_ctx(config, conn)
    try:
        assert not cap.is_throttled("courtlistener")
        cap.mark_throttled("courtlistener")
        assert cap.is_throttled("courtlistener")
        rn.run_stage(conn, config, rn.RunOptions("court"), ctx, component=component)
        assert not cap.is_throttled("courtlistener")  # the start of a run forgets it
    finally:
        cap.close()


def test_identifiers_of_a_record_that_turned_weak_or_stopped_being_found_are_not_readable(
    config: Config, conn: sqlite3.Connection
) -> None:
    from efp.court.located import Located
    from efp.court.names import parse_name
    from tests.dbhelpers import insert_capture

    add_court_row(conn, 1)
    insert_capture(conn, 901, 1, content_sha256="b" * 64)
    text = "Alex Example, 47, of Dayton, Ohio, pleaded guilty in Case No. 3:13-cr-00045."

    def item(weak: bool) -> Located:
        return Located(
            source_kind="doj_release", jurisdiction="US", court=None, case_number=None,
            document_kind="press_release", url="u", strategy="court.doj", capture_id=901,
            text=text, text_capture_id=901, basis={"release_uuid": "r1"}, weak=weak,
        )  # fmt: skip

    def readable() -> set[str]:
        return {r[0] for r in conn.execute("SELECT kind FROM court_identifiers_current")}

    state = component.CaseState(component.classify("", "", []), parse_name("Alex Example"))
    state.located = [item(False)]
    record = component._upsert_record(conn, 1, state.located[0])
    component._insert_identifiers(conn, 1, state)
    assert {"age", "city"} <= readable()  # confirmed: the facts are readable
    # a later run finds the same release ambiguous: the facts written earlier must disappear
    component._upsert_record(conn, 1, item(True))
    assert not ({"age", "city", "dob", "office", "employer", "party_on_ballot"} & readable())
    assert conn.execute("SELECT count(*) FROM identifiers WHERE kind = 'age'").fetchone()[0] == 1
    # confirmed again, then no longer found at all: nothing from it is readable
    component._upsert_record(conn, 1, item(False))
    assert {"age", "city"} <= readable()
    component._supersede(conn, 1, {"court.doj"}, set())
    assert readable() == set() and record


def test_a_pin_report_that_failed_to_fetch_is_tried_once_per_run_not_per_row(
    config: Config,
    conn: sqlite3.Connection,
    rsps: responses.RequestsMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from efp.court import pin

    url = "https://www.justice.gov/criminal/dead-report.pdf"
    wired = config.model_copy(
        update={"court": config.court.model_copy(update={"pin_report_urls": [url]})}
    )
    monkeypatch.setattr(pin, "_backend", lambda: lambda _pdf: [])
    rsps.get("https://www.justice.gov/robots.txt", status=404)
    rsps.get(url, status=404)
    ctx, cap, _ = make_ctx(wired, conn)
    index = pin.PinIndex(wired.paths.bulk / "pin.sqlite")
    try:
        module = pin.CourtPin()
        for _ in range(3):  # three pre-2013 rows
            task = SimpleNamespace(cs=SimpleNamespace(notes=[]))
            module._ensure_indexed(ctx, index, task)  # type: ignore[arg-type]

        def captures() -> int:
            return int(
                conn.execute(
                    "SELECT count(*) FROM captures WHERE url_requested = ?", (url,)
                ).fetchone()[0]
            )

        # the response cache already spares the network a repeat of a 404, but without the memory
        # every row would still write its own capture row: once per run, however many rows
        assert captures() == 1
        cap.clear_throttles()  # the next run starts fresh and tries again
        module._ensure_indexed(ctx, index, SimpleNamespace(cs=SimpleNamespace(notes=[])))  # type: ignore[arg-type]
        assert captures() == 2
    finally:
        index.close()
        cap.close()


def _recap_setup(
    config: Config,
    conn: sqlite3.Connection,
    rsps: responses.RequestsMock,
    docs: list[dict[str, Any]],
):  # type: ignore[no-untyped-def]
    _index_without(config)
    add_court_row(conn, 1, name="Quinn Absent", details="Charged in U.S. District Court")
    _quiet_doj(rsps)
    mine = _docket(recap_documents=docs, party=["Quinn Absent"], caseName="United States v. Absent")
    rsps.get(SEARCH, json={"results": [mine]}, match=[_has_param("party_name")])
    rsps.get(SEARCH, json={"results": [mine]}, match=[_has_param("docket_id")])
    rsps.get("https://storage.courtlistener.com/robots.txt", status=404)


_LISTED = {
    "id": 9,
    "description": "Indictment",
    "filepath_local": PDF_URL.removeprefix("https://storage.courtlistener.com/"),
    "is_available": True,
}


def test_filings_that_were_listed_but_failed_to_download_are_not_queued_as_missing(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    _recap_setup(config, conn, rsps, [_LISTED])
    rsps.get(PDF_URL, status=503)  # the archive lists it but the storage host fails
    result = process(config, conn, 1)
    summary = result.summary_json
    assert summary["pacer_queued"] == 0 and not Path(config.court.pacer_queue).exists()
    assert "documents not fetched" in json.dumps(summary["recap"])
    assert result.input_hash and result.input_hash.endswith(":recap")  # --recheck retries it


def test_an_empty_listing_or_only_unavailable_filings_is_absent_from_recap_and_queued(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    _recap_setup(config, conn, rsps, [{**_LISTED, "is_available": False}])
    result = process(config, conn, 1)
    assert result.summary_json["pacer_queued"] == 1  # genuinely not in RECAP: for the operator
    assert not (result.input_hash or "").endswith(":recap")


def test_a_spent_budget_while_fetching_a_filing_is_a_quota_deferral(
    config: Config,
    conn: sqlite3.Connection,
    rsps: responses.RequestsMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _recap_setup(config, conn, rsps, [_LISTED])
    from efp.capture import Capture, CaptureBudgetError

    real = Capture.fetch

    def fetch(self, url, **kwargs):  # type: ignore[no-untyped-def]
        if url == PDF_URL:
            raise CaptureBudgetError("budget not available")
        return real(self, url, **kwargs)

    monkeypatch.setattr(Capture, "fetch", fetch)
    summary = process(config, conn, 1).summary_json
    assert summary["flags"]["api_quota_exhausted"] is True  # type: ignore[index]
    assert summary["pacer_queued"] == 0


def test_a_failed_or_empty_pin_read_is_final_only_for_the_reader_that_tried_it(
    tmp_path: Path,
) -> None:
    from efp.court import pin

    index = pin.PinIndex(tmp_path / "pin.sqlite")
    try:
        url = "https://example.test/report.pdf"
        index.mark(url, 1, 0, "could not be read", "pypdf 4.0")
        assert index.indexed(url, "pypdf 4.0")  # the same reader will not read it again
        assert not index.indexed(url, "pdfplumber 0.11")  # a different (or fixed) one will
        assert not index.indexed(url, "none")
        index.mark(url, 1, 3, None, "pdfplumber 0.11")
        index.index_pages(url, 1, ["x" * 60])
        assert index.indexed(url, "whatever 9")  # it yielded entries: final for every reader
    finally:
        index.close()


def test_a_filing_is_only_as_current_and_confirmed_as_its_docket(
    config: Config, conn: sqlite3.Connection
) -> None:
    from efp.court.located import Located

    add_court_row(conn, 1)
    strategy = "court.courtlistener_api"

    def filing() -> Located:
        return Located(
            source_kind="federal_docket", jurisdiction="US", court="D. Conn.",
            case_number="3:14-cr-00045", document_kind="filing", url="https://s/f.pdf",
            strategy=strategy, capture_id=None, basis={"recap_document_id": 9}, docket_id=4242,
        )  # fmt: skip

    def seen(view: str) -> set[str]:
        return {r[0] for r in conn.execute(f"SELECT document_kind FROM {view}")}

    docket = component._upsert_record(conn, 1, _docket_item(weak=False, strategy=strategy))
    component._upsert_record(conn, 1, filing())
    assert seen("court_records_confirmed") == {"docket", "filing"}
    # a second candidate makes the docket ambiguous; the filing rows are not touched by the run
    component._upsert_record(conn, 1, _docket_item(weak=True, strategy=strategy))
    assert seen("court_records_confirmed") == set()  # the filing is no longer confirmed
    assert seen("court_records_current") == {"docket", "filing"}  # still found, as a candidate
    # confirmed again, then the docket is no longer found at all
    component._upsert_record(conn, 1, _docket_item(weak=False, strategy=strategy))
    assert seen("court_records_confirmed") == {"docket", "filing"}
    component._supersede(conn, 1, {strategy}, set())
    assert seen("court_records_current") == set() and docket

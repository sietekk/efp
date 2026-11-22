"""Regression tests for the independent review of build 07."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import responses
import stamina
from typer.testing import CliRunner

from efp.capture import CaptureError
from efp.cli import app
from efp.config import Config
from efp.court import bulk, doj
from efp.court.identifiers import extract_identifiers
from efp.court.names import match_name, parse_name
from tests.courthelpers import (
    ARTICLE,
    add_court_row,
    court_config,
    doj_page,
    make_ctx,
    process,
    record_manifests,
    release,
    release_html,
    write_bulk_files,
)

runner = CliRunner()
API = doj.API


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


# --- names and identifiers: other people are not the subject -----------------------------------------


def test_one_defendants_facts_and_case_number_are_not_given_to_another() -> None:
    text = (
        "Alex Example, 47, of Springfield, and Blake Other, 52, of Hartford, were charged. "
        "The cases are No. 19-CR-123 and No. 19-CR-124."
    )
    found = {(f.kind, f.value) for f in extract_identifiers(text, parse_name("Alex Example"))}
    assert ("age", "47") in found and ("city", "Springfield") in found
    assert ("age", "52") not in found and ("city", "Hartford") not in found
    assert not [
        k for k, _ in found if k == "case_number"
    ]  # two cases, none in the subject's clause


def test_a_single_case_number_in_a_release_belongs_to_it_and_several_belong_to_their_own_sentence() -> (
    None
):
    own = "Alex Example, 47, of Springfield, pleaded guilty in Case No. 19-CR-123. Blake Other pleaded in Case No. 19-CR-124."
    values = [
        f.value
        for f in extract_identifiers(own, parse_name("Alex Example"))
        if f.kind == "case_number"
    ]
    assert values == ["19-CR-123"]


@pytest.mark.parametrize(
    ("name", "text"),
    [
        (
            "John May",
            "John told May that the vote was close",
        ),  # words between are not a middle name
        ("Al Ng", "Al and Ng were present"),
        ("Jane Smith", "Janet Smith pleaded guilty"),
    ],
)
def test_ordinary_words_between_a_given_name_and_a_surname_are_not_a_match(
    name: str, text: str
) -> None:
    found = match_name(parse_name(name), text)
    assert not (found and found.full)


def test_a_capitalised_middle_name_is_still_a_match_and_an_initial_alone_is_flagged() -> None:
    assert match_name(parse_name("Jane Smith"), "Jane Q. Smith pleaded").full  # type: ignore[union-attr]
    assert match_name(parse_name("Jane Smith"), "Jane Quincy Smith pleaded").full  # type: ignore[union-attr]
    weak = match_name(parse_name("Jane Smith"), "J. Smith spoke")
    assert weak is not None and weak.full and weak.initial_only


@pytest.mark.parametrize(
    ("text", "abbreviation", "expected"),
    [
        ("a West Virginia man", "VA", False),  # not Virginia
        ("a Virginia man", "VA", True),
        ("Washington, D.C. - a man", "WA", False),  # a dateline, not the state
        ("SEATTLE, Washington", "WA", True),
        ("a North Carolina clerk", "SC", False),
        ("Hartford, CT", "CT", True),
    ],
)
def test_state_corroboration_does_not_confuse_similar_names(
    text: str, abbreviation: str, expected: bool
) -> None:
    assert doj.mentions_state(text, abbreviation) is expected


# --- DOJ: depth, aliases and the weak flag -----------------------------------------------------------

TARGET = (
    "Alex Example, 47, of Hartford, Connecticut, pleaded guilty to voting twice. "
    "Case No. 3:13-cr-00045 is pending. " + ARTICLE
)


def test_the_title_search_pages_back_to_the_rows_year_and_asks_for_aliases(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    add_court_row(conn, 1, name="Alex Example aka Alec Sample", details="x")
    rsps.get(
        "https://www.justice.gov/usao-ct/pr/example-release",
        body=release_html(TARGET),
        content_type="text/html",
    )
    recent = [
        release(f"Example sentenced {i}", "voter fraud", date="2020-01-01", uuid=f"n{i}")
        for i in range(50)
    ]
    wanted = release(
        "Alex Example pleads guilty to voter fraud", TARGET, date="2014-03-05", uuid="w"
    )
    older = [release("Example older", "x", date="2009-01-01", uuid="o")]
    for number, payload in enumerate(
        (doj_page(*recent), doj_page(wanted, *recent[:2]), doj_page(*older))
    ):
        rsps.get(
            API,
            json=payload,
            match=[
                responses.matchers.query_param_matcher(
                    {"page": str(number), "parameters[title]": "Example"}, strict_match=False
                )
            ],
        )
    rsps.get(
        API,
        json=doj_page(),
        match=[
            responses.matchers.query_param_matcher(
                {"parameters[title]": "Sample"}, strict_match=False
            )
        ],
    )
    result = process(config, conn, 1)
    assert (
        result.summary_json["court_status"] == "press_release_only"
    )  # found on page 1, past page 0
    queries = [c.request.url for c in rsps.calls if "press_releases" in c.request.url]
    assert any("Sample" in q for q in queries) and any("page=1" in q for q in queries)


def test_a_release_naming_several_case_numbers_is_flagged_weak(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    add_court_row(conn, 1)
    body = (
        "Alex Example, 47, of Hartford, Connecticut, pleaded guilty to voting twice in Case No. 3:13-cr-00045. "
        "Blake Other pleaded in Case No. 3:13-cr-00046 to ballot fraud. " + ARTICLE
    )
    rsps.get(
        "https://www.justice.gov/usao-ct/pr/example-release",
        body=release_html(body),
        content_type="text/html",
    )
    rsps.get(
        API,
        json=doj_page(release("Alex Example and Blake Other plead guilty to voter fraud", body)),
    )
    summary = process(config, conn, 1).summary_json
    located = summary["located"][0]  # type: ignore[index]
    assert located["weak"] is True and located["basis"]["document_has_several_case_numbers"] is True
    assert located["case_number"] == "3:13-cr-00045"  # the one in the subject's own sentence
    assert (
        located["basis"]["name_span"] and located["capture_id"]
    )  # why it matched is in the summary


# --- the bulk index: several hits are not settled; bad rows; cleanup ------------------------------------------


def test_a_name_with_several_dockets_in_the_index_is_flagged_weak(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    paths = write_bulk_files(config.paths.bulk / "courtlistener")
    bulk.load_index(config, paths, dataset_date="2026-09-30", manifest_captures={})
    con = bulk.duckdb.connect(str(bulk.index_path(config)))
    con.execute(
        "INSERT INTO dockets VALUES (999, 'ctd', '3:14-cr-00999', 'United States v. Example', '2014-01-01', NULL, '999', '/docket/999/x/')"
    )
    con.execute("INSERT INTO parties VALUES (9999, 'Alex Example')")
    con.execute("INSERT INTO docket_parties VALUES (999, 9999)")
    con.close()
    add_court_row(conn, 1, details="U.S. District Court")
    rsps.get("https://www.justice.gov/api/v1/press_releases.json", json=doj_page())
    rsps.get("https://www.courtlistener.com/api/rest/v4/search/", json={"results": []})
    located = process(config, conn, 1).summary_json["located"]  # type: ignore[index]
    dockets = [i for i in located if i["document_kind"] == "docket"]
    assert len(dockets) == 2 and all(
        d["weak"] for d in dockets
    )  # one name, two dockets: not settled


def test_a_malformed_docket_id_is_skipped_and_counted_not_fatal(
    config: Config, tmp_path: Path
) -> None:
    paths = write_bulk_files(tmp_path / "bad", compress=False)
    text = paths["dockets"].read_text().splitlines()
    text.insert(2, "not-a-number,ctd,3:13-cr-99999,United States v. Bad,2013-01-01,,1,/x/")
    paths["dockets"].write_text("\n".join(text) + "\n")
    result = bulk.load_index(config, paths, dataset_date="x", manifest_captures={})
    assert result.bad_rows == 1 and result.dockets > 300


def test_a_failed_load_leaves_no_partial_index_or_wal_behind(
    config: Config, tmp_path: Path
) -> None:
    paths = write_bulk_files(tmp_path / "broken", compress=False)
    paths["parties"].write_text(
        "id,label\n1,x\n"
    )  # the layout error comes after dockets are loaded
    with pytest.raises(bulk.BulkLayoutError):
        bulk.load_index(config, paths, dataset_date="x", manifest_captures={})
    left = [p.name for p in config.paths.bulk.glob("courtlistener.duckdb*")]
    assert left == []


def test_like_wildcards_in_a_name_are_not_wildcards(config: Config, tmp_path: Path) -> None:
    paths = write_bulk_files(tmp_path / "w", compress=False)
    bulk.load_index(config, paths, dataset_date="x", manifest_captures={})
    assert bulk.search_dockets(config, parse_name("Alex %"), state="CT", year=2014) == []
    assert bulk.search_dockets(config, parse_name("Alex _xample"), state="CT", year=2014) == []


# --- downloads ----------------------------------------------------------------------------------------------


def test_a_file_already_downloaded_and_recorded_is_not_fetched_again(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    ctx, cap, _ = make_ctx(config, conn)
    payload = b"id,name\n1,x\n"
    item = bulk.BulkFile(
        "parties", "2026-09-30", "bulk-data/parties-2026-09-30.csv", len(payload), "", ""
    )
    rsps.get("https://bulk.example.test/bulk-data/parties-2026-09-30.csv", body=payload)
    first = bulk.fetch_files(ctx, {"parties": item}, max_gb=1)
    second = bulk.fetch_files(ctx, {"parties": item}, max_gb=1)
    cap.close()
    assert first == second  # the same capture
    assert len([c for c in rsps.calls if c.request.url.endswith(".csv")]) == 1


def test_a_download_that_redirects_is_refused_not_followed(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    _ctx, cap, _ = make_ctx(config, conn)
    rsps.get(
        "https://bulk.example.test/f.csv",
        status=302,
        headers={"Location": "https://elsewhere.test/f.csv"},
    )
    with pytest.raises(CaptureError, match="redirected"):
        cap.download("https://bulk.example.test/f.csv", config.paths.bulk / "f.csv", stage="bulk")
    cap.close()
    assert not [c for c in rsps.calls if "elsewhere.test" in c.request.url]


def test_load_uses_one_complete_quarter_never_a_mix(
    config: Config, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EFP_CONFIG", str(Path(config.paths.data_dir).parent / "config.toml"))
    folder = config.paths.bulk / "courtlistener"
    older = write_bulk_files(folder, date="2026-06-30", compress=False)  # a complete older quarter
    record_manifests(config, conn, older)
    newer = write_bulk_files(folder, date="2026-09-30", compress=False)
    newer["parties"].unlink()  # the newer quarter is missing a file
    result = runner.invoke(app, ["court", "bulk", "load"])
    assert result.exit_code == 0 and "dataset 2026-06-30" in result.output  # the complete one
    record_manifests(config, conn, write_bulk_files(folder, date="2026-09-30", compress=False))
    again = runner.invoke(app, ["court", "bulk", "load"])
    assert "dataset 2026-09-30" in again.output


# --- the queue ----------------------------------------------------------------------------------------------


def test_weak_candidates_are_neither_looked_up_in_recap_nor_queued_for_purchase(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    paths = write_bulk_files(config.paths.bulk / "courtlistener")
    bulk.load_index(config, paths, dataset_date="2026-09-30", manifest_captures={})
    con = bulk.duckdb.connect(str(bulk.index_path(config)))
    con.execute(
        "INSERT INTO dockets VALUES (999, 'ctd', '3:14-cr-00999', 'United States v. Example', '2014-01-01', NULL, '999', '/d/999/')"
    )
    con.execute("INSERT INTO parties VALUES (9999, 'Alex Example')")
    con.execute("INSERT INTO docket_parties VALUES (999, 9999)")
    con.close()
    add_court_row(conn, 1, details="U.S. District Court")
    rsps.get("https://www.justice.gov/api/v1/press_releases.json", json=doj_page())
    rsps.get("https://www.courtlistener.com/api/rest/v4/search/", json={"results": []})
    summary = process(config, conn, 1).summary_json
    assert summary["court_status"] == "candidate_only"  # two dockets for one name: not settled
    assert not [
        c
        for c in rsps.calls
        if "courtlistener.com/api/rest/v4/search" in c.request.url and "docket_id" in c.request.url
    ]  # no API call spent on a candidate's documents
    assert all(v.get("skipped") for v in summary["recap"].values())  # type: ignore[union-attr]
    assert summary["pacer_queued"] == 0 and not Path(config.court.pacer_queue).exists()


def test_the_queue_lives_under_the_data_directory_unless_a_path_is_given(config: Config) -> None:
    from efp.court import recap

    relative = config.model_copy(
        update={"court": config.court.model_copy(update={"pacer_queue": "queues/pacer.csv"})}
    )
    assert recap.queue_path(relative) == config.paths.data_dir / "queues" / "pacer.csv"
    assert json.dumps(str(recap.queue_path(config))).startswith(
        '"/'
    )  # the test config gives an absolute one

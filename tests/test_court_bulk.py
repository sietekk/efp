from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import duckdb
import pytest
import responses
import stamina

from efp.capture import CaptureError
from efp.config import Config
from efp.court import bulk
from efp.court.names import parse_name
from tests.courthelpers import (
    HEADERS,
    add_court_row,
    court_config,
    make_ctx,
    process,
    s3_listing,
    write_bulk_files,
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


@pytest.fixture
def loaded(config: Config) -> bulk.LoadResult:
    paths = write_bulk_files(config.paths.bulk / "courtlistener")
    return bulk.load_index(
        config, paths, dataset_date="2026-09-30", manifest_captures={"dockets": 5}
    )


# --- the listing ---------------------------------------------------------------------------------


def test_the_listing_is_parsed_and_the_newest_file_of_each_kind_chosen() -> None:
    xml = s3_listing(
        [
            ("bulk-data/dockets-2026-06-30.csv.bz2", 10),
            ("bulk-data/dockets-2026-09-30.csv.bz2", 20_000_000_000),
            ("bulk-data/courts-2026-09-30.csv.bz2", 5_000),
            ("bulk-data/parties-2026-09-30.csv.bz2", 7),
            ("bulk-data/party-types-2026-09-30.csv.bz2", 7),
            ("bulk-data/dockets-2026-12-31.csv.bz2", 30_000_000_000),  # newest, but no full set
            ("bulk-data/opinions-2026-09-30.csv.bz2", 99),  # not a kind we load
            ("bulk-data/readme.txt", 1),
        ]
    )
    files, token = bulk.parse_listing(xml)
    assert token is None and {f.kind for f in files} == set(bulk.KINDS)
    best = bulk.latest_set(files)  # the 12-31 dockets file has no full set: one date, all four
    assert {f.date for f in best.values()} == {"2026-09-30"}
    assert best["dockets"].size == 20_000_000_000
    assert bulk.total_gb(best.values()) == pytest.approx(20.000014)
    assert bulk.latest_set([f for f in files if f.kind != "parties"]) == {}  # no complete date


def test_a_truncated_listing_gives_its_continuation_token() -> None:
    _, token = bulk.parse_listing(s3_listing([("bulk-data/courts-2026-09-30.csv", 1)], token="abc"))
    assert token == "abc"


def test_listing_is_fetched_through_the_capture_layer_and_follows_pages(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    ctx, cap, _ = make_ctx(config, conn)
    url = config.court.bulk_listing_url.split("?")[0]
    rsps.get(
        url,
        body=s3_listing([("bulk-data/courts-2026-09-30.csv.bz2", 1)], token="t1"),
        match=[
            responses.matchers.query_param_matcher(
                {"list-type": "2", "prefix": "bulk-data/"}, strict_match=False
            )
        ],
    )
    rsps.get(
        url,
        body=s3_listing([("bulk-data/dockets-2026-09-30.csv.bz2", 2)]),
        match=[
            responses.matchers.query_param_matcher({"continuation-token": "t1"}, strict_match=False)
        ],
    )
    files = bulk.list_bulk_files(ctx)
    cap.close()
    assert {f.kind for f in files} == {"courts", "dockets"}
    assert conn.execute("SELECT count(*) FROM captures WHERE stage = 'bulk'").fetchone()[0] == 2


# --- the loader ---------------------------------------------------------------------------------


def test_the_loader_keeps_federal_criminal_dockets_from_1999_and_their_parties(
    config: Config, loaded: bulk.LoadResult
) -> None:
    assert loaded.courts == 4 and loaded.dockets > 300  # bankruptcy and state courts are dropped
    con = duckdb.connect(str(bulk.index_path(config)), read_only=True)
    try:
        numbers = {r[0] for r in con.execute("SELECT docket_number FROM dockets").fetchall()}
        assert "3:13-cr-00045" in numbers  # the planted target
        assert "3:13-cv-00046" not in numbers  # a civil docket
        assert "3:13-cr-00047" not in numbers  # a bankruptcy court
        assert "3:95-cr-00001" not in numbers  # before 1999
        parties = con.execute("SELECT count(*) FROM parties").fetchone()[0]
        assert parties == loaded.parties > 300
        states = dict(con.execute("SELECT court_id, state FROM courts").fetchall())
        assert states["ctd"] == "CT" and states["nysd"] == "NY" and states["ohnd"] == "OH"
    finally:
        con.close()


def test_the_index_records_where_it_came_from(config: Config, loaded: bulk.LoadResult) -> None:
    info = bulk.index_info(config)
    assert info is not None
    assert (info.dataset_date, info.dockets, info.manifest_captures) == (
        "2026-09-30",
        loaded.dockets,
        {"dockets": 5},
    )
    assert info.loaded_at.startswith("20")


def test_no_index_means_no_info(config: Config) -> None:
    assert bulk.index_info(config) is None


def test_plain_and_compressed_files_load_the_same(config: Config, tmp_path: Path) -> None:
    plain = write_bulk_files(tmp_path / "plain", compress=False)
    result = bulk.load_index(config, plain, dataset_date="2026-09-30", manifest_captures={})
    assert result.dockets > 300


def test_extra_columns_are_ignored_and_aliases_are_found(config: Config, tmp_path: Path) -> None:
    # the real files carry many more columns; ours renames two and adds noise
    headers = {k: list(v) for k, v in HEADERS.items()}
    headers["dockets"] = [
        "id",
        "court",
        "docket_number",
        "case_name_full",
        "date_filed",
        "date_terminated",
        "pacer_case_id",
        "absolute_url",
    ]
    paths = write_bulk_files(tmp_path / "alias", headers=headers)
    assert bulk.load_index(config, paths, dataset_date="x", manifest_captures={}).dockets > 300


def test_a_missing_column_stops_the_load_and_names_what_the_file_has(
    config: Config, tmp_path: Path
) -> None:
    headers = {k: list(v) for k, v in HEADERS.items()}
    headers["dockets"] = ["id", "court_id", "case_name", "date_filed"]  # no docket_number
    paths = write_bulk_files(tmp_path / "bad", headers=headers)
    with pytest.raises(bulk.BulkLayoutError) as excinfo:
        bulk.load_index(config, paths, dataset_date="x", manifest_captures={})
    message = str(excinfo.value)
    assert "docket_number" in message and "case_name" in message and "dockets" in message
    assert not bulk.index_path(config).exists()  # nothing half-built is left in place


def test_court_states_are_read_from_court_names() -> None:
    assert bulk.state_of_court("District Court, D. Connecticut") == "CT"
    assert bulk.state_of_court("District Court, W.D. Virginia") == "VA"
    assert bulk.state_of_court("District Court, N.D. West Virginia") == "WV"  # not Virginia
    assert bulk.state_of_court("Supreme Court of the United States") is None


@pytest.mark.parametrize(
    ("number", "name", "criminal"),
    [
        ("3:13-cr-00045", "x", True),
        ("1:19-CR-123", "x", True),
        ("3:13-cv-00046", "Doe v. Roe", False),
        ("", "United States v. Example", True),
        ("", "USA v. Example", True),
        ("2:20-mj-1", "In re Sealed", False),
    ],
)
def test_criminal_dockets_are_recognised(number: str, name: str, criminal: bool) -> None:
    assert bulk.looks_criminal(number, name) is criminal


# --- searching ----------------------------------------------------------------------------------


def test_search_finds_the_planted_docket_by_name_state_and_year(
    config: Config, loaded: bulk.LoadResult
) -> None:
    hits = bulk.search_dockets(config, parse_name("Alex Example"), state="CT", year=2014)
    assert [h.docket_number for h in hits] == ["3:13-cr-00045"]
    assert hits[0].state == "CT" and hits[0].score >= 3 and "party name" in hits[0].matched_on


def test_the_state_filter_keeps_out_the_same_name_in_another_state(
    config: Config, loaded: bulk.LoadResult
) -> None:
    ny = bulk.search_dockets(config, parse_name("Alex Example"), state="NY", year=2014)
    assert [h.docket_number for h in ny] == ["1:13-cr-00777"]


def test_the_year_window_applies(config: Config, loaded: bulk.LoadResult) -> None:
    assert bulk.search_dockets(config, parse_name("Alex Example"), state="CT", year=2000) == []
    assert bulk.search_dockets(config, parse_name("Alex Example"), state="CT", year=2030) == []


def test_a_surname_alone_is_not_a_match(config: Config, loaded: bulk.LoadResult) -> None:
    hits = bulk.search_dockets(config, parse_name("Zed Example"), state="CT", year=2014)
    assert hits == []  # "Alex" and "Bo" Example exist, but no Zed


def test_no_index_or_no_name_finds_nothing(config: Config) -> None:
    assert bulk.search_dockets(config, parse_name("Alex Example"), state="CT", year=2014) == []
    assert bulk.search_dockets(config, parse_name(""), state="CT", year=2014) == []


# --- the strategy: bulk before API ------------------------------------------------------------------


def test_an_index_hit_is_a_federal_docket_and_the_api_is_not_called(
    config: Config, conn: sqlite3.Connection, loaded: bulk.LoadResult, rsps: responses.RequestsMock
) -> None:
    add_court_row(conn, 1, details="Charged in U.S. District Court")
    rsps.get("https://www.justice.gov/robots.txt", status=404)
    rsps.get("https://www.justice.gov/api/v1/press_releases.json", json={"results": []})
    rsps.get("https://www.courtlistener.com/api/rest/v4/search/", json={"results": []})
    result = process(config, conn, 1)
    summary = result.summary_json
    assert summary["court_status"] == "primary_record"
    assert summary["classification"]["kind"] == "federal"  # type: ignore[index]
    record = conn.execute(
        "SELECT source_kind, court, case_number, document_kind, strategy FROM court_records"
    ).fetchone()
    assert record == (
        "federal_docket",
        "District Court, D. Connecticut",
        "3:13-cr-00045",
        "docket",
        "court.courtlistener_bulk",
    )
    modules = {m["name"] for m in summary["modules"]}  # type: ignore[index, union-attr]
    assert "court.courtlistener_api" not in modules  # the docket search never ran
    # the only REST call is the RECAP documents lookup for the docket the index found
    rest = [c.request.url for c in rsps.calls if "courtlistener.com/api" in c.request.url]
    assert len(rest) == 1 and "docket_id=1" in rest[0]
    ident = conn.execute(
        "SELECT kind, value, span FROM identifiers WHERE kind = 'case_number'"
    ).fetchone()
    assert ident[1] == "3:13-cr-00045" and ident[2] == "3:13-cr-00045"
    evidence = conn.execute(
        "SELECT tool, headers_json FROM captures WHERE tool = 'courtlistener_bulk_row'"
    ).fetchone()
    assert json.loads(evidence[1])["dataset_date"] == "2026-09-30"  # provenance: which quarter


def test_the_bulk_module_is_unavailable_until_the_index_is_loaded(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    add_court_row(conn, 1)
    rsps.get("https://www.justice.gov/api/v1/press_releases.json", json={"results": []})
    summary = process(config, conn, 1).summary_json
    reasons = {m["name"]: m["reason"] for m in summary["modules"]}  # type: ignore[index, union-attr]
    assert "not loaded" in reasons["court.courtlistener_bulk"]


# --- downloading ----------------------------------------------------------------------------------


def test_a_download_streams_to_disk_hashes_it_and_records_a_manifest_not_the_file(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    rsps.get("https://bulk.example.test/robots.txt", status=404)
    _ctx, cap, _ = make_ctx(config, conn)
    payload = b"id,name\n" + b"x" * 5000
    url = "https://bulk.example.test/bulk-data/parties-2026-09-30.csv"
    rsps.get(url, body=payload, headers={"ETag": '"abc"'})
    dest = config.paths.bulk / "courtlistener" / "parties-2026-09-30.csv"
    record = cap.download(url, dest, stage="bulk", expected_size=len(payload))
    cap.close()
    assert dest.read_bytes() == payload and not dest.with_name(dest.name + ".part").exists()
    manifest = (
        json.loads(cap.read_body(record))
        if False
        else json.loads(
            conn.execute(
                "SELECT headers_json FROM captures WHERE capture_id = ?", (record.capture_id,)
            ).fetchone()[0]
        )
    )
    assert manifest["sha256"] == hashlib.sha256(payload).hexdigest() and manifest["etag"] == '"abc"'
    assert record.tool == "bulk_download" and record.content_type == "application/json"
    store_hits = [
        p for p in (config.paths.store).rglob("*") if p.is_file() and p.stat().st_size >= 5000
    ]
    assert not store_hits  # the 5 KB file itself is not in the content store


def test_an_interrupted_download_resumes_from_the_part_file(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    rsps.get("https://bulk.example.test/robots.txt", status=404)
    _ctx, cap, _ = make_ctx(config, conn)
    payload = bytes(range(256)) * 40
    url = "https://bulk.example.test/bulk-data/courts-2026-09-30.csv"
    dest = config.paths.bulk / "courtlistener" / "courts-2026-09-30.csv"
    dest.parent.mkdir(parents=True)
    dest.with_name(dest.name + ".part").write_bytes(payload[:3000])
    dest.with_name(dest.name + ".part.etag").write_text('"v1"')  # the validator it began under
    rsps.get(
        url,
        body=payload[3000:],
        status=206,
        headers={"Content-Range": f"bytes 3000-{len(payload) - 1}/{len(payload)}"},
    )
    cap.download(url, dest, stage="bulk", expected_size=len(payload))
    cap.close()
    assert dest.read_bytes() == payload
    assert rsps.calls[-1].request.headers["Range"] == "bytes=3000-"


def test_a_wrong_size_is_an_error_and_leaves_the_part_file(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    rsps.get("https://bulk.example.test/robots.txt", status=404)
    _ctx, cap, _ = make_ctx(config, conn)
    url = "https://bulk.example.test/bulk-data/courts-2026-09-30.csv"
    rsps.get(url, body=b"short")
    dest = config.paths.bulk / "courtlistener" / "courts-2026-09-30.csv"
    with pytest.raises(CaptureError, match="expected"):
        cap.download(url, dest, stage="bulk", expected_size=999)
    cap.close()
    assert not dest.exists()


def test_the_size_guard_refuses_a_download_over_the_configured_limit(
    config: Config, conn: sqlite3.Connection
) -> None:
    ctx, cap, _ = make_ctx(config, conn)
    files = {
        "dockets": bulk.BulkFile(
            "dockets",
            "2026-09-30",
            "bulk-data/dockets-2026-09-30.csv.bz2",
            5_000_000_000,
            "",
            "bz2",
        )
    }
    with pytest.raises(CaptureError, match=r"court\.bulk_max_gb"):
        bulk.fetch_files(ctx, files, max_gb=1)
    cap.close()


def _tiny_index(config: Config) -> None:
    target = bulk.index_path(config)
    target.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(target))
    con.execute(
        "CREATE TABLE courts (court_id VARCHAR, full_name VARCHAR, short_name VARCHAR, "
        "jurisdiction VARCHAR, state VARCHAR);"
        "CREATE TABLE dockets (docket_id BIGINT, court_id VARCHAR, docket_number VARCHAR, "
        "case_name VARCHAR, date_filed DATE, date_terminated DATE, pacer_case_id VARCHAR, "
        "absolute_url VARCHAR);"
        "CREATE TABLE docket_parties (docket_id BIGINT, party_id BIGINT);"
        "CREATE TABLE parties (party_id BIGINT, name VARCHAR);"
        "CREATE TABLE meta (key VARCHAR, value VARCHAR);"
        "INSERT INTO courts VALUES ('ctd', 'District Court, D. Connecticut', 'D. Conn.', 'FD', 'CT');"
        "INSERT INTO dockets VALUES "
        "(1, 'ctd', '3:13-cr-1', 'United States v. Pat Lane', '2013-01-01', NULL, '1', '/d/1/'),"
        "(2, 'ctd', '3:13-cr-2', 'Sealed matter', '2013-02-01', NULL, '2', '/d/2/'),"
        "(3, 'ctd', '3:13-cr-3', 'United States v. Quinn Other', '2013-03-01', NULL, '3', '/d/3/');"
        "INSERT INTO parties VALUES (10, 'Pat Lane'), (11, 'Sam Roe'), (12, 'Quinn Other');"
        "INSERT INTO docket_parties VALUES (2, 10), (2, 11), (3, 12);"
        "INSERT INTO meta VALUES ('dataset_date', '2026-09-30');"
    )
    con.close()


def test_the_surname_prefilter_finds_a_case_name_match_and_a_party_only_match(
    config: Config,
) -> None:
    _tiny_index(config)
    hits = bulk.search_dockets(config, parse_name("Pat Lane"), state="CT", year=2014)
    assert sorted(h.docket_number for h in hits) == ["3:13-cr-1", "3:13-cr-2"]  # not Quinn Other
    by_number = {h.docket_number: h for h in hits}
    assert "case name" in by_number["3:13-cr-1"].matched_on
    assert "party name" in by_number["3:13-cr-2"].matched_on  # the case name does not carry it
    roe = bulk.search_dockets(config, parse_name("Sam Roe"), state="CT", year=2014)
    assert [h.docket_number for h in roe] == ["3:13-cr-2"]  # one row per docket, not per party


def test_a_weak_only_bulk_result_is_a_miss_kept_as_candidates_not_a_primary_record(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    # two dockets for one name in one state: neither can be settled, so every match is weak
    _tiny_index(config)
    add_court_row(conn, 1, name="Pat Lane", details="Charged in U.S. District Court")
    rsps.get("https://www.justice.gov/robots.txt", status=404)
    rsps.get("https://www.justice.gov/api/v1/press_releases.json", json={"results": []})
    rsps.get("https://www.courtlistener.com/api/rest/v4/search/", json={"results": []})
    summary = process(config, conn, 1).summary_json
    statuses = {m["name"]: m["status"] for m in summary["modules"]}  # type: ignore[index, union-attr]
    assert statuses["court.courtlistener_bulk"] == "miss"  # the chain was not stopped by it
    assert summary["court_status"] == "candidate_only"  # type: ignore[index]
    flags = conn.execute("SELECT weak, basis_json FROM court_records").fetchall()
    assert len(flags) == 2 and all(weak == 1 and json.loads(basis) for weak, basis in flags)


@pytest.mark.parametrize("junk", ["", "N/A", "Unknown", "Multiple", "XX"])
def test_an_unrecognised_state_is_unknown_not_a_filter_that_matches_nothing(
    config: Config, loaded: bulk.LoadResult, junk: str
) -> None:
    unfiltered = bulk.search_dockets(config, parse_name("Alex Example"), state=None, year=2014)
    assert {h.state for h in unfiltered} == {"CT", "NY"}
    junked = bulk.search_dockets(config, parse_name("Alex Example"), state=junk, year=2014)
    assert [h.docket_id for h in junked] == [h.docket_id for h in unfiltered]


def test_a_full_state_name_filters_like_its_abbreviation(
    config: Config, loaded: bulk.LoadResult
) -> None:
    hits = bulk.search_dockets(config, parse_name("Alex Example"), state="connecticut", year=2014)
    assert [h.docket_number for h in hits] == ["3:13-cr-00045"]


@pytest.mark.parametrize("junk", ["N/A", "Unknown", ""])
def test_a_row_with_an_unrecognised_state_gets_only_weak_matches(
    config: Config,
    conn: sqlite3.Connection,
    loaded: bulk.LoadResult,
    rsps: responses.RequestsMock,
    junk: str,
) -> None:
    add_court_row(conn, 1, state=junk, details="Charged in U.S. District Court")
    rsps.get("https://www.justice.gov/robots.txt", status=404)
    rsps.get("https://www.justice.gov/api/v1/press_releases.json", json={"results": []})
    rsps.get("https://www.courtlistener.com/api/rest/v4/search/", json={"results": []})
    summary = process(config, conn, 1).summary_json
    assert summary["court_status"] == "candidate_only"  # type: ignore[index]
    assert conn.execute("SELECT count(*) FROM court_records WHERE weak = 0").fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM court_records WHERE weak = 1").fetchone()[0] >= 2


def test_an_old_docket_with_no_termination_date_is_not_in_a_later_rows_window(
    config: Config, loaded: bulk.LoadResult
) -> None:
    con = bulk.duckdb.connect(str(bulk.index_path(config)))
    con.execute(
        "INSERT INTO dockets VALUES (501, 'ctd', '3:03-cr-00001', 'United States v. Example', '2003-02-01', NULL, '501', '/d/501/')"
    )
    con.execute(
        "INSERT INTO dockets VALUES (502, 'ctd', '3:19-cr-00002', 'United States v. Example', NULL, NULL, '502', '/d/502/')"
    )
    con.execute("INSERT INTO parties VALUES (7001, 'Alex Example')")
    con.execute("INSERT INTO docket_parties VALUES (501, 7001)")
    con.execute("INSERT INTO docket_parties VALUES (502, 7001)")
    con.close()
    hits = bulk.search_dockets(config, parse_name("Alex Example"), state="CT", year=2020)
    numbers = {h.docket_number for h in hits}
    assert "3:03-cr-00001" not in numbers  # filed 17 years before: a different case
    assert "3:19-cr-00002" in numbers  # no filing date: kept, but only as a candidate
    from efp.court.located import docket_is_weak

    unknown = next(h for h in hits if h.docket_number == "3:19-cr-00002")
    assert docket_is_weak(
        row_state="CT",
        court_state="CT",
        candidates=1,
        initial_only=False,
        date_known=bool(unknown.date_filed),
    )


def test_a_courts_file_whose_jurisdiction_codes_match_nothing_stops_the_load(
    config: Config, tmp_path: Path
) -> None:
    paths = write_bulk_files(tmp_path / "odd", compress=False)
    lines = paths["courts"].read_text().splitlines()
    odd = [lines[0]] + [",".join([*line.split(",")[:-1], "ZZ"]) for line in lines[1:]]
    paths["courts"].write_text("\n".join(odd) + "\n")  # every code is one the loader never saw
    with pytest.raises(bulk.BulkLayoutError, match="federal filter"):
        bulk.load_index(config, paths, dataset_date="x", manifest_captures={})
    assert not bulk.index_path(config).exists()  # and no index of every court's dockets


def test_short_rows_are_counted_not_silently_dropped(config: Config, tmp_path: Path) -> None:
    paths = write_bulk_files(tmp_path / "short", compress=False)
    with paths["dockets"].open("a") as handle:
        handle.write("999999,ctd\\n".replace("\\n", "\n"))  # truncated: two fields, not eight
    result = bulk.load_index(config, paths, dataset_date="x", manifest_captures={})
    assert result.bad_rows == 1
    info = bulk.index_info(config)
    assert info is not None

"""Downloads, the refusal and User-Agent rules, manual import, the enrichment-only API clients and
the Ballotpedia lookup. HTTP is mocked; nothing here is real data."""

from __future__ import annotations

import dataclasses
import inspect
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import responses

from efp.config import Config, load_config
from efp.ratelimit import DomainLimiter
from efp.reference import ballotpedia, ops
from efp.reference.api import ApiUnavailable, ConfirmedMatch, congress, openfec, openstates
from tests import reference_helpers as h

BROWSER = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130 Safari/537.36"
CENSUS = "https://www2.census.gov/topics/genealogy/2010surnames/names.zip"
SSA = "https://www.ssa.gov/oact/babynames/names.zip"
REJECTED = "<html><head><title>Request Rejected</title></head><body>Rejected</body></html>"


@pytest.fixture
def rsps() -> Iterator[responses.RequestsMock]:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield mock


def make(write_config: Callable[..., Path], **swap: str) -> Config:
    return load_config(write_config(swap))


def with_ua(
    write_config: Callable[..., Path], hosts: str = '["www2.census.gov", "www.ssa.gov"]'
) -> Config:
    return make(
        write_config,
        **{
            'download_user_agent = ""': f'download_user_agent = "{BROWSER}"',
            'download_user_agent_hosts = ["www2.census.gov", "www.ssa.gov"]': f"download_user_agent_hosts = {hosts}",
        },
    )


def gets(rsps: responses.RequestsMock, url: str) -> list[Any]:
    return [c.request for c in rsps.calls if c.request.url == url and c.request.method == "GET"]


# --- downloads ----------------------------------------------------------------------------------------


def test_a_redirected_download_is_followed_hop_by_hop_and_recorded_as_a_reference_capture(
    write_config: Callable[..., Path], conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    config = make(
        write_config,
        **{
            "fec_first_cycle = 2000": "fec_first_cycle = 2024",
            "fec_last_cycle = 2026": "fec_last_cycle = 2024",
        },
    )
    ctx, cap = h.make_ctx(config, conn)
    zipped = h.zip_bytes({"cn.txt": h.FEC_CN})
    url = "https://www.fec.gov/files/bulk-downloads/2024/cn24.zip"
    bucket = "https://bucket.example.test/bulk-downloads/2024/cn24.zip"
    rsps.head(url, status=302, headers={"Location": bucket})
    rsps.head(bucket, status=200, headers={"Content-Length": str(len(zipped))})
    rsps.get(url, status=302, headers={"Location": bucket})
    rsps.get(bucket, body=zipped, content_type="application/zip")
    try:
        (fetched,) = ops.download(ctx, ["fec_candidates"])["fec_candidates"]
        assert fetched.status == "ok" and fetched.size == len(zipped)
        row = conn.execute(
            "SELECT stage, row_id, tool, url_requested FROM captures WHERE capture_id = ?",
            (fetched.capture_id,),
        ).fetchone()
        assert row == ("reference", None, "bulk_download", bucket)
        (loaded,) = ops.load(ctx, ["fec_candidates"])["fec_candidates"]
        assert (loaded.status, loaded.rows) == ("loaded", 4)
        assert (
            conn.execute("SELECT capture_id FROM ref_sources WHERE status='loaded'").fetchone()[0]
            == fetched.capture_id
        )
    finally:
        cap.close()


def test_a_second_download_of_the_same_file_makes_no_request(
    write_config: Callable[..., Path], conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    config = make(
        write_config,
        **{
            "fec_first_cycle = 2000": "fec_first_cycle = 2024",
            "fec_last_cycle = 2026": "fec_last_cycle = 2024",
        },
    )
    ctx, cap = h.make_ctx(config, conn)
    zipped = h.zip_bytes({"cn.txt": h.FEC_CN})
    url = "https://www.fec.gov/files/bulk-downloads/2024/cn24.zip"
    rsps.head(url, status=200, headers={"Content-Length": str(len(zipped))})
    rsps.get(url, body=zipped)
    try:
        assert ops.download(ctx, ["fec_candidates"])["fec_candidates"][0].status == "ok"
        before = len(rsps.calls)
        again = ops.download(ctx, ["fec_candidates"])["fec_candidates"][0]
        assert again.status == "ok" and again.from_cache and len(rsps.calls) == before
    finally:
        cap.close()


def test_a_rejection_page_is_gated_and_without_a_configured_user_agent_nothing_else_is_tried(
    write_config: Callable[..., Path], conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    config = make(write_config)  # download_user_agent is empty by default
    ctx, cap = h.make_ctx(config, conn)
    rsps.head(CENSUS, status=200, content_type="text/html")
    rsps.get(CENSUS, body=REJECTED, content_type="text/html")
    try:
        (fetched,) = ops.download(ctx, ["census_surnames"])["census_surnames"]
    finally:
        cap.close()
    assert fetched.status == "gated" and "web page" in fetched.reason
    assert len(gets(rsps, CENSUS)) == 1 and not fetched.ua_override
    assert not (config.paths.bulk / "census_surnames" / "names.zip").exists()  # never kept as data
    assert conn.execute(
        "SELECT status, current FROM ref_sources WHERE dataset='census_surnames'"
    ).fetchone() == ("gated", 0)


def test_the_browser_user_agent_is_used_once_after_a_refusal_and_only_for_listed_hosts(
    write_config: Callable[..., Path], conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    config = with_ua(write_config)
    ctx, cap = h.make_ctx(config, conn)
    zipped = h.zip_bytes({"Names_2010Census.csv": h.CENSUS_CSV})

    def answer(request: Any) -> tuple[int, dict[str, str], bytes]:
        if request.headers.get("User-Agent") == BROWSER:
            return 200, {"Content-Type": "application/zip"}, zipped
        return 200, {"Content-Type": "text/html"}, REJECTED.encode()

    rsps.head(CENSUS, status=200, content_type="text/html")
    rsps.add_callback(responses.GET, CENSUS, callback=answer)
    try:
        (fetched,) = ops.download(ctx, ["census_surnames"])["census_surnames"]
    finally:
        cap.close()
    assert fetched.status == "ok" and fetched.ua_override
    first, second = gets(rsps, CENSUS)
    assert (
        first.headers["User-Agent"] == config.project.user_agent
    )  # the project identity goes first
    assert second.headers["User-Agent"] == BROWSER
    import json

    manifest = json.loads(
        conn.execute(
            "SELECT headers_json FROM captures WHERE capture_id = ?", (fetched.capture_id,)
        ).fetchone()[0]
    )
    assert manifest["headers_overridden"] == ["User-Agent"]  # the capture says how it was obtained


def test_a_host_not_in_the_list_never_gets_the_browser_user_agent(
    write_config: Callable[..., Path], conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    config = with_ua(write_config, hosts='["www.ssa.gov"]')
    ctx, cap = h.make_ctx(config, conn)
    rsps.head(CENSUS, status=200, content_type="text/html")
    rsps.get(CENSUS, body=REJECTED, content_type="text/html")
    try:
        (fetched,) = ops.download(ctx, ["census_surnames"])["census_surnames"]
    finally:
        cap.close()
    assert fetched.status == "gated" and len(gets(rsps, CENSUS)) == 1


def test_a_403_is_gated_and_the_retry_runs_only_with_the_override_configured(
    write_config: Callable[..., Path], conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    config = make(write_config)
    ctx, cap = h.make_ctx(config, conn)
    rsps.head(SSA, status=403)
    rsps.get(SSA, status=403, body="denied")
    rsps.head("https://www.ssa.gov/oact/babynames/state/namesbystate.zip", status=403)
    rsps.get("https://www.ssa.gov/oact/babynames/state/namesbystate.zip", status=403)
    try:
        results = ops.download(ctx, ["ssa_names"])["ssa_names"]
    finally:
        cap.close()
    assert [r.status for r in results] == ["gated", "gated"] and all(
        "403" in r.reason for r in results
    )
    assert len(gets(rsps, SSA)) == 1


def test_a_file_announced_above_the_size_cap_is_not_downloaded(
    write_config: Callable[..., Path], conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    config = make(write_config, **{"max_gb = 20.0": "max_gb = 0.001"})
    ctx, cap = h.make_ctx(config, conn)
    rsps.head(CENSUS, status=200, headers={"Content-Length": "5000000"})
    try:
        (fetched,) = ops.download(ctx, ["census_surnames"])["census_surnames"]
    finally:
        cap.close()
    assert fetched.status == "gated" and "max_gb" in fetched.reason and not gets(rsps, CENSUS)


def test_a_dataset_with_no_url_is_skipped_with_the_import_instruction(
    write_config: Callable[..., Path], conn: sqlite3.Connection
) -> None:
    config = make(write_config)
    ctx, cap = h.make_ctx(config, conn)
    try:
        (dime,) = ops.download(ctx, ["dime"])["dime"]
    finally:
        cap.close()
    assert dime.status == "skipped" and "efp reference import dime" in dime.reason


# --- manual import ---------------------------------------------------------------------------------------


def test_a_file_downloaded_by_hand_is_checked_recorded_and_loaded(
    write_config: Callable[..., Path], conn: sqlite3.Connection, tmp_path: Path
) -> None:
    config = make(write_config)
    ctx, cap = h.make_ctx(config, conn)
    mine = h.write(
        tmp_path / "from_browser" / "names.zip", h.zip_bytes({"Names_2010Census.csv": h.CENSUS_CSV})
    )
    try:
        fetched, loaded = ops.import_by_hand(ctx, "census_surnames", mine)
    finally:
        cap.close()
    assert fetched.status == "ok" and [(r.status, r.rows) for r in loaded] == [("loaded", 3)]
    row = conn.execute(
        "SELECT tool, manual_source, stage, row_id FROM captures WHERE capture_id = ?",
        (fetched.capture_id,),
    ).fetchone()
    assert row == ("manual_import", "reference", "reference", None)
    assert (config.paths.bulk / "census_surnames" / "names.zip").is_file()


def test_importing_a_web_page_or_an_unknown_part_is_refused(
    write_config: Callable[..., Path], conn: sqlite3.Connection, tmp_path: Path
) -> None:
    config = make(write_config)
    ctx, cap = h.make_ctx(config, conn)
    page = h.write(tmp_path / "names.zip", REJECTED)
    ssa = h.write(tmp_path / "something.zip", h.zip_bytes(h.SSA_NATIONAL))
    try:
        assert ops.import_by_hand(ctx, "census_surnames", page)[0].status == "failed"
        refused, _ = ops.import_by_hand(ctx, "ssa_names", ssa)  # two parts: which one is it?
        assert refused.status == "failed" and "--part" in refused.reason
        ok, loaded = ops.import_by_hand(ctx, "ssa_names", ssa, "national")
        assert ok.status == "ok" and loaded[0].part == "national"
        with pytest.raises(ops.UnknownDataset):
            ops.import_by_hand(ctx, "nope", ssa)
    finally:
        cap.close()


# --- the enrichment-only API clients --------------------------------------------------------------------


@pytest.fixture
def keyed(env_file: Path) -> Path:
    with env_file.open("a", encoding="utf-8") as handle:
        handle.write(  # pragma: allowlist secret
            "CONGRESS_API_KEY=congress-test-key\nOPENSTATES_KEY=openstates-test-key\n"  # pragma: allowlist secret
        )
    return env_file


def test_no_client_can_be_called_with_a_name() -> None:
    for module in (openfec, congress, openstates):
        params = list(inspect.signature(module.enrich).parameters)
        assert params == ["ctx", "match", "row_id"]
    assert {f.name for f in dataclasses.fields(ConfirmedMatch)} == {
        "fec_candidate_id",
        "bioguide_id",
        "openstates_id",
    }  # identifiers only: nothing to search by name with


def test_the_budgets_are_eighty_percent_of_the_published_limits() -> None:
    repo = Path(__file__).resolve().parents[1]
    for name in ("config.toml", "config.example.toml"):
        check_budgets(load_config(repo / name))


def check_budgets(config: Config) -> None:
    limiter = DomainLimiter(config)
    try:
        assert limiter.budget("api.open.fec.gov").rps <= 0.8 * 1000 / 3600 + 0.0001
        assert limiter.budget("api.congress.gov").rps <= 0.8 * 5000 / 3600 + 0.0001
        assert limiter.budget("v3.openstates.org").rps == 0.5
    finally:
        limiter.close()


def test_openfec_enriches_a_confirmed_id_through_the_capture_layer(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    ctx, cap = h.make_ctx(config, conn)
    rsps.get(
        "https://api.open.fec.gov/v1/candidate/H0CT01001/",
        json={
            "results": [
                {
                    "candidate_id": "H0CT01001",
                    "name": "SMITH, JOHN Q",
                    "party_full": "DEMOCRATIC PARTY",
                    "office_full": "House",
                    "state": "CT",
                    "district": "01",
                    "election_years": [2012, 2014],
                    "incumbent_challenge_full": "Incumbent",
                }
            ]
        },
    )
    try:
        got = openfec.enrich(ctx, ConfirmedMatch(fec_candidate_id="H0CT01001"))
        assert got["party"] == "DEMOCRATIC PARTY" and got["election_years"] == [2012, 2014]
        assert openfec.enrich(ctx, ConfirmedMatch()) == {}  # no ID: nothing to enrich, no call
        assert len(rsps.calls) == 1
        stored = conn.execute(
            "SELECT stage, url_requested, tool FROM captures WHERE stage='reference'"
        ).fetchone()
        assert stored[0] == "reference" and "SENTINEL" not in stored[1]  # the key is never stored
    finally:
        cap.close()


def test_congress_and_openstates_enrich_by_id_with_their_keys(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock, keyed: Path
) -> None:
    ctx, cap = h.make_ctx(config, conn)
    rsps.get(
        "https://api.congress.gov/v3/member/X999001",
        json={
            "member": {
                "bioguideId": "X999001",
                "directOrderName": "John Q. Smith",
                "birthYear": "1960",
                "partyHistory": [{"partyName": "Democratic"}],
                "terms": {"item": [{"chamber": "House"}]},
            }
        },
    )
    rsps.get(
        "https://v3.openstates.org/people",
        json={
            "results": [
                {
                    "id": "ocd-person/aaa",
                    "name": "John Smith",
                    "party": "Democratic",
                    "current_role": {
                        "title": "Representative",
                        "district": "12",
                        "org_classification": "lower",
                    },
                    "jurisdiction": {"name": "Connecticut"},
                    "other_names": [{"name": "Johnny Smith"}],
                }
            ]
        },
    )
    try:
        member = congress.enrich(ctx, ConfirmedMatch(bioguide_id="X999001"))
        assert member["name"] == "John Q. Smith" and member["terms"] == [{"chamber": "House"}]
        person = openstates.enrich(ctx, ConfirmedMatch(openstates_id="ocd-person/aaa"))
        assert person["district"] == "12" and person["other_names"] == ["Johnny Smith"]
        assert rsps.calls[1].request.headers["X-API-KEY"] == "openstates-test-key"
    finally:
        cap.close()


def test_a_missing_key_or_a_refusal_is_api_unavailable_not_a_crash(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    ctx, cap = h.make_ctx(config, conn)  # the fixture env file has no CONGRESS_API_KEY
    rsps.get("https://api.open.fec.gov/v1/candidate/X/", status=403, json={})
    try:
        with pytest.raises(ApiUnavailable, match="CONGRESS_API_KEY is not set"):
            congress.enrich(ctx, ConfirmedMatch(bioguide_id="X999001"))
        with pytest.raises(ApiUnavailable, match="HTTP 403"):
            openfec.enrich(ctx, ConfirmedMatch(fec_candidate_id="X"))
    finally:
        cap.close()


# --- Ballotpedia ---------------------------------------------------------------------------------------------

PAGE = """<html><body><h1>John Example</h1>
<div class="infobox person"><div class="office-title">U.S. House Connecticut District 1</div>
<p>Democratic Party</p><p>Assumed office: 2005</p><p>Left office: 2013</p><p>Connecticut</p></div></body></html>"""


def test_ballotpedia_page_is_read_for_office_party_and_years(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    ctx, cap = h.make_ctx(config, conn)
    rsps.get("https://ballotpedia.org/John_Example", body=PAGE, content_type="text/html")
    rsps.get("https://ballotpedia.org/Nobody_Here", status=404)
    try:
        page = ballotpedia.lookup(ctx, "John Example", "CT")
        missing = ballotpedia.lookup(ctx, "Nobody Here", "CT")
    finally:
        cap.close()
    assert (
        page.found
        and page.party == "Democratic"
        and page.years == [2005, 2013]
        and page.state == "CT"
    )
    assert page.offices == ["U.S. House Connecticut District 1"] and page.capture_id
    assert not missing.found and missing.reason == "HTTP 404"
    assert ballotpedia.parse("<html><h1>X</h1>does not have an article</html>", "u").found is False


# --- review round 1 -------------------------------------------------------------------------------------------


def test_a_refused_refresh_never_touches_the_good_file_on_disk(
    write_config: Callable[..., Path], conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    config = make(write_config)
    ctx, cap = h.make_ctx(config, conn)
    good = h.zip_bytes({"Names_2010Census.csv": h.CENSUS_CSV})
    path = h.place(config, "census_surnames", "names.zip", good)
    rsps.head(CENSUS, status=200, content_type="text/html")
    rsps.get(CENSUS, body=REJECTED, content_type="text/html")
    try:
        (fetched,) = ops.download(ctx, ["census_surnames"], refresh=True)["census_surnames"]
        assert fetched.status == "gated"
        assert path.read_bytes() == good  # the earlier copy survived a refused refresh
        assert not path.with_name("names.zip.new").exists()
        rsps.replace(responses.GET, CENSUS, status=403, body="denied")
        assert (
            ops.download(ctx, ["census_surnames"], refresh=True)["census_surnames"][0].status
            == "gated"
        )
        assert path.read_bytes() == good
        (loaded,) = ops.load(ctx, ["census_surnames"])["census_surnames"]
        assert loaded.status == "loaded"  # and it still loads
    finally:
        cap.close()


def test_the_browser_user_agent_does_not_follow_a_redirect_to_an_unlisted_host(
    write_config: Callable[..., Path], conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    config = with_ua(write_config)
    ctx, cap = h.make_ctx(config, conn)
    elsewhere = "https://bucket.example.test/names.zip"
    rsps.head(CENSUS, status=200, content_type="text/html")
    rsps.get(CENSUS, body=REJECTED, content_type="text/html")  # the listed host refuses
    rsps.get(elsewhere, body=REJECTED, content_type="text/html")
    try:
        # an unlisted address is never sent the override, whatever redirected there
        from efp.reference import files

        assert files._ua_allowed_for(config, CENSUS) and not files._ua_allowed_for(
            config, elsewhere
        )
        (fetched,) = ops.download(ctx, ["census_surnames"])["census_surnames"]
    finally:
        cap.close()
    assert fetched.status == "gated"
    assert all(
        c.request.headers["User-Agent"] != BROWSER
        for c in rsps.calls
        if "bucket.example" in c.request.url
    )


def test_only_a_refusal_gets_the_user_agent_retry_not_a_server_error(
    write_config: Callable[..., Path], conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    from efp.reference import files

    assert files._is_refusal("https://x/names.zip: HTTP 403") and files._is_refusal(
        "refused: the host returned a web page"
    )
    assert not files._is_refusal("https://x/names.zip: HTTP 503") and not files._is_refusal(
        "ReadTimeout: slow"
    )


def test_resolving_a_url_logs_each_hop(
    write_config: Callable[..., Path],
    conn: sqlite3.Connection,
    rsps: responses.RequestsMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = make(write_config)
    _ctx, cap = h.make_ctx(config, conn)
    rsps.head(
        "https://www.fec.gov/a.zip",
        status=302,
        headers={"Location": "https://bucket.example.test/a.zip"},
    )
    rsps.head("https://bucket.example.test/a.zip", status=200, headers={"Content-Length": "5"})
    try:
        with caplog.at_level("INFO"):
            resolved = cap.resolve_url("https://www.fec.gov/a.zip")
    finally:
        cap.close()
    assert resolved.url == "https://bucket.example.test/a.zip" and resolved.size == 5
    heads = [r.getMessage() for r in caplog.records if r.getMessage().startswith("head ")]
    assert len(heads) == 2 and "status=302" in heads[0] and "status=200" in heads[1]


def test_an_api_key_is_in_no_capture_not_the_warc_not_the_cache(
    config: Config, conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    from tests.capturehelpers import warc_records

    ctx, cap = h.make_ctx(config, conn)
    rsps.get(
        "https://api.open.fec.gov/v1/candidate/H0CT01001/",
        json={"results": [{"candidate_id": "H0CT01001"}]},
    )
    try:
        openfec.enrich(ctx, ConfirmedMatch(fec_candidate_id="H0CT01001"))
    finally:
        cap.close()
    blob = repr(warc_records(config)) + repr(conn.execute("SELECT * FROM captures").fetchall())
    assert "SENTINEL-fec" not in blob
    cache = config.paths.cache
    for path in cache.rglob("*") if cache.exists() else []:
        if path.is_file():
            assert b"SENTINEL-fec" not in path.read_bytes()


def test_a_presigned_url_credentials_are_never_stored_and_the_recorded_source_has_no_query(
    write_config: Callable[..., Path], conn: sqlite3.Connection, rsps: responses.RequestsMock
) -> None:
    from efp.capture import redact_url

    signed = (
        "https://objects.example.test/file?response-content-disposition=attachment"
        "&X-Amz-Credential=AKIAEXAMPLE%2F2026%2Fs3&X-Amz-Signature=deadbeefcafe&X-Amz-Expires=60"
    )
    stored = redact_url(signed, api=True)
    assert "deadbeefcafe" not in stored and "AKIAEXAMPLE" not in stored
    assert "X-Amz-Expires=60" in stored  # only the credentials go

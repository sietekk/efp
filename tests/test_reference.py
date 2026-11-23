"""Reference loaders (build 11): each dataset in its real file format, provenance, the candidate
index, the surname floor, and `efp selftest --reference`. Synthetic files only."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from efp.config import Config
from efp.reference import DATASET_CLASSES, datasets, index
from efp.reference.checks import reference_checks
from efp.reference.names import split_name, surname_key
from tests import reference_helpers as h


@pytest.fixture
def ctx(config: Config, conn: sqlite3.Connection):  # type: ignore[no-untyped-def]
    context, cap = h.make_ctx(config, conn)
    yield context
    cap.close()


def loaded(ctx, name: str):  # type: ignore[no-untyped-def]
    return datasets()[name].load(ctx)


def rows(conn: sqlite3.Connection, sql: str) -> list[tuple[object, ...]]:
    return conn.execute(sql).fetchall()


# --- the population prior ---------------------------------------------------------------------------


def test_census_surnames_load_with_the_floor_and_the_other_names_row_set_aside(
    ctx, config, conn
) -> None:  # type: ignore[no-untyped-def]
    h.place(
        config, "census_surnames", "names.zip", h.zip_bytes({"Names_2010Census.csv": h.CENSUS_CSV})
    )
    (result,) = loaded(ctx, "census_surnames")
    assert (result.status, result.rows) == ("loaded", 3)  # ALL OTHER NAMES is not a surname
    assert rows(
        conn, "SELECT surname, count, prop100k FROM ref_surname_freq_current ORDER BY rank DESC"
    )[-1] == ("SMITH", 2442977, 828.19)
    notes = json.loads(conn.execute("SELECT notes_json FROM ref_sources").fetchone()[0])
    # the floor: half of the smallest listed count (100 -> 50) over the file's population
    population = round(2442977 * 100_000 / 828.19)
    assert notes["floor_prop100k"] == pytest.approx(50 / population * 100_000, rel=1e-4)
    assert notes["floor_rule"] == "half the smallest listed proportion"
    assert notes["all_other_names_count"] == 29312001 and notes["smallest_listed_count"] == 100


def test_ssa_national_and_state_names(ctx, config, conn) -> None:  # type: ignore[no-untyped-def]
    h.place(config, "ssa_names", "names.zip", h.zip_bytes(h.SSA_NATIONAL))
    h.place(config, "ssa_names", "namesbystate.zip", h.zip_bytes(h.SSA_STATE))
    by_part = {r.part: r for r in loaded(ctx, "ssa_names")}
    assert (by_part["national"].rows, by_part["state"].rows) == (5, 3)  # readme members ignored
    assert rows(
        conn,
        "SELECT count FROM ref_given_freq_current WHERE name='Mary' AND sex='F' AND year=1880 AND state IS NULL",
    ) == [(7065,)]
    assert rows(
        conn, "SELECT count FROM ref_given_freq_current WHERE name='Mary' AND state='CT'"
    ) == [(300,)]
    assert {r[0] for r in rows(conn, "SELECT DISTINCT state FROM ref_given_freq_current")} == {
        None,
        "CT",
        "AK",
    }


def test_census_population_is_banded_by_five_years_for_states_only(ctx, config, conn) -> None:  # type: ignore[no-untyped-def]
    h.place(config, "census_population", "state_agesex.csv", h.POPULATION_CSV)
    (result,) = loaded(ctx, "census_population")
    assert result.status == "loaded"
    got = dict(
        ((s, b, y), c)
        for s, b, y, c in rows(
            conn, "SELECT state, age_band, year, count FROM ref_population_current"
        )
    )
    assert got[("CT", "0-4", 2020)] == 1000 + 1000 + 900  # ages 0, 3, 4; sex 0 only
    assert got[("CT", "0-4", 2023)] == 1100 + 1100 + 800
    assert got[("CT", "5-9", 2023)] == 600 and got[("CT", "85+", 2023)] == 50
    assert {k[0] for k in got} == {"CT"}  # the national row and the age-999 total are not states


# --- the candidate universes ------------------------------------------------------------------------


def test_fec_candidates_names_are_split_and_the_cycle_is_the_files(ctx, config, conn) -> None:  # type: ignore[no-untyped-def]
    h.place(config, "fec_candidates", "cn24.zip", h.zip_bytes({"cn.txt": h.FEC_CN}))
    results = [r for r in loaded(ctx, "fec_candidates") if r.status == "loaded"]
    assert [r.part for r in results] == ["2024"] and results[0].rows == 4
    smith = conn.execute(
        "SELECT surname, given, middle, surname_key, party, state, office, cycle FROM ref_fec_candidates WHERE cand_id='H0CT01001'"
    ).fetchone()
    assert smith == ("SMITH", "JOHN", "Q", "SMITH", "DEM", "CT", "H", 2024)


def test_congress_crosswalk_and_terms(ctx, config, conn) -> None:  # type: ignore[no-untyped-def]
    h.place(config, "congress", "legislators-current.yaml", h.CONGRESS_YAML)
    (result,) = [r for r in loaded(ctx, "congress") if r.part == "current"]
    assert result.rows == 2
    row = conn.execute(
        "SELECT fec_ids, opensecrets, name_full, surname, given, first_year, last_year, state, party "
        "FROM ref_congress WHERE bioguide='X999001'"
    ).fetchone()
    assert json.loads(row[0]) == ["H0CT01001", "S0CT00099"]
    assert row[1:] == ("N0000001", "John Q. Smith", "Smith", "John", 2005, 2013, "CT", "Democrat")


def test_openstates_reads_legislature_and_retired_only(ctx, config, conn) -> None:  # type: ignore[no-untyped-def]
    h.place(config, "openstates", "people-main.zip", h.zip_bytes(h.OPENSTATES))
    (result,) = loaded(ctx, "openstates")
    assert result.rows == 2  # the executive file and the readme are not legislators
    got = rows(
        conn,
        "SELECT openstates_id, state, chamber, party, first_year, last_year, retired FROM ref_state_leg ORDER BY 1",
    )
    assert got == [
        ("ocd-person/aaa", "CT", "lower", "Democratic", 2015, 2019, 0),
        ("ocd-person/bbb", "NY", "upper", "Republican", 2003, 2011, 1),
    ]


def test_harvard_local_party_is_always_flagged_imputed_when_it_is_the_databases_estimate(
    ctx, config, conn
) -> None:  # type: ignore[no-untyped-def]
    h.place(config, "harvard_local", "local_elections.csv", h.HARVARD_CSV)
    (result,) = loaded(ctx, "harvard_local")
    assert result.rows == 2  # the row with no name is skipped
    got = rows(
        conn,
        "SELECT record_id, surname, given, state, office, party, party_imputed, year FROM ref_local ORDER BY 1",
    )
    assert got == [
        ("1", "example", "gil", "CA", "Prosecutor", "D", 1, 1992),
        ("2", "sample", "pat", "IL", "Sheriff", "R", 1, 2018),
    ]


def test_dime_reads_a_sqlite_build_and_a_csv(ctx, config, conn, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    h.dime_sqlite(tmp_path / "dime.sqlite")
    h.place(config, "dime", "dime.sqlite", (tmp_path / "dime.sqlite").read_bytes())
    (result,) = loaded(ctx, "dime")
    assert result.status == "loaded" and result.rows == 3
    got = rows(
        conn,
        "SELECT bonica_rid, cycle, surname, state, party, cfscore FROM ref_dime ORDER BY cycle",
    )
    assert got[0] == ("cand1", 2012, "smith", "CT", "100", -0.7)


def test_ftm_csv(ctx, config, conn) -> None:  # type: ignore[no-untyped-def]
    h.place(config, "ftm", "ftm_candidates.csv", h.FTM_CSV)
    (result,) = loaded(ctx, "ftm")
    assert result.rows == 2
    got = rows(
        conn, "SELECT ftm_id, surname, given, state, office, year, won FROM ref_ftm ORDER BY 1"
    )
    assert got == [
        ("F1", "DOE", "JANE", "NY", "State Senate", 2018, 1),
        ("F2", "SAMPLE", "PAT", "IL", "Governor", 2014, 0),
    ]


def test_wmn_is_pinned_not_tabled_and_its_hash_is_an_instrument(ctx, config, conn) -> None:  # type: ignore[no-untyped-def]
    from efp import instruments
    from efp.reference.wmn import pin, wmn_path

    h.place(config, "wmn", "wmn-data.json", h.WMN_JSON)
    (result,) = loaded(ctx, "wmn")
    assert (result.status, result.rows) == ("loaded", 1)
    assert (
        pin(config).sha256
        == conn.execute("SELECT file_sha256 FROM ref_sources WHERE dataset='wmn'").fetchone()[0]
    )
    assert instruments.pin("wmn_data").sha256 == pin(config).sha256 and wmn_path(config).is_file()


# --- provenance and idempotence ---------------------------------------------------------------------


def test_every_load_writes_a_ref_sources_row_and_a_byte_identical_reload_is_a_no_op(
    ctx, config, conn
) -> None:  # type: ignore[no-untyped-def]
    h.place(
        config, "census_surnames", "names.zip", h.zip_bytes({"Names_2010Census.csv": h.CENSUS_CSV})
    )
    first = loaded(ctx, "census_surnames")[0]
    row = conn.execute(
        "SELECT dataset, url, version, file_sha256, file_size, license, attribution, row_count, status, current "
        "FROM ref_sources"
    ).fetchone()
    assert (
        row[0] == "census_surnames"
        and row[3]
        and row[4] > 0
        and row[7] == 3
        and row[8:] == ("loaded", 1)
    )
    assert "Public domain" in row[5] and "Census" in row[6] and row[2] == "2010"
    again = loaded(ctx, "census_surnames")[0]
    assert again.status == "unchanged" and again.load_id == first.load_id
    assert conn.execute("SELECT count(*) FROM ref_sources").fetchone()[0] == 1
    assert (
        conn.execute("SELECT count(*) FROM ref_surname_freq").fetchone()[0] == 3
    )  # nothing doubled


def test_a_changed_file_is_a_new_load_and_the_old_one_stops_being_current(
    ctx, config, conn
) -> None:  # type: ignore[no-untyped-def]
    h.place(
        config, "census_surnames", "names.zip", h.zip_bytes({"Names_2010Census.csv": h.CENSUS_CSV})
    )
    first = loaded(ctx, "census_surnames")[0]
    newer = h.CENSUS_CSV.replace("ZZZRARE,162253,100,0.03", "ZZZRARE,162253,120,0.04")
    h.place(config, "census_surnames", "names.zip", h.zip_bytes({"Names_2010Census.csv": newer}))
    second = loaded(ctx, "census_surnames")[0]
    assert second.status == "loaded" and second.load_id != first.load_id
    assert rows(conn, "SELECT load_id, current FROM ref_sources ORDER BY load_id") == [
        (first.load_id, 0),
        (second.load_id, 1),
    ]
    assert (
        conn.execute("SELECT count(*) FROM ref_surname_freq_current").fetchone()[0] == 3
    )  # only the new rows
    assert (
        conn.execute("SELECT count(*) FROM ref_surname_freq").fetchone()[0] == 6
    )  # the old rows are kept


def test_a_web_page_where_a_zip_should_be_is_gated_and_never_loaded(ctx, config, conn) -> None:  # type: ignore[no-untyped-def]
    h.place(
        config,
        "census_surnames",
        "names.zip",
        "<html><head><title>Request Rejected</title></head></html>",
    )
    (result,) = loaded(ctx, "census_surnames")
    assert result.status == "gated" and "web page" in result.reason
    assert conn.execute("SELECT count(*) FROM ref_surname_freq").fetchone()[0] == 0
    assert (
        conn.execute("SELECT status, current FROM ref_sources").fetchone() == ("gated", 0)
        if conn.execute("SELECT count(*) FROM ref_sources").fetchone()[0]
        else True
    )


def test_a_file_in_an_unexpected_layout_is_gated_with_what_was_seen_and_rolled_back(
    ctx, config, conn
) -> None:  # type: ignore[no-untyped-def]
    h.place(
        config,
        "census_surnames",
        "names.zip",
        h.zip_bytes({"Names_2010Census.csv": "surname,n\nSMITH,1\n"}),
    )
    (result,) = loaded(ctx, "census_surnames")
    assert result.status == "gated" and "expected columns" in result.reason
    assert conn.execute("SELECT count(*) FROM ref_surname_freq").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM ref_sources").fetchall() == [("gated",)]


def test_a_dataset_without_its_file_is_skipped_and_the_others_still_load(ctx, config, conn) -> None:  # type: ignore[no-untyped-def]
    h.place(config, "wmn", "wmn-data.json", h.WMN_JSON)
    results = h.load_all(ctx)
    assert results["wmn"][0].status == "loaded"
    assert all(r.status == "skipped" for name in ("census_surnames", "dime") for r in results[name])


# --- the candidate index ----------------------------------------------------------------------------


def _load_candidates(ctx, config, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    h.place(config, "fec_candidates", "cn24.zip", h.zip_bytes({"cn.txt": h.FEC_CN}))
    h.place(config, "congress", "legislators-current.yaml", h.CONGRESS_YAML)
    h.place(config, "openstates", "people-main.zip", h.zip_bytes(h.OPENSTATES))
    h.place(config, "harvard_local", "local_elections.csv", h.HARVARD_CSV)
    h.dime_sqlite(tmp_path / "d.sqlite")
    h.place(config, "dime", "dime.sqlite", (tmp_path / "d.sqlite").read_bytes())
    h.place(config, "ftm", "ftm_candidates.csv", h.FTM_CSV)
    for name in ("fec_candidates", "congress", "openstates", "harvard_local", "dime", "ftm"):
        loaded(ctx, name)


def test_the_candidate_index_unifies_every_source_in_one_shape(
    ctx, config, conn, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    _load_candidates(ctx, config, tmp_path)
    result = index.rebuild(conn)
    assert result.counts == {
        "dime": 2,
        "fec": 4,
        "ftm": 2,
        "harvard_local": 2,
        "congress": 2,
        "state_leg": 2,
    }
    columns = [r[1] for r in conn.execute("PRAGMA table_info(ref_candidates_all)")]
    assert columns[:12] == ["source", "source_id", "load_id", "surname", "given", "middle", "surname_key",
                            "state", "office", "party", "party_imputed", "years"]  # fmt: skip
    smith = rows(
        conn,
        "SELECT source, source_id, years, first_year, last_year FROM ref_candidates_all WHERE surname_key='SMITH' ORDER BY source",
    )
    assert (
        "dime",
        "cand1",
        "2012-2014",
        2012,
        2014,
    ) in smith  # DIME's cycles collapse to one candidate
    assert ("congress", "X999001", "2005-2013", 2005, 2013) in smith
    imputed = rows(
        conn, "SELECT DISTINCT party_imputed FROM ref_candidates_all WHERE source='harvard_local'"
    )
    assert imputed == [(1,)]
    assert index.counts(conn)["dime"] == 2 and index.coverage_by_state(conn)[0][0] in {
        "CT",
        "IL",
        "NY",
        "CA",
        "US",
    }


def test_the_index_works_over_whichever_tables_exist_and_rebuilds_without_doubling(
    ctx, config, conn
) -> None:  # type: ignore[no-untyped-def]
    h.place(config, "congress", "legislators-current.yaml", h.CONGRESS_YAML)
    loaded(ctx, "congress")
    assert index.rebuild(conn).counts["congress"] == 2 and index.rebuild(conn).total == 2
    assert conn.execute("SELECT count(*) FROM ref_candidates_all").fetchone()[0] == 2


# --- names -------------------------------------------------------------------------------------------


def test_names_are_split_once_and_keyed_for_blocking() -> None:
    assert split_name("SMITH, JOHN Q") == split_name("John Q. Smith").__class__(
        "SMITH", "JOHN", "Q"
    )
    assert split_name("").surname == "" and split_name("Madonna").surname == "Madonna"
    assert surname_key("O'Brien-Smith Jr.") == "OBRIENSMITHJR"


# --- the selftest -----------------------------------------------------------------------------------


def test_selftest_reference_recomputes_known_values_and_reports_the_rest(ctx, config, conn) -> None:  # type: ignore[no-untyped-def]
    h.place(
        config, "census_surnames", "names.zip", h.zip_bytes({"Names_2010Census.csv": h.CENSUS_CSV})
    )
    h.place(config, "ssa_names", "names.zip", h.zip_bytes(h.SSA_NATIONAL))
    loaded(ctx, "census_surnames")
    loaded(ctx, "ssa_names")
    checks = {c.name: c for c in reference_checks(config, conn)}
    assert (
        checks["reference.census_surnames"].ok
        and "162" not in checks["reference.census_surnames"].detail
    )
    assert checks["known: SMITH proportion per 100k (Census 2010)"].ok
    assert checks["known: Mary (F) born 1880, national count"].ok
    assert (
        not checks["reference.dime"].ok and not checks["reference.dime"].required
    )  # not loaded: reported


def test_selftest_reference_fails_when_a_known_value_is_wrong(ctx, config, conn) -> None:  # type: ignore[no-untyped-def]
    wrong = h.CENSUS_CSV.replace("828.19", "500.00")
    h.place(config, "census_surnames", "names.zip", h.zip_bytes({"Names_2010Census.csv": wrong}))
    loaded(ctx, "census_surnames")
    bad = [c for c in reference_checks(config, conn) if c.name.startswith("known: SMITH")]
    assert bad and not bad[0].ok and bad[0].required


def test_selftest_reference_fails_when_a_file_no_longer_matches_its_hash(ctx, config, conn) -> None:  # type: ignore[no-untyped-def]
    path = h.place(config, "wmn", "wmn-data.json", h.WMN_JSON)
    loaded(ctx, "wmn")
    path.write_text(h.WMN_JSON.replace("ExampleSite", "Changed"))
    check = next(c for c in reference_checks(config, conn) if c.name == "reference.wmn")
    assert not check.ok


# --- the chain -----------------------------------------------------------------------------------------


def test_every_dataset_is_a_module_of_the_reference_chain_and_says_why_it_cannot_run(
    config, conn
) -> None:  # type: ignore[no-untyped-def]
    from efp import modules as chain
    from efp.builtin_modules import register_builtin
    from efp.modules import REGISTRY

    register_builtin()
    context, cap = h.make_ctx(config, conn)
    try:
        listed = {m.name: m for m in chain.describe(config, context, REGISTRY, "reference")}
    finally:
        cap.close()
    assert set(listed) == {f"reference.{cls.name}" for cls in DATASET_CLASSES}
    assert all(m.registered for m in listed.values())
    assert not listed["reference.dime"].ok and "dime_url" in listed["reference.dime"].reason
    assert listed["reference.census_surnames"].ok


# --- review round 1: the index follows the loads, known values, the chain, names -----------------------------


def test_two_fec_cycles_of_one_candidate_collapse_to_one_index_row(ctx, config, conn) -> None:  # type: ignore[no-untyped-def]
    h.place(
        config,
        "fec_candidates",
        "cn22.zip",
        h.zip_bytes({"cn.txt": h.FEC_CN.replace("|2024|", "|2022|")}),
    )
    h.place(config, "fec_candidates", "cn24.zip", h.zip_bytes({"cn.txt": h.FEC_CN}))
    loaded(ctx, "fec_candidates")
    index.rebuild(conn)
    got = conn.execute(
        "SELECT years, first_year, last_year FROM ref_candidates_all WHERE source_id = 'H0CT01001'"
    ).fetchall()
    assert got == [("2022-2024", 2022, 2024)]  # one candidate across cycles, one row


def test_an_implausible_year_is_null_in_the_index_but_kept_in_the_source_table(
    ctx, config, conn
) -> None:  # type: ignore[no-untyped-def]
    bad = h.FEC_CN.replace("|2024|CT|H|", "|2990|CT|H|")
    h.place(config, "fec_candidates", "cn24.zip", h.zip_bytes({"cn.txt": bad}))
    loaded(ctx, "fec_candidates")
    index.rebuild(conn)
    assert conn.execute(
        "SELECT election_year FROM ref_fec_candidates WHERE cand_id='H0CT01001'"
    ).fetchone() == (2990,)
    assert conn.execute(
        "SELECT first_year, last_year, years FROM ref_candidates_all WHERE source_id='H0CT01001'"
    ).fetchone() == (None, None, None)


def test_loading_a_candidate_dataset_rebuilds_the_index_and_a_stale_index_is_a_failed_selftest(
    ctx, config, conn
) -> None:  # type: ignore[no-untyped-def]
    from efp.reference import ops

    h.place(config, "congress", "legislators-current.yaml", h.CONGRESS_YAML)
    ops.load(ctx, ["congress"])
    assert index.counts(conn) == {"congress": 2} and not index.is_stale(conn)  # rebuilt by the load
    assert all(c.ok for c in reference_checks(config, conn) if c.name == "reference.candidates_all")
    h.place(config, "openstates", "people-main.zip", h.zip_bytes(h.OPENSTATES))
    datasets()["openstates"].load(ctx)  # loaded without going through ops.load: the index is behind
    assert index.is_stale(conn)
    stale = next(c for c in reference_checks(config, conn) if c.name == "reference.candidates_all")
    assert not stale.ok and stale.required and "STALE" in stale.detail


def test_with_nothing_loaded_the_selftest_reports_and_fails_nothing(config, conn) -> None:  # type: ignore[no-untyped-def]
    checks = reference_checks(config, conn)
    assert checks and not [c for c in checks if not c.ok and c.required]
    assert all(c.detail == "not loaded" or c.name == "reference.candidates_all" for c in checks)


def test_known_values_run_on_the_candidate_and_population_data(ctx, config, conn) -> None:  # type: ignore[no-untyped-def]
    h.place(
        config,
        "fec_candidates",
        "cn16.zip",
        h.zip_bytes({"cn.txt": h.FEC_CN.replace("|2024|", "|2016|")}),
    )
    h.place(config, "census_population", "state_agesex.csv", h.POPULATION_CSV)
    loaded(ctx, "fec_candidates")
    loaded(ctx, "census_population")
    known = {c.name: c for c in reference_checks(config, conn) if c.name.startswith("known:")}
    assert known["known: FEC candidate P80001571, 2016 cycle: party REP"].ok
    assert not known[
        "known: jurisdictions in the state file (50 states and DC)"
    ].ok  # one state in the fixture
    assert all(c.required for c in known.values())


def test_verify_fails_when_the_recorded_file_is_missing(ctx, config, conn) -> None:  # type: ignore[no-untyped-def]
    path = h.place(config, "wmn", "wmn-data.json", h.WMN_JSON)
    loaded(ctx, "wmn")
    path.unlink()
    check = next(c for c in reference_checks(config, conn) if c.name == "reference.wmn")
    assert not check.ok


def test_the_reference_chain_in_config_decides_which_datasets_run(write_config) -> None:  # type: ignore[no-untyped-def]
    from efp.config import load_config
    from efp.reference import ops

    only = load_config(
        write_config({'"reference.ssa_names", ': "", '"reference.census_surnames", ': ""})
    )
    names = [d.name for d in ops.configured(only)]
    assert "ssa_names" not in names and "census_surnames" not in names and "wmn" in names
    with pytest.raises(ops.UnknownDataset):
        ops.select(only, ["ssa_names"])


def test_the_pinned_wmn_instrument_is_found_in_a_fresh_process_state(
    config, conn, monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    from typer.testing import CliRunner

    from efp import instruments
    from efp.cli import app

    h.place(config, "wmn", "wmn-data.json", h.WMN_JSON)
    monkeypatch.setattr(
        instruments, "_REGISTRY", dict(instruments._REGISTRY)
    )  # undo after the test
    monkeypatch.setenv("EFP_CONFIG", str(tmp_path / "config.toml"))
    result = CliRunner().invoke(app, ["reference", "status"])
    assert result.exit_code == 0, result.output
    assert instruments.pin("wmn_data").files == ("wmn-data.json",)


def test_names_need_nameparser_3_and_do_not_fall_back_silently(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import sys

    monkeypatch.setitem(sys.modules, "nameparser", None)  # an install without it
    with pytest.raises(ImportError):
        split_name("SMITH, JOHN")


def test_a_hand_imported_file_records_where_it_came_from_as_the_dataset_url(
    ctx, config, conn, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    from efp.reference import ops

    mine = h.write(tmp_path / "h.csv", h.HARVARD_CSV)
    ops.import_by_hand(ctx, "harvard_local", mine)
    assert conn.execute(
        "SELECT url FROM ref_sources WHERE dataset='harvard_local' AND status='loaded'"
    ).fetchone() == ("manual:harvard_local/local_elections.csv",)


def test_a_stray_line_break_character_inside_a_fec_name_does_not_split_the_record(
    ctx, config, conn
) -> None:  # type: ignore[no-untyped-def]
    text = (
        "H0CT01001|SMITH, JOH\x85N Q|DEM|2024|CT|H|01|I|C|C001|1 MAIN ST||HARTFORD|CT|06101\n"
        "S0CT00002|DOE, JANE|REP|2022|CT|S|00|C|C|C002|2 MAIN ST||HARTFORD|CT|06101\r\n"
        "broken line with too few cells\n"
        "\n"
        "H0CT01003|ROE, RICK|DEM|2024|CT|H|02|I|C|C003|3 MAIN ST||HARTFORD|CT|06101\n"
    )
    h.place(config, "fec_candidates", "cn24.zip", h.zip_bytes({"cn.txt": text}))
    (result,) = [r for r in loaded(ctx, "fec_candidates") if r.status == "loaded"]
    assert result.rows == 3  # the \x85 did not cut a record; the \r\n line is whole
    notes = json.loads(
        conn.execute("SELECT notes_json FROM ref_sources WHERE status='loaded'").fetchone()[0]
    )
    assert notes["skipped_malformed_lines"] == 1 and notes["first_skipped_lines"] == [3]  # counted
    assert (
        conn.execute(
            "SELECT count(*) FROM ref_fec_candidates WHERE cand_id = 'H0CT01001'"
        ).fetchone()[0]
        == 1
    )

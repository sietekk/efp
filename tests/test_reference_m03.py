"""Maintenance 03: the DIME v4 file in its real shape, candidate-only loading, the party map, the
extra fields, a rebuild of the index, the two files both called names.zip, and what the identity
stage does when the reference loads change. Synthetic files only: no row, name or figure is from a
real file (the column names are DIME's own)."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest

from efp import identity
from efp.config import Config
from efp.identity import checks as identity_checks
from efp.identity import params, rescore
from efp.modules import Registry
from efp.reference import datasets, index, ops
from efp.types import Context, Row
from tests import reference_helpers as h
from tests.identity_helpers import seed_candidate, seed_reference, seed_row

DIME_HEADER = (
    "election,cycle,fecyear,bonica.rid,bonica.cid,name,lname,ffname,fname,mname,title,suffix,"
    "party,state,seat,district,distcyc,ico.status,cand.gender,recipient.cfscore,pwinner,gwinner,"
    "recipient.type\n"
)
DIME_CSV = DIME_HEADER + "".join(
    line + "\n"
    for line in (
        # a candidate over two cycles: Democratic, then listed with a suffix and as the winner
        'fd2012,2012,2012,cand1,c1,"sample, pat q",sample,pat q,pat,q,,,100,CT,federal:house,CT-01,,C,M,-0.7,,L,cand',
        'fd2014,2014,2014,cand1,c1,"sample, pat q",sample,pat q,pat,q,,jr jr,100,CT,federal:house,CT-01,,I,M,-0.6,W,W,cand',
        # a Republican state legislator, party code with a letter code that is not mapped
        'ST2016,2016,2016,cand2,c2,"example, jo",example,jo,jo,,,,200,ND,state:lower,ND-12,,O,F,0.9,,W,cand',
        'ST2018,2018,2018,cand3,c3,"other, kim",other,kim,kim,,,,GRE,ND,state:upper,ND-3,,C,U,0.2,,L,cand',
        # no surname part in lname: split from the full name, which is how 1,816 real rows arrive
        'ST2018,2018,2018,cand4,c4,"lastonly, ali",,ali,ali,,,,328,IL,local:mayor,,,O,,,,,cand',
        # a numeric 'state' is kept as no state
        'ST2018,2018,2018,cand5,c5,"numeric, sam",numeric,sam,sam,,,,,99,local:other,,,,,,,,cand',
        # committees are not people
        'fd2014,2014,2014,comm1,m1,"people for x pac",,,,,,,328,CT,federal:committee,,,,,,,,comm',
        'fd2016,2016,2016,comm2,m2,"another committee",,,,,,,200,TX,state:committee,,,,,,,,comm',
    )
)


@pytest.fixture
def ctx(config: Config, conn: sqlite3.Connection):  # type: ignore[no-untyped-def]
    context, cap = h.make_ctx(config, conn)
    yield context
    cap.close()


def load_dime(ctx: Context, config: Config) -> object:
    h.place(config, "dime", "dime_recipients.csv", DIME_CSV)
    (result,) = datasets()["dime"].load(ctx)
    return result


def test_dime_keeps_candidates_only_and_counts_the_committees_it_set_aside(
    ctx: Context, config: Config, conn: sqlite3.Connection
) -> None:
    result = load_dime(ctx, config)
    assert (result.status, result.rows) == ("loaded", 6)  # type: ignore[attr-defined]
    notes = json.loads(
        conn.execute("SELECT notes_json FROM ref_sources WHERE dataset='dime'").fetchone()[0]
    )
    assert notes["excluded"] == {"committees": 2, "no_id_or_surname": 0}
    assert notes["kept"] == "candidates only" and "recipient.type" in notes["columns"]
    assert (
        conn.execute("SELECT count(*) FROM ref_dime WHERE bonica_rid LIKE 'comm%'").fetchone()[0]
        == 0
    )


def test_dime_maps_the_party_code_and_keeps_the_raw_one(
    ctx: Context, config: Config, conn: sqlite3.Connection
) -> None:
    load_dime(ctx, config)
    got = dict(
        conn.execute(
            "SELECT bonica_rid || cycle, party || '|' || coalesce(party_code, '') FROM ref_dime"
        )
    )
    assert got["cand12012"] == "DEM|100" and got["cand22016"] == "REP|200"
    assert got["cand42018"] == "IND|328"
    assert (
        got["cand32018"] == "|GRE" or got["cand32018"] is None
    )  # any other code: no party, the code kept
    assert conn.execute(
        "SELECT party, party_code FROM ref_dime WHERE bonica_rid = 'cand3'"
    ).fetchone() == (None, "GRE")


def test_dime_carries_suffix_outcome_status_gender_and_the_fec_year(
    ctx: Context, config: Config, conn: sqlite3.Connection
) -> None:
    load_dime(ctx, config)
    first = conn.execute(
        "SELECT suffix, gender, won, primary_won, ico_status, fecyear FROM ref_dime WHERE bonica_rid='cand1' ORDER BY cycle"
    ).fetchall()
    assert first == [(None, "M", 0, None, "C", 2012), ("JR", "M", 1, 1, "I", 2014)]  # 'jr jr' is JR
    assert (
        conn.execute("SELECT gender FROM ref_dime WHERE bonica_rid='cand3'").fetchone()[0] is None
    )  # U


def test_dime_splits_a_row_with_no_surname_part_from_the_full_name(
    ctx: Context, config: Config, conn: sqlite3.Connection
) -> None:
    load_dime(ctx, config)
    assert conn.execute(
        "SELECT surname, given, surname_key FROM ref_dime WHERE bonica_rid='cand4'"
    ).fetchone() == ("lastonly", "ali", "LASTONLY")


def test_a_numeric_state_is_no_state_and_the_seat_is_the_office(
    ctx: Context, config: Config, conn: sqlite3.Connection
) -> None:
    load_dime(ctx, config)
    assert conn.execute(
        "SELECT state, office FROM ref_dime WHERE bonica_rid='cand5'"
    ).fetchone() == (None, "local:other")


def test_the_index_collapses_cycles_and_carries_the_mapped_party(
    ctx: Context, config: Config, conn: sqlite3.Connection
) -> None:
    load_dime(ctx, config)
    index.rebuild(conn)
    got = conn.execute(
        "SELECT source, source_id, party, office, years FROM ref_candidates_all WHERE source='dime' ORDER BY source_id"
    ).fetchall()
    assert ("dime", "cand1", "DEM", "federal:house", "2012-2014") in got
    assert len([g for g in got if g[1] == "cand1"]) == 1 and len(got) == 5
    assert index.counts(conn)["dime"] == 5


def test_a_known_value_from_the_real_file_is_checked_only_where_that_file_is_loaded(
    ctx: Context, config: Config, conn: sqlite3.Connection
) -> None:
    from dataclasses import replace

    load_dime(ctx, config)
    dime = datasets()["dime"]
    (known,) = dime.known
    skipped = dime.check_known(ctx)[0]
    assert skipped.ok and skipped.detail.startswith("not checked")  # the synthetic file is small
    dime.known = [replace(known, when="SELECT 1")]  # type: ignore[misc]
    wrong = dime.check_known(ctx)[0]
    assert not wrong.ok and "1 (expected 1823)" in wrong.detail  # evaluated, and it fails
    dime.known = [replace(known, when="SELECT 1", expected=1)]  # type: ignore[misc]
    assert dime.check_known(ctx)[0].ok


def test_the_by_state_known_value_does_not_fail_on_a_national_only_load(
    ctx: Context, config: Config, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    from efp.reference.checks import reference_checks

    ops.import_by_hand(
        ctx,
        "ssa_names",
        h.write(tmp_path / "n" / "names.zip", h.zip_bytes(h.SSA_NATIONAL)),
        "national",
    )
    checks = [
        c for c in reference_checks(config, conn) if c.name.startswith("known: Mary (F) born 1950")
    ]
    assert checks and all(c.ok for c in checks)


def test_a_dime_missing_value_marker_is_not_a_suffix_or_a_status(
    ctx: Context, config: Config, conn: sqlite3.Connection
) -> None:
    csv_text = DIME_HEADER + (
        'fd2012,2012,2012,candx,c1,"sample, pat",sample,pat,pat,,,NA,100,CT,federal:house,CT-01,,NA,M,0.1,,,cand\n'
    )
    h.place(config, "dime", "dime_na.csv", csv_text)
    (result,) = datasets()["dime"].load(ctx)
    assert result.rows == 1
    assert conn.execute("SELECT suffix, ico_status FROM ref_dime").fetchone() == (None, None)


# ---- the identity stage and the DIME candidate --------------------------------------------------


@pytest.fixture
def registry() -> Registry:
    reg = Registry()
    identity.register_modules(reg)
    return reg


def run_identity(
    conn: sqlite3.Connection, ctx: Context, registry: Registry, ids: list[int]
) -> dict[int, dict]:  # type: ignore[type-arg]
    rows = []
    for row_id in ids:
        r = conn.execute(
            "SELECT year, state, name, details FROM rows WHERE row_id = ?", (row_id,)
        ).fetchone()
        rows.append(
            Row(
                row_id=row_id,
                year=r[0],
                state=r[1],
                source={"name": r[2], "details": r[3], "state": r[1]},
            )
        )
    return {r.row_id: r.summary_json for r in identity.process_batch(rows, ctx, registry)}


def test_a_dime_candidate_brings_its_suffix_nickname_and_facts(
    config: Config, conn: sqlite3.Connection, registry: Registry
) -> None:
    seed_reference(conn)
    seed_row(conn, 1, "Pat Q. Sample Jr", state="CT")
    seed_candidate(conn, "dime", "cand1", "SAMPLE", "PAT", middle="Q")
    conn.execute(
        "INSERT INTO ref_dime (load_id, bonica_rid, cycle, surname, given, middle, surname_key, state, "
        "office, party, party_code, suffix, gender, won, primary_won, ico_status, fecyear, nickname) "
        "SELECT load_id, 'cand1', 2014, 'SAMPLE', 'PAT', 'Q', 'SAMPLE', 'CT', 'federal:house', 'DEM', '100', "
        "'SR', 'M', 1, 0, 'I', 2014, 'Patty' FROM ref_sources WHERE dataset = 'dime' LIMIT 1"
    )
    conn.commit()
    ctx = Context(config=config, db=conn, log=logging.getLogger("t"))
    summary = run_identity(conn, ctx, registry, [1])[1]
    assert summary["candidates"] == 1
    fields = json.loads(
        conn.execute("SELECT fields_json FROM identity_candidates WHERE row_id = 1").fetchone()[0]
    )
    extras = fields["person"]["extras"]
    assert extras["party_code"] == "100" and extras["won"] is True and extras["ico_status"] == "I"
    assert (
        extras["nicknames"] == ["Patty"] and extras["gender"] == "M" and extras["fecyear"] == 2014
    )
    assert fields["person"]["suffix"] == "SR"
    assert "PATTY" in fields["compared"]["right"]["given_forms"]
    assert fields["flags"] == ["suffix_conflict"]  # the row says Jr, DIME says Sr: at most reviewed


# ---- two files both called names.zip ------------------------------------------------------------


def test_imports_land_in_the_dataset_and_part_they_were_given_even_when_two_files_share_a_name(
    ctx: Context, config: Config, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    census = tmp_path / "census" / "names.zip"
    ssa = tmp_path / "ssa" / "names.zip"
    state = tmp_path / "ssa" / "namesbystate.zip"
    for path, data in (
        (census, h.zip_bytes({"Names_2010Census.csv": h.CENSUS_CSV})),
        (ssa, h.zip_bytes(h.SSA_NATIONAL)),
        (state, h.zip_bytes(h.SSA_STATE)),
    ):
        h.write(path, data)
    ops.import_by_hand(ctx, "census_surnames", census, None)
    ops.import_by_hand(ctx, "ssa_names", ssa, "national")
    ops.import_by_hand(ctx, "ssa_names", state, "state")
    got = conn.execute(
        "SELECT dataset, part, status, row_count FROM ref_sources WHERE status = 'loaded' ORDER BY load_id"
    ).fetchall()
    assert [(g[0], g[1]) for g in got] == [
        ("census_surnames", ""),
        ("ssa_names", "national"),
        ("ssa_names", "state"),
    ]
    assert conn.execute("SELECT count(*) FROM ref_surname_freq_current").fetchone()[0] == 3
    assert (
        conn.execute("SELECT count(*) FROM ref_given_freq_current WHERE state IS NULL").fetchone()[
            0
        ]
        == 5
    )
    assert (
        conn.execute(
            "SELECT count(*) FROM ref_given_freq_current WHERE state IS NOT NULL"
        ).fetchone()[0]
        == 3
    )


# ---- identity follows the reference loads -------------------------------------------------------


def task(row_id: int = 1) -> Row:
    return Row(
        row_id=row_id,
        year=2018,
        state="CT",
        source={"name": "Jane Marie Raresurname", "state": "CT"},
    )


def test_the_input_hash_changes_when_a_new_load_becomes_current_and_not_otherwise(
    config: Config, conn: sqlite3.Connection
) -> None:
    seed_row(conn, 1, "Jane Marie Raresurname")
    ctx = Context(config=config, db=conn, log=logging.getLogger("t"))
    before = identity.input_hash(task(), ctx)
    assert identity.input_hash(task(), ctx) == before
    seed_reference(conn)  # the Census surnames, SSA names and population become current
    after = identity.input_hash(task(), ctx)
    assert after != before
    assert identity.input_hash(task(), ctx) == after


def test_a_rescore_on_the_real_prior_rewrites_only_the_rows_whose_outcome_changes_and_flips_the_tag(
    config: Config, conn: sqlite3.Connection, registry: Registry
) -> None:
    ctx = Context(config=config, db=conn, log=logging.getLogger("t"))
    seed_row(
        conn, 1, "Jane Marie Raresurname"
    )  # full middle name agrees: needs a real prior to count
    seed_candidate(conn, "fec", "F1", "RARESURNAME", "JANE", middle="MARIE")
    seed_row(conn, 2, "Jane Marie Commonsurname")
    seed_candidate(conn, "fec", "F2", "COMMONSURNAME", "JANE", middle="MARIE")
    run_identity(conn, ctx, registry, [1, 2])
    tags = dict(conn.execute("SELECT row_id, decided_by FROM decisions WHERE kind = 'identity'"))
    assert all("prior=ceiling" in t for t in tags.values())
    outcomes = dict(conn.execute("SELECT row_id, outcome FROM decisions WHERE kind = 'identity'"))
    assert outcomes == {1: "unresolved", 2: "unresolved"}  # the ceiling treats every name as common
    seed_reference(conn)  # the real tables arrive
    out = rescore.rescore(ctx, params.load_published())
    assert out.changed == [1] and out.unchanged == 1  # the rare name moves; the common one does not
    latest = dict(
        conn.execute(
            "SELECT row_id, outcome || '|' || decided_by FROM decisions WHERE kind = 'identity' ORDER BY decision_id"
        )
    )
    assert latest[1].startswith("needs_review|") and "prior=census" in latest[1]
    assert latest[2].startswith("unresolved|") and "prior=ceiling" in latest[2]  # not rewritten
    assert conn.execute("SELECT count(*) FROM decisions WHERE row_id = 2").fetchone()[0] == 1


# ---- the guarantee at both ends -----------------------------------------------------------------


def test_the_rare_surname_probe_runs_and_a_failure_is_reported(
    config: Config, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_reference(conn)
    for i, name in enumerate(
        ["Raresurname", "Raresurname", "Commonsurname", "Midsurname", "Zzqx"], 1
    ):
        seed_row(conn, i, f"John {name}")
    low = identity_checks.name_only_posteriors(
        conn, config, params.load_published(), top=2, rarest=True
    )
    assert len(low) == 4 and all(p < 0.9 and bits <= 0 for _, _, bits, p in low)
    checks = {c.name: c for c in identity_checks.identity_checks(config, conn)}
    assert checks["identity.guarantee"].ok and checks["identity.guarantee_rare"].ok
    assert "rarest" in checks["identity.guarantee_rare"].detail
    real = identity_checks.name_only_posteriors
    monkeypatch.setattr(
        identity_checks,
        "name_only_posteriors",
        lambda *a, **k: [(1, "census", 0.0, 0.95)] if k.get("rarest") else real(*a, **k),
    )
    after = {c.name: c for c in identity_checks.identity_checks(config, conn)}
    assert after["identity.guarantee"].ok and not after["identity.guarantee_rare"].ok


def test_the_rarest_surnames_are_ranked_by_the_census_proportion_floor_first(
    config: Config, conn: sqlite3.Connection
) -> None:
    from collections import Counter

    from efp.identity.prior import PriorModel
    from tests.identity_helpers import seed_reference

    seed_reference(conn)  # COMMONSURNAME 800, MIDSURNAME 20, RARESURNAME 0.5 (per 100,000)
    workbook = Counter({"Commonsurname": 50, "Midsurname": 5, "Raresurname": 1, "Nosuchname": 3})
    order = [s for s, _ in identity_checks.ranked_surnames(workbook, PriorModel(conn), rarest=True)]
    assert order == ["Nosuchname", "Raresurname", "Midsurname", "Commonsurname"]  # the floor first
    common = [
        s for s, _ in identity_checks.ranked_surnames(workbook, PriorModel(conn), rarest=False)
    ]
    assert common[0] == "Commonsurname"

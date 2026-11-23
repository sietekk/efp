"""Parameter modules, the labeled fit (on synthetic labels only), refit and the sensitivity batch."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from efp import identity, runner
from efp.config import Config, load_config
from efp.identity import charts, model, params, rescore
from efp.identity.params import ParamsLabeledPairs, ParamsPublishedPriors
from efp.modules import Registry, run_chain
from efp.types import Context, Row
from tests.dbhelpers import STAMP
from tests.identity_helpers import seed_candidate, seed_reference, seed_row


def test_every_published_value_has_a_basis_and_a_note() -> None:
    published = params.load_published()
    seen = 0
    for comparison, entry in published.params["comparisons"].items():
        assert set(entry["levels"]) == set(model.LEVELS[comparison])
        for level, value in entry["levels"].items():
            seen += 1
            assert value["basis"] in {"quoted", "derived", "design"}, (comparison, level)
            assert value["note"], (comparison, level)
            assert 0 < value["m"] < 1 and 0 < value["u"] < 1
            if value["basis"] in {"quoted", "derived"} and "source" in value:
                assert value["source"] in published.params["sources"]
    assert seen == sum(len(v) for v in model.LEVELS.values())
    assert all(
        "Read 2026-11-23" in s or "read 2026-11-23" in s or s
        for s in published.params["sources"].values()
    )


def test_the_exact_name_levels_are_neutral_in_the_published_priors() -> None:
    p = params.load_published()
    for comparison in ("surname", "given_name"):
        exact = p.params["comparisons"][comparison]["levels"]["exact"]
        assert exact["m"] == exact["u"]


def test_the_settings_hash_is_stable_and_changes_with_a_value() -> None:
    a = params.load_published()
    assert params.settings_sha(a) == params.settings_sha(params.load_published())
    b = model.Settings("x", "published_priors", json.loads(json.dumps(a.params)))
    b.params["comparisons"]["city"]["levels"]["exact"]["m"] = 0.5
    assert params.settings_sha(a) != params.settings_sha(b)


@pytest.fixture
def small_config(write_config: Callable[..., Path]) -> Config:
    return load_config(
        write_config(
            {
                "labeled_pairs_required = 150": "labeled_pairs_required = 12",
                "prior_pseudocount = 20": "prior_pseudocount = 2",
            }
        )
    )


@pytest.fixture
def world(small_config: Config, conn: sqlite3.Connection) -> tuple[Context, Registry]:
    seed_reference(conn)
    registry = Registry()
    identity.register_modules(registry)
    return Context(config=small_config, db=conn, log=logging.getLogger("t")), registry


def run_all(conn: sqlite3.Connection, ctx: Context, registry: Registry, ids: list[int]) -> None:
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
    identity.process_batch(rows, ctx, registry)


def seed_cases(conn: sqlite3.Connection) -> tuple[list[int], list[int]]:
    """Eight rows whose FEC record has the same full middle name, four with a different one."""
    same, other = [], []
    for i in range(1, 13):  # a surname of its own per row, so rows do not share candidates
        surname = "Surname" + chr(96 + i)
        seed_row(conn, i, f"Jane Marie {surname}")
        middle = "MARIE" if i <= 8 else "ADAMS"
        seed_candidate(conn, "fec", f"S{i}", surname.upper(), "JANE", middle=middle)
        (same if i <= 8 else other).append(i)
    return same, other


def label(conn: sqlite3.Connection, row_id: int, outcome: str) -> None:
    cid = conn.execute(
        "SELECT candidate_id FROM identity_candidates WHERE row_id = ? AND current = 1", (row_id,)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO decisions (row_id, kind, outcome, decided_by, decided_at, stage, candidate_id) "
        "VALUES (?, 'review', ?, 'XX', ?, 'identity', ?)",
        (row_id, outcome, STAMP, cid),
    )


def test_the_parameter_chain_runs_published_priors_until_enough_labels(
    world: tuple[Context, Registry],
) -> None:
    ctx, registry = world
    chains = [c for c in identity.chains_for(ctx.config, "identity") if c.name == "parameter"]
    out = run_chain(chains[0], Row(row_id=0), ctx, registry)
    assert [r["name"] for r in out.records] == [
        "identity.params.labeled_pairs",
        "identity.params.published_priors",
    ]
    assert (
        out.records[0]["status"] == "skipped"
        and "0 of 12 labeled pairs" in out.records[0]["reason"]
    )
    settings = out.wrote["identity.params.published_priors"]["settings"]
    assert settings.provisional and settings.kind == "published_priors"


def test_refit_is_refused_below_the_required_labels_and_needs_no_labels_otherwise_provisional(
    world: tuple[Context, Registry],
) -> None:
    ctx, _ = world
    assert ParamsLabeledPairs().available(ctx).ok is False
    assert ParamsPublishedPriors().available(ctx).ok is True


def test_unsure_labels_do_not_count(world: tuple[Context, Registry]) -> None:
    ctx, registry = world
    conn = ctx.db
    assert conn is not None
    same, other = seed_cases(conn)
    run_all(conn, ctx, registry, same + other)
    for r in same[:6]:
        label(conn, r, "label=unsure")
    for r in same[6:] + other:
        label(conn, r, "label=match")
    assert params.label_count(conn) == 6


def test_a_labeled_fit_then_rescore_changes_only_the_rows_whose_outcome_changes(
    world: tuple[Context, Registry],
) -> None:
    ctx, registry = world
    conn = ctx.db
    assert conn is not None
    same, other = seed_cases(conn)
    run_all(conn, ctx, registry, same + other)
    before = {r: runner._identity_outcome(conn, r) for r in same + other}
    assert {before[r] for r in same} == {"needs_review"} and {before[r] for r in other} == {
        "unresolved"
    }
    for r in same:
        label(conn, r, "label=match")
    for r in other:
        label(conn, r, "label=nonmatch")
    conn.commit()
    examples = params.labeled_examples(conn)
    assert len(examples) == 12 and ParamsLabeledPairs().available(ctx).ok
    fitted = params.fit(params.load_published(), examples, ctx.config.identity.prior_pseudocount)
    assert fitted.kind == "labeled_pairs" and not fitted.provisional
    exact = fitted.params["comparisons"]["middle"]["levels"]["exact"]
    assert exact["basis"] == "labeled" and exact["m"] > 0.9 and exact["u"] < 0.01
    name = fitted.params["comparisons"]["surname"]["levels"]["exact"]
    assert name["m"] == name["u"]  # the exact-name level stays neutral in a fit

    decisions_before = conn.execute(
        "SELECT count(*) FROM decisions WHERE kind = 'identity'"
    ).fetchone()[0]
    out = rescore.rescore(ctx, fitted)
    assert out.batch == "main" and sorted(out.changed) == same
    assert out.unchanged == len(other) and out.skipped_reviewed == 0
    assert {runner._identity_outcome(conn, r) for r in same} == {"confirmed"}
    assert {runner._identity_outcome(conn, r) for r in other} == {"unresolved"}
    assert conn.execute("SELECT count(*) FROM decisions WHERE kind = 'identity'").fetchone()[
        0
    ] == decisions_before + len(same)
    by = conn.execute(
        "SELECT decided_by FROM decisions WHERE row_id = ? AND outcome = 'confirmed'", (same[0],)
    ).fetchone()[0]
    assert "params=fitted" in by and "params=provisional" not in by
    assert (
        conn.execute(
            "SELECT status FROM case_stage WHERE row_id = ? AND stage = 'identity'", (same[0],)
        ).fetchone()[0]
        == "done"
    )
    # the older score is kept, superseded, so the earlier decision can still be read back
    assert (
        conn.execute(
            "SELECT count(*) FROM identity_candidates WHERE row_id = ?", (same[0],)
        ).fetchone()[0]
        == 2
    )
    assert (
        conn.execute(
            "SELECT count(*) FROM identity_candidates WHERE row_id = ? AND current = 1", (same[0],)
        ).fetchone()[0]
        == 1
    )
    # a second pass changes nothing
    again = rescore.rescore(ctx, fitted)
    assert again.changed == [] and again.unchanged == len(same) + len(other)


def test_a_refit_never_overrides_the_operator(world: tuple[Context, Registry]) -> None:
    ctx, registry = world
    conn = ctx.db
    assert conn is not None
    same, other = seed_cases(conn)
    run_all(conn, ctx, registry, same + other)
    conn.execute(
        "INSERT INTO decisions (row_id, kind, outcome, rationale, decided_by, decided_at, stage) "
        "VALUES (?, 'review', 'unresolved', 'not her', 'XX', ?, 'identity')",
        (same[0], STAMP),
    )
    for r in same:
        label(conn, r, "label=match")
    for r in other:
        label(conn, r, "label=nonmatch")
    conn.commit()
    fitted = params.fit(params.load_published(), params.labeled_examples(conn), 2)
    out = rescore.rescore(ctx, fitted)
    assert out.skipped_reviewed == 1 and same[0] not in out.changed
    assert runner._identity_outcome(conn, same[0]) == "unresolved"


def test_a_stricter_threshold_writes_a_separate_batch_and_never_the_main_one(
    world: tuple[Context, Registry],
) -> None:
    ctx, registry = world
    conn = ctx.db
    assert conn is not None
    same, other = seed_cases(conn)
    run_all(conn, ctx, registry, same + other)
    for r in same:
        label(conn, r, "label=match")
    for r in other:
        label(conn, r, "label=nonmatch")
    conn.commit()
    fitted = params.fit(params.load_published(), params.labeled_examples(conn), 2)
    rescore.rescore(ctx, fitted)  # 0.997 on the main batch: confirmed
    main_before = conn.execute("SELECT count(*) FROM decisions WHERE batch = 'main'").fetchone()[0]
    out = rescore.rescore(ctx, fitted, confirm=0.9999)
    assert out.batch == "sensitivity"
    assert (
        conn.execute("SELECT count(*) FROM decisions WHERE batch = 'main'").fetchone()[0]
        == main_before
    )
    rows = conn.execute(
        "SELECT row_id, outcome, decided_by FROM decisions WHERE batch = 'sensitivity' ORDER BY row_id"
    ).fetchall()
    assert len(rows) == 12 and all("batch=sensitivity" in r[2] for r in rows)
    assert {r[1] for r in rows if r[0] in same} == {"needs_review"}  # 0.997 < 0.9999
    assert {runner._identity_outcome(conn, r) for r in same} == {
        "confirmed"
    }  # the gate never reads it
    assert (
        conn.execute(
            "SELECT status FROM case_stage WHERE row_id = ? AND stage = 'identity'", (same[0],)
        ).fetchone()[0]
        == "done"
    )
    again = rescore.rescore(ctx, fitted, confirm=0.9999)
    assert again.changed == []  # idempotent


def test_a_confirmation_reopens_downstream_rows_skipped_for_lack_of_an_identity(
    world: tuple[Context, Registry],
) -> None:
    ctx, registry = world
    conn = ctx.db
    assert conn is not None
    same, other = seed_cases(conn)
    run_all(conn, ctx, registry, same + other)
    conn.execute(
        "UPDATE case_stage SET status='skipped', gate_reason='identity_unresolved' WHERE row_id = ? AND stage = 'news'",
        (same[0],),
    )
    for r in same:
        label(conn, r, "label=match")
    for r in other:
        label(conn, r, "label=nonmatch")
    conn.commit()
    rescore.rescore(ctx, params.fit(params.load_published(), params.labeled_examples(conn), 2))
    assert (
        conn.execute(
            "SELECT status FROM case_stage WHERE row_id = ? AND stage = 'news'", (same[0],)
        ).fetchone()[0]
        == "pending"
    )


def test_the_stability_chart_is_written_for_each_module(tmp_path: Path) -> None:
    published = params.load_published()
    html, spec = charts.write("published_priors", {"published priors v1": published}, tmp_path)
    assert html.name == "parameter_estimates_published_priors.html" and html.stat().st_size > 1000
    body = json.loads(spec.read_text())
    assert body["data"]["values"] and {"m", "u"} == {r["m_or_u"] for r in body["data"]["values"]}
    fits = params.bootstrap_fits(
        published,
        [(True, {"middle": "exact"}), (False, {"middle": "conflict"})] * 5,
        2,
        resamples=5,
        seed=1,
    )
    html2, _ = charts.write(
        "labeled_pairs",
        {"published": published, **{f"b{i}": f for i, f in enumerate(fits)}},
        tmp_path,
    )
    assert html2.name == "parameter_estimates_labeled_pairs.html"

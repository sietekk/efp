"""Regression tests for the independent review of build 12: the operator is never overridden,
demotions reach the later stages, the age is counted once, initials are not unique people, and the
audit pool, weighting and document identity are right."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from efp import identity, runner
from efp.cli import app
from efp.config import Config, load_config
from efp.identity import audit, params, rescore, review, store
from efp.identity import decide as dec
from efp.identity.pairs import Pair, PairBuilder
from efp.identity.prior import Prior, PriorModel
from efp.identity.profile import Person, doc_keys
from efp.modules import Registry
from efp.types import Context, Row
from tests.dbhelpers import STAMP
from tests.identity_helpers import seed_candidate, seed_reference, seed_row
from tests.test_identity_params import label, run_all, seed_cases


@pytest.fixture
def ctx(config: Config, conn: sqlite3.Connection) -> Context:
    seed_reference(conn)
    return Context(config=config, db=conn, log=logging.getLogger("t"))


@pytest.fixture
def registry() -> Registry:
    reg = Registry()
    identity.register_modules(reg)
    return reg


def human(conn: sqlite3.Connection, row_id: int, outcome: str) -> None:
    conn.execute(
        "INSERT INTO decisions (row_id, kind, outcome, rationale, decided_by, decided_at, stage) "
        "VALUES (?, 'review', ?, 'r', 'XX', ?, 'identity')",
        (row_id, outcome, STAMP),
    )
    conn.commit()


def band_row(conn: sqlite3.Connection, ctx: Context, registry: Registry) -> None:
    seed_row(conn, 1, "Jane Marie Raresurname")
    seed_candidate(conn, "fec", "F1", "RARESURNAME", "JANE", middle="MARIE")
    run_all(conn, ctx, registry, [1])


# ---- 2: a re-run never overrides the operator ----------------------------------------------


def test_a_rerun_scores_again_but_keeps_the_operators_decision(
    conn: sqlite3.Connection, ctx: Context, registry: Registry
) -> None:
    band_row(conn, ctx, registry)
    human(conn, 1, "unresolved")
    before = conn.execute("SELECT count(*) FROM decisions").fetchone()[0]
    run_all(conn, ctx, registry, [1])  # what `efp run identity --recheck` would do
    assert conn.execute("SELECT count(*) FROM decisions").fetchone()[0] == before
    assert runner._identity_outcome(conn, 1) == "unresolved"
    row = Row(
        row_id=1, year=2018, state="CT", source={"name": "Jane Marie Raresurname", "state": "CT"}
    )
    result = identity.process_batch([row], ctx, registry)[0]
    assert result.status == "done" and result.summary_json["operator_decision_kept"] == "unresolved"


def test_the_input_hash_changes_with_the_fit_not_with_the_label_count(
    world_config: Config, conn: sqlite3.Connection
) -> None:
    seed_reference(conn)
    ctx = Context(config=world_config, db=conn, log=logging.getLogger("t"))
    reg = Registry()
    identity.register_modules(reg)
    same, other = seed_cases(conn)
    run_all(conn, ctx, reg, same + other)
    for r in same:
        label(conn, r, "label=match")
    for r in other:
        label(conn, r, "label=nonmatch")
    conn.commit()
    row = Row(
        row_id=1, year=2018, state="CT", source={"name": "Jane Marie Surnamea", "state": "CT"}
    )
    first = identity.input_hash(row, ctx)
    # confirming a label again changes no fit (the latest label of a candidate wins), so no row is
    # made stale by it; a label that flips the fit does
    label(conn, same[0], "label=match")
    conn.commit()
    assert identity.input_hash(row, ctx) == first
    label(conn, same[0], "label=nonmatch")
    conn.commit()
    assert identity.input_hash(row, ctx) != first


@pytest.fixture
def world_config(write_config: Callable[..., Path]) -> Config:
    return load_config(
        write_config(
            {
                "labeled_pairs_required = 150": "labeled_pairs_required = 12",
                "prior_pseudocount = 20": "prior_pseudocount = 2",
            }
        )
    )


# ---- 4/5: demotion and audit_wrong -------------------------------------------------------------


def test_an_audit_wrong_verdict_resets_the_stages_that_ran_on_the_confirmation(
    conn: sqlite3.Connection, ctx: Context, registry: Registry
) -> None:
    band_row(conn, ctx, registry)
    human(conn, 1, "confirmed")
    conn.execute(
        "UPDATE case_stage SET status = 'done' WHERE row_id = 1 AND stage IN ('news','social')"
    )
    conn.execute(
        "INSERT INTO identity_audit_sample (row_id, decision_id, stratum, band, weight, draw, seed, drawn_at) "
        "VALUES (1, (SELECT max(decision_id) FROM decisions), 'rare', 1, 1.0, 1, 1, 's')"
    )
    conn.commit()
    answers = iter(["w", "the date of birth differs"])
    out = review.audit_loop(
        conn,
        reviewer="XX",
        ask=lambda p, c: next(answers),
        ask_text=lambda p: next(answers),
        say=lambda s: None,
    )
    assert out.by_answer == {"audit_wrong": 1}
    assert runner._identity_outcome(conn, 1) == "unresolved"
    states = dict(
        conn.execute(
            "SELECT stage, status FROM case_stage WHERE row_id = 1 AND stage IN ('news','social')"
        )
    )
    assert states == {"news": "pending", "social": "pending"}


def test_a_refit_that_demotes_a_confirmed_row_resets_its_downstream_stages(
    conn: sqlite3.Connection, ctx: Context, registry: Registry
) -> None:
    same, other = seed_cases(conn)
    run_all(conn, ctx, registry, same + other)
    for r in same:
        label(conn, r, "label=match")
    for r in other:
        label(conn, r, "label=nonmatch")
    conn.commit()
    fitted = params.fit(params.load_published(), params.labeled_examples(conn), 2)
    rescore.rescore(ctx, fitted)  # the 8 rows become confirmed
    conn.execute(
        "UPDATE case_stage SET status = 'done' WHERE row_id = ? AND stage = 'news'", (same[0],)
    )
    conn.commit()
    # fitted on opposite labels the same rows fall back below confirmation
    for r in same:
        label(conn, r, "label=nonmatch")
    for r in other:
        label(conn, r, "label=match")
    conn.commit()
    flipped = params.fit(params.load_published(), params.labeled_examples(conn), 2)
    out = rescore.rescore(ctx, flipped)
    assert same[0] in out.changed
    assert runner._identity_outcome(conn, same[0]) != "confirmed"
    assert (
        conn.execute(
            "SELECT status FROM case_stage WHERE row_id = ? AND stage = 'news'", (same[0],)
        ).fetchone()[0]
        == "pending"
    )


def test_a_refit_never_flips_back_a_row_the_audit_called_wrong(
    conn: sqlite3.Connection, ctx: Context, registry: Registry
) -> None:
    same, other = seed_cases(conn)
    run_all(conn, ctx, registry, same + other)
    for r in same:
        label(conn, r, "label=match")
    for r in other:
        label(conn, r, "label=nonmatch")
    human(conn, same[0], "audit_wrong")
    fitted = params.fit(params.load_published(), params.labeled_examples(conn), 2)
    out = rescore.rescore(ctx, fitted)
    assert out.skipped_reviewed == 1 and same[0] not in out.changed
    assert runner._identity_outcome(conn, same[0]) == "unresolved"


# ---- 6: the audit pool -------------------------------------------------------------------------


def test_rows_the_operator_confirmed_from_the_band_are_in_the_audit_pool_and_in_the_band_cell(
    conn: sqlite3.Connection, ctx: Context, registry: Registry
) -> None:
    band_row(conn, ctx, registry)
    human(conn, 1, "confirmed")
    pool = audit.confirmed_rows(conn, audit.Strata(conn, 1000), 0.90, 0.99)
    assert [(e.row_id, e.band) for e in pool] == [(1, 1)]
    # a sensitivity-batch needs_review is not "was in the review band"
    seed_row(conn, 2, "Jane Marie Otherrare")
    conn.execute(
        "INSERT INTO decisions (row_id, kind, outcome, decided_by, decided_at, stage, batch) "
        "VALUES (2, 'identity', 'confirmed', 'x', 's', 'identity', 'main')"
    )
    conn.execute(
        "INSERT INTO decisions (row_id, kind, outcome, decided_by, decided_at, stage, batch) "
        "VALUES (2, 'identity', 'needs_review', 'x', 's', 'identity', 'sensitivity')"
    )
    conn.commit()
    two = [
        e for e in audit.confirmed_rows(conn, audit.Strata(conn, 1000), 0.90, 0.99) if e.row_id == 2
    ]
    assert two and two[0].band == 0
    human(conn, 2, "audit_wrong")  # a withdrawn confirmation is not drawn
    assert [e.row_id for e in audit.confirmed_rows(conn, audit.Strata(conn, 1000), 0.90, 0.99)] == [
        1
    ]


def test_the_exact_bound_uses_the_effective_sample_size(conn: sqlite3.Connection) -> None:
    for i in range(1, 5):
        seed_row(conn, i, f"Jane Marie Surname{chr(96 + i)}")
        conn.execute(
            "INSERT INTO decisions (row_id, kind, outcome, decided_by, decided_at, stage) "
            "VALUES (?, 'review', 'audit_correct', 'x', 's', 'identity')",
            (i,),
        )
        conn.execute(
            "INSERT INTO decisions (row_id, kind, outcome, decided_by, decided_at, stage, batch) VALUES (?, 'identity', 'confirmed', 'x', 's', 'identity', 'main')",
            (i,),
        )
        conn.execute(
            "INSERT INTO identity_audit_sample (row_id, decision_id, stratum, band, weight, draw, seed, drawn_at) "
            "VALUES (?, (SELECT max(decision_id) FROM decisions), 'rare', 0, ?, 1, 1, 's')",
            (i, 100.0 if i == 1 else 1.0),
        )
    conn.commit()
    rare = {r.stratum: r for r in audit.rates(conn, resamples=200, seed=1)}["rare"]
    assert rare.upper95 is not None and rare.upper95 > 1 - 0.05 ** (1 / 4)  # fewer effective rows


# ---- 3: the age is counted once ----------------------------------------------------------------


def person(**kw: object) -> Person:
    return Person("court", "x", "John", "", "Smith", **kw)  # type: ignore[arg-type]


def water(dob_bits: float, other_bits: float = 0.0) -> dict:  # type: ignore[type-arg]
    steps = [
        {"comparison": "dob", "level": "exact", "log2_weight": dob_bits},
        {"comparison": "city", "level": "exact", "log2_weight": other_bits},
    ]
    return {
        "evidence_bits": dob_bits + other_bits,
        "prior_odds": 1.0,
        "posterior": 0.0,
        "steps": steps,
    }


def test_the_age_is_credited_once() -> None:
    pair = Pair(1, {}, {}, Prior(80.0, {}), age_boost_bits=6.0)
    scored = dec.score_candidate(person(), pair, water(12.0, 5.0))
    assert scored.waterfall["age_overlap_bits"] == 6.0
    assert scored.waterfall["evidence_bits"] == pytest.approx(11.0)
    assert scored.waterfall["evidence_bits_raw"] == pytest.approx(17.0)
    # the credit is capped by the age evidence itself and never turns a negative weight positive
    small = dec.score_candidate(person(), pair, water(2.0))
    assert small.waterfall["age_overlap_bits"] == 2.0 and small.waterfall["evidence_bits"] == 0.0
    conflict = dec.score_candidate(person(), pair, water(-4.0))
    assert (
        conflict.waterfall["age_overlap_bits"] == 0.0
        and conflict.waterfall["evidence_bits"] == -4.0
    )


def test_the_boost_is_what_the_cohort_gave_the_prior(conn: sqlite3.Connection) -> None:
    seed_reference(conn)
    model = PriorModel(conn)
    inputs = {
        "surname": "Commonsurname",
        "given": "John",
        "state": "CT",
        "birth_lo": 1970,
        "birth_hi": 1970,
        "widen": 0,
    }
    narrow = model.prior(
        surname="Commonsurname", given="John", state="CT", birth_lo=1970, birth_hi=1970
    )
    assert model.age_boost_bits(inputs, narrow) == pytest.approx(
        6.17, abs=0.01
    )  # 14.4M / 200k = 72
    assert model.age_boost_bits({**inputs, "birth_lo": None}, narrow) == 0.0


def test_a_date_of_birth_and_a_city_on_a_common_name_no_longer_confirm_it(
    conn: sqlite3.Connection,
) -> None:
    """The review's probe: an exact date of birth narrows the prior to one cohort and then was
    paid for again as evidence."""
    seed_reference(conn)
    builder = PairBuilder(conn, PriorModel(conn))
    left = Person("row", "1", "John", "", "Commonsurname", state="CT")
    right = Person("court", "x", "John", "", "Commonsurname", state="CT")
    from efp.identity.profile import Value

    for p in (left, right):
        p.add(
            "dob", Value("March 3, 1970", None, "born March 3, 1970", frozenset({f"d{p.source}"}))
        )
        p.add("city", Value("Hartford", None, "of Hartford", frozenset({f"c{p.source}"})))
    pair = builder.build(1, left, right, 2018)
    assert pair.age_boost_bits > 5
    from efp.identity import model

    w = model.score(
        [{"pair_id": 1, "left": pair.left, "right": pair.right}],
        params.load_published(),
        tf={
            "surname": model.term_frequencies(["X"] * 100),
            "given_name": model.term_frequencies(["Y"] * 100),
        },
    )[1]
    scored = dec.score_candidate(right, pair, w)
    assert scored.posterior < 0.99  # with the age counted twice this was 0.9996


# ---- 1/9: suffixes and documents -------------------------------------------------------------


def test_a_suffix_conflict_is_at_most_reviewed(conn: sqlite3.Connection) -> None:
    seed_reference(conn)
    builder = PairBuilder(conn, PriorModel(conn))
    left = Person("row", "1", "John", "", "Raresurname", suffix="Jr", state="CT")
    right = Person("court", "x", "John", "", "Raresurname", suffix="Sr", state="CT")
    assert builder.build(1, left, right, 2018).flags == ["suffix_conflict"]
    same = Person("court", "y", "John", "", "Raresurname", suffix="Jr", state="CT")
    assert builder.build(2, left, same, 2018).flags == []
    pair = Pair(1, {}, {}, Prior(1.0, {}), flags=["suffix_conflict"])
    water = {
        "evidence_bits": 25.0,
        "prior_odds": 1.0,
        "posterior": 0,
        "steps": [{"comparison": "case_number", "level": "exact", "log2_weight": 19.0}],
    }
    out = dec.decide([dec.score_candidate(person(), pair, water)], confirm=0.99, review=0.90)
    assert (out.outcome, out.reason) == ("needs_review", "suffix_conflict")


def test_a_wayback_copy_is_the_same_document_as_the_page_it_archived() -> None:
    live = doc_keys("a" * 64, "https://www.example.test/story/")
    archived = doc_keys(
        "b" * 64, "https://web.archive.org/web/20200101000000/https://www.example.test/story"
    )
    assert live & archived == {"url:example.test/story"}


# ---- 7: the weighted fit -----------------------------------------------------------------------


def test_labels_are_weighted_by_their_strata(
    conn: sqlite3.Connection, ctx: Context, registry: Registry
) -> None:
    from efp.identity import labels

    same, other = seed_cases(conn)
    run_all(conn, ctx, registry, same + other)
    labels.build_queue(conn, size=12, seed=1, common_rank=1)
    for _cid, row_id, _ in labels.pending(conn):
        label(conn, row_id, "label=match")
    conn.commit()
    examples = params.labeled_examples(conn)
    assert len(examples) == 12 and all(len(e) == 3 for e in examples)
    weights = [e[2] for e in examples]  # type: ignore[misc]
    assert sum(weights) / len(weights) == pytest.approx(1.0)


# ---- the refit command with enough labels ---------------------------------------------------


def test_the_refit_command_fits_charts_and_rescores(
    conn: sqlite3.Connection,
    ctx: Context,
    registry: Registry,
    write_config: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg = load_config(
        write_config(
            {
                "labeled_pairs_required = 150": "labeled_pairs_required = 12",
                "prior_pseudocount = 20": "prior_pseudocount = 2",
            }
        )
    )
    monkeypatch.setenv("EFP_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setattr("efp.identity.charts.REPORT_DIR", tmp_path / "charts")
    ctx2 = Context(config=cfg, db=conn, log=logging.getLogger("t"))
    same, other = seed_cases(conn)
    run_all(conn, ctx2, registry, same + other)
    for r in same:
        label(conn, r, "label=match")
    for r in other:
        label(conn, r, "label=nonmatch")
    conn.commit()
    result = CliRunner().invoke(app, ["identity", "refit"])
    assert result.exit_code == 0, result.output
    assert "fitted labeled_" in result.output and "8 written" in result.output
    assert (tmp_path / "charts" / "parameter_estimates_labeled_pairs.html").is_file()
    assert runner._identity_outcome(conn, same[0]) == "confirmed"
    assert store.human_decision(conn, same[0]) is None


# ---- a re-score through the runner keeps the later stages in step -------------------------------


@pytest.fixture
def strict_config(write_config: Callable[..., Path]) -> Config:
    return load_config(write_config({"confirm_threshold = 0.99": "confirm_threshold = 0.9999"}))


COURT_TEXT = "John Quincy Raresurname, 52, of Hartford, was sentenced. Case No. 3:19-cr-123."
COURT_IDS = [
    ("age", "52", "Raresurname, 52, of Hartford"),
    ("city", "Hartford", "52, of Hartford"),
    ("case_number", "3:19-cr-123", "Case No. 3:19-cr-123"),
]


def confirmable_row(conn: sqlite3.Connection, config: Config) -> None:
    from tests.identity_helpers import seed_court

    seed_row(
        conn,
        1,
        "John Q. Raresurname",
        details="John Q. Raresurname, 52, of Hartford, was convicted.",
    )
    seed_court(
        conn,
        config,
        1,
        text=COURT_TEXT,
        case_number="3:19-cr-123",
        url="https://example.test/r",
        identifiers=COURT_IDS,
    )


def stage_status(conn: sqlite3.Connection, stage: str) -> tuple[str, str | None]:
    return conn.execute(
        "SELECT status, gate_reason FROM case_stage WHERE row_id = 1 AND stage = ?", (stage,)
    ).fetchone()


def test_a_runner_rescore_that_demotes_a_confirmed_row_resets_news_and_social(
    config: Config, strict_config: Config, conn: sqlite3.Connection, registry: Registry
) -> None:
    seed_reference(conn)
    confirmable_row(conn, config)
    normal = Context(config=config, db=conn, log=logging.getLogger("t"))
    run_all(conn, normal, registry, [1])
    assert runner._identity_outcome(conn, 1) == "confirmed"
    conn.execute(
        "UPDATE case_stage SET status = 'done' WHERE row_id = 1 AND stage IN ('news', 'social')"
    )
    conn.commit()
    strict = Context(config=strict_config, db=conn, log=logging.getLogger("t"))
    run_all(conn, strict, registry, [1])  # what --recheck does after the data or the settings moved
    assert runner._identity_outcome(conn, 1) == "needs_review"
    assert stage_status(conn, "news") == ("pending", None) and stage_status(conn, "social") == (
        "pending",
        None,
    )


def test_a_runner_rescore_that_confirms_a_row_releases_the_stages_skipped_for_want_of_it(
    config: Config, strict_config: Config, conn: sqlite3.Connection, registry: Registry
) -> None:
    seed_reference(conn)
    confirmable_row(conn, config)
    strict = Context(config=strict_config, db=conn, log=logging.getLogger("t"))
    run_all(conn, strict, registry, [1])
    assert runner._identity_outcome(conn, 1) == "needs_review"
    conn.execute(
        "UPDATE case_stage SET status = 'skipped', gate_reason = 'identity_unresolved' "
        "WHERE row_id = 1 AND stage IN ('news', 'social')"
    )
    conn.commit()
    normal = Context(config=config, db=conn, log=logging.getLogger("t"))
    run_all(conn, normal, registry, [1])
    assert runner._identity_outcome(conn, 1) == "confirmed"
    assert stage_status(conn, "news") == ("pending", None) and stage_status(conn, "social") == (
        "pending",
        None,
    )


def test_a_rescore_through_the_runner_leaves_an_operators_downstream_alone(
    config: Config, conn: sqlite3.Connection, registry: Registry
) -> None:
    seed_reference(conn)
    confirmable_row(conn, config)
    ctx = Context(config=config, db=conn, log=logging.getLogger("t"))
    run_all(conn, ctx, registry, [1])
    human(conn, 1, "confirmed")
    conn.execute("UPDATE case_stage SET status = 'done' WHERE row_id = 1 AND stage = 'news'")
    conn.commit()
    run_all(conn, ctx, registry, [1])  # the operator's decision stands, so nothing downstream moves
    assert stage_status(conn, "news")[0] == "done"

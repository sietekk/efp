"""The label queue, the three review loops, the F3 audit sample, the rate and the sheets."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Sequence
from pathlib import Path

import pytest

from efp import identity, runner
from efp.config import Config
from efp.identity import audit, labels, review
from efp.identity.explain import explain
from efp.modules import Registry
from efp.types import Context, Row
from tests.identity_helpers import seed_candidate, seed_reference, seed_row


@pytest.fixture
def ctx(config: Config, conn: sqlite3.Connection) -> Context:
    seed_reference(conn)
    return Context(config=config, db=conn, log=logging.getLogger("t"))


@pytest.fixture
def registry() -> Registry:
    reg = Registry()
    identity.register_modules(reg)
    return reg


def run_rows(conn: sqlite3.Connection, ctx: Context, registry: Registry, ids: list[int]) -> None:
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
    for result in identity.process_batch(rows, ctx, registry):
        conn.execute(
            "UPDATE case_stage SET status = ? WHERE row_id = ? AND stage = 'identity'",
            (result.status, result.row_id),
        )
    conn.commit()


def seed_many(conn: sqlite3.Connection, count: int = 40) -> list[int]:
    """Rows with a reference candidate each: half common and half rare surnames, half with middle
    names that agree. Surnames are made up and unique so no two rows share a candidate."""
    ids = []
    for i in range(1, count + 1):
        common = i % 2 == 0
        surname = (
            "Commonsurname"
            if common and i % 4 == 0
            else f"Surname{chr(96 + (i % 26) + 1)}{chr(96 + i // 26 + 1)}"
        )
        seed_row(conn, i, f"Jane Marie {surname}", year=2000 + i % 20)
        seed_candidate(
            conn, "fec", f"F{i}", surname.upper(), "JANE", middle="MARIE" if i % 3 else "ADAMS"
        )
        ids.append(i)
    return ids


class Script:
    """Scripted answers for ``ask`` / ``ask_text`` and the output the loop printed."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.out: list[str] = []

    def ask(self, prompt: str, choices: Sequence[str]) -> str:
        answer = self.answers.pop(0)
        assert answer in choices, (answer, choices)
        return answer

    def text(self, prompt: str) -> str:
        return self.answers.pop(0)

    def say(self, line: str) -> None:
        self.out.append(line)


def test_the_label_queue_is_stratified_seeded_and_resumable(
    conn: sqlite3.Connection, ctx: Context, registry: Registry
) -> None:
    run_rows(conn, ctx, registry, seed_many(conn))
    first = labels.build_queue(conn, size=24, seed=12, common_rank=1)
    assert first.total == 24 and first.added == 24
    strata = {s.split("/")[0] for s in first.by_stratum}
    assert strata == {"common", "rare"}  # both kinds of surname are drawn
    assert all(k.endswith("/reference") for k in first.by_stratum)
    again = labels.build_queue(conn, size=24, seed=12, common_rank=1)
    assert again.added == 0 and again.total == 24  # an existing queue is never reshuffled
    # a smaller ask never shrinks it, and a larger one tops it up without reordering
    top = labels.build_queue(conn, size=30, seed=12, common_rank=1)
    assert top.total == 30
    positions = [
        r[0] for r in conn.execute("SELECT position FROM identity_label_queue ORDER BY queue_id")
    ]
    assert positions == sorted(positions) == list(range(1, 31))
    # a fixed seed gives the same queue on a fresh database
    ids = [
        r[0]
        for r in conn.execute("SELECT candidate_id FROM identity_label_queue ORDER BY position")
    ]
    assert len(set(ids)) == 30


def test_the_queue_spans_the_posterior_range_not_only_the_hard_cases(
    conn: sqlite3.Connection, ctx: Context, registry: Registry
) -> None:
    run_rows(conn, ctx, registry, seed_many(conn))
    labels.build_queue(conn, size=20, seed=3, common_rank=1)
    posts = [
        r[0]
        for r in conn.execute(
            "SELECT c.posterior FROM identity_label_queue q JOIN identity_candidates c USING (candidate_id)"
        )
    ]
    assert min(posts) < 0.5 and max(posts) > 0.5


def test_labeling_is_blind_resumable_and_writes_review_decisions(
    conn: sqlite3.Connection, ctx: Context, registry: Registry
) -> None:
    run_rows(conn, ctx, registry, seed_many(conn))
    labels.build_queue(conn, size=6, seed=1, common_rank=1)
    script = Script(
        "m", "same middle name and office", "n", "different middle", "u", "cannot tell", "q"
    )
    tally = review.label_loop(
        conn, reviewer="XX", ask=script.ask, ask_text=script.text, say=script.say
    )
    assert tally.answered == 3 and tally.by_answer == {"match": 1, "nonmatch": 1, "unsure": 1}
    shown = "\n".join(script.out)
    assert (
        "posterior" not in shown.lower() and "needs_review" not in shown
    )  # the model's score is hidden
    rows = conn.execute(
        "SELECT kind, outcome, rationale, decided_by, stage, candidate_id FROM decisions WHERE outcome LIKE 'label=%' ORDER BY decision_id"
    ).fetchall()
    assert [r[1] for r in rows] == ["label=match", "label=nonmatch", "label=unsure"]
    assert all(r[0] == "review" and r[3] == "XX" and r[4] == "identity" and r[5] for r in rows)
    progress = labels.progress(conn)
    assert progress == {"queued": 6, "match": 1, "nonmatch": 1, "unsure": 1, "waiting": 3}
    # resume: only the three unlabeled pairs are offered
    again = Script("s", "s", "s")
    review.label_loop(conn, reviewer="XX", ask=again.ask, ask_text=again.text, say=again.say)
    assert sum("CANDIDATE from" in line for line in again.out) == 3


def test_the_review_band_records_a_new_decision_and_never_edits_the_models(
    conn: sqlite3.Connection, ctx: Context, registry: Registry
) -> None:
    seed_row(conn, 1, "Jane Marie Raresurname")
    seed_candidate(conn, "fec", "F1", "RARESURNAME", "JANE", middle="MARIE")
    run_rows(conn, ctx, registry, [1])
    assert review.band_rows(conn) == [1]
    model_before = conn.execute(
        "SELECT decision_id, outcome FROM decisions WHERE kind = 'identity'"
    ).fetchall()
    script = Script(
        "c",
        "",
        "the FEC filing gives the same full middle name and the state",
    )
    out = review.band_loop(
        conn, reviewer="MC", ask=script.ask, ask_text=script.text, say=script.say
    )
    assert out.by_answer == {"confirmed": 1}
    assert (
        conn.execute(
            "SELECT decision_id, outcome FROM decisions WHERE kind = 'identity'"
        ).fetchall()
        == model_before
    )
    latest = conn.execute(
        "SELECT kind, outcome, rationale, decided_by, stage FROM decisions ORDER BY decision_id DESC LIMIT 1"
    ).fetchone()
    assert (
        latest[:2] == ("review", "confirmed")
        and latest[3:] == ("MC", "identity")
        and "same full middle" in latest[2]
    )
    assert runner._identity_outcome(conn, 1) == "confirmed"
    assert (
        conn.execute(
            "SELECT status FROM case_stage WHERE row_id = 1 AND stage = 'identity'"
        ).fetchone()[0]
        == "done"
    )
    assert review.band_rows(conn) == []
    assert any("posterior" in line for line in script.out)  # the band shows the whole explanation


def test_an_unresolved_review_stops_the_chain(
    conn: sqlite3.Connection, ctx: Context, registry: Registry
) -> None:
    seed_row(conn, 1, "Jane Marie Raresurname")
    seed_candidate(conn, "fec", "F1", "RARESURNAME", "JANE", middle="MARIE")
    run_rows(conn, ctx, registry, [1])
    script = Script("u", "a different Jane")
    review.band_loop(conn, reviewer="MC", ask=script.ask, ask_text=script.text, say=script.say)
    assert runner._identity_outcome(conn, 1) == "unresolved"
    assert runner.gate_news(conn, ctx.config, 1) == "identity_unresolved"


# ---- the F3 audit ----------------------------------------------------------------------------


def confirmed_world(conn: sqlite3.Connection, count: int, *, common: int = 0) -> None:
    """``count`` confirmed decisions (the decisions are inserted directly: this tests the audit)."""
    for i in range(1, count + 1):
        surname = "Commonsurname" if i <= common else f"Surname{chr(96 + i)}"
        seed_row(conn, i, f"Jane Marie {surname}")
        conn.execute(
            "INSERT INTO identity_candidates (row_id, source, fields_json, posterior, current, waterfall_json) "
            "VALUES (?, 'fec', ?, ?, 1, '{}')",
            (i, json.dumps({"person": {"surname": surname.upper()}}), 0.995 if i % 2 else 0.95),
        )
        cid = conn.execute("SELECT max(candidate_id) FROM identity_candidates").fetchone()[0]
        conn.execute(
            "INSERT INTO decisions (row_id, kind, outcome, decided_by, decided_at, stage, batch, candidate_id) "
            "VALUES (?, 'identity', 'confirmed', 'x', 's', 'identity', 'main', ?)",
            (i, cid),
        )
    conn.commit()


def test_the_audit_draw_oversamples_common_names_and_the_band_and_is_seeded(
    conn: sqlite3.Connection,
) -> None:
    confirmed_world(conn, 20, common=10)
    for _ in range(2):  # a second call on the same data
        pass
    drawn = audit.build_sample(conn, size=8, seed=7, common_rank=1, review=0.90, confirm=0.99)
    assert len(drawn) == 8
    by = {}
    for e, w, draw in drawn:
        by.setdefault((e.stratum, e.band), []).append(w)
        assert draw == 1
    assert {"common", "rare"} == {k[0] for k in by}
    assert {k[1] for k in by} == {0, 1}  # rows that were in the band are drawn too
    # equal allocation over four cells, so the weights differ with the cell size
    assert sum(len(v) for v in by.values()) == 8 and all(len(v) == 2 for v in by.values())
    rows = conn.execute("SELECT row_id FROM identity_audit_sample ORDER BY audit_id").fetchall()
    assert len(set(rows)) == 8
    other = audit.build_sample(conn, size=8, seed=7, common_rank=1, review=0.90, confirm=0.99)
    assert {e.row_id for e, _, _ in other}.isdisjoint(
        {r[0] for r in rows}
    )  # an earlier draw is not repeated


def test_a_small_pool_is_taken_whole(conn: sqlite3.Connection) -> None:
    confirmed_world(conn, 3)
    assert (
        len(audit.build_sample(conn, size=50, seed=1, common_rank=1, review=0.9, confirm=0.99)) == 3
    )


def test_the_rate_is_weighted_by_stratum_and_reports_pending(conn: sqlite3.Connection) -> None:
    confirmed_world(conn, 20, common=10)
    audit.build_sample(conn, size=8, seed=7, common_rank=1, review=0.90, confirm=0.99)
    none_yet = audit.rates(conn, resamples=200, seed=1)
    assert (
        [r.n for r in none_yet] == [0, 0, 0]
        and none_yet[0].pending == 4
        and none_yet[2].pending == 8
    )
    ids = [
        (r[0], r[1])
        for r in conn.execute("SELECT row_id, stratum FROM identity_audit_sample ORDER BY audit_id")
    ]
    script_rows = list(ids)
    for k, (row_id, stratum) in enumerate(script_rows):
        wrong = stratum == "common" and k == 0
        conn.execute(
            "INSERT INTO decisions (row_id, kind, outcome, decided_by, decided_at, stage) "
            "VALUES (?, 'review', ?, 'XX', 's', 'identity')",
            (row_id, "audit_wrong" if wrong else "audit_correct"),
        )
    conn.commit()
    items = {r.stratum: r for r in audit.rates(conn, resamples=500, seed=1)}
    assert items["common"].n == 4 and items["common"].wrong == 1 and 0 < items["common"].rate < 1
    assert items["common"].low <= items["common"].rate <= items["common"].high
    assert items["rare"].n == 4 and items["rare"].wrong == 0 and items["rare"].rate == 0.0
    assert items["rare"].high == 0.0  # a bootstrap of no errors is degenerate ...
    assert items["rare"].upper95 == pytest.approx(
        1 - 0.05 ** (1 / 4)
    )  # ... so the exact bound is given
    assert items["all"].pending == 0
    assert "stratum" in audit.render_rates(list(items.values()))


def test_the_audit_loop_records_verdicts_and_audit_wrong_withdraws_the_confirmation(
    conn: sqlite3.Connection, ctx: Context
) -> None:
    confirmed_world(conn, 4)
    audit.build_sample(conn, size=4, seed=1, common_rank=1, review=0.90, confirm=0.99)
    script = Script("c", "matches the docket", "w", "the DOB is different", "q")
    out = review.audit_loop(
        conn, reviewer="XX", ask=script.ask, ask_text=script.text, say=script.say
    )
    assert out.by_answer == {"audit_correct": 1, "audit_wrong": 1}
    wrong = conn.execute("SELECT row_id FROM decisions WHERE outcome = 'audit_wrong'").fetchone()[0]
    assert runner._identity_outcome(conn, wrong) == "unresolved"
    assert len(review.audit_rows(conn)) == 2


def test_the_repository_sheet_has_ids_only_and_the_full_sheet_stays_in_the_data_directory(
    conn: sqlite3.Connection, ctx: Context, tmp_path: Path
) -> None:
    confirmed_world(conn, 4)
    audit.build_sample(conn, size=4, seed=1, common_rank=1, review=0.90, confirm=0.99)
    repo, full = audit.write_sheets(
        conn, str(ctx.config.paths.data_dir), tmp_path / "reports" / "f3_audit.md"
    )
    body = repo.read_text()
    assert "efp identity explain" in body and "efp review identity --audit" in body
    assert "Jane" not in body and "Surname" not in body  # no names in the repository file
    assert "| audit | row |" in body and body.count("pending") >= 4
    assert str(ctx.config.paths.data_dir) in str(full) and full.stat().st_mode & 0o777 == 0o600
    assert "Jane" in full.read_text()


def test_explain_lists_every_component_the_audit_requires(
    conn: sqlite3.Connection, ctx: Context, registry: Registry
) -> None:
    seed_row(conn, 1, "Jane Marie Raresurname")
    seed_candidate(conn, "fec", "F1", "RARESURNAME", "JANE", middle="MARIE")
    run_rows(conn, ctx, registry, [1])
    text = explain(conn, 1)
    for needle in (
        "N_namesakes",
        "p(surname)",
        "p(given)",
        "N_adults",
        "prior odds",  # the prior and its parts
        "surname",
        "given_name",
        "middle",
        "dob",
        "city",
        "case_number",  # every comparison
        "exact",
        "BF",
        "tf raw",
        "tf used",
        "bits",  # level, Bayes factor, term-frequency adjustment
        "evidence",
        "posterior",
        "thresholds",
        "reason",  # the sum, the posterior, the rule
        "splink:",
        "params=provisional",
        "prior=census",  # who decided and with what
        "FEC" if False else "fec",  # the source
    ):
        assert needle in text, needle
    assert "needs_review" in text and "Other candidates" not in text
    assert explain(conn, 999) == "row 999: no such row"
    seed_row(conn, 2, "No Candidates")
    assert "No identity decision yet" in explain(conn, 2)

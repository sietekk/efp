"""Re-scoring stored candidates: ``efp identity refit``, ``rescore`` and the 0.995 sensitivity run.

Candidate gathering is not repeated (it reads documents and reference tables and is deterministic
given them); the stored comparison inputs are scored again with the current parameters, a prior
recomputed from the current reference tables, and the decision rule at the requested thresholds.

* the **main** batch (after a labeled fit, or after the Census and SSA files are imported) writes a
  new candidate score and a new decision only for a row whose outcome changes, moves its
  ``case_stage`` row, and sets the dependent ``news`` / ``social`` rows that were skipped for lack
of
  an identity back to ``pending`` so exactly those rows run again. A row the operator has already
  decided is left alone: a refit never overrides a human.
* the **sensitivity** batch (any confirm threshold other than the pre-registered one is forced into
  it) writes decisions only, never touches ``case_stage`` and is never read by a gate.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from efp import identity as stage
from efp.identity import decide as dec
from efp.identity import model, store
from efp.identity.pairs import Pair
from efp.identity.params import settings_sha
from efp.identity.prior import Prior
from efp.identity.profile import Person
from efp.types import Context

CHUNK = 400


@dataclass
class RescoreSummary:
    batch: str
    rows: int = 0
    changed: list[int] = field(default_factory=list)
    unchanged: int = 0
    skipped_reviewed: int = 0
    outcomes: Counter[str] = field(default_factory=Counter)
    settings: str = ""
    decided_by: str = ""


def _person(fields: dict[str, Any]) -> Person:
    p = fields["person"]
    return Person(
        source=p["source"], source_id=p["source_id"], given=p["given"], middle=p["middle"],
        surname=p["surname"], suffix=p.get("suffix", ""), state=p.get("state"),
        extras=p.get("extras", {}), alias_index=int(p.get("alias_index", 0)),
    )  # fmt: skip


def _pair(pair_id: int, fields: dict[str, Any], prior: Prior) -> Pair:
    return Pair(
        pair_id, fields["compared"]["left"], fields["compared"]["right"], prior,
        fields.get("used", {}), fields.get("masked", []), fields.get("prior_inputs", {}),
    )  # fmt: skip


def _latest(conn: sqlite3.Connection, row_id: int, batch: str) -> tuple[int, str, str] | None:
    found = conn.execute(
        "SELECT decision_id, outcome, rationale FROM decisions WHERE row_id = ? "
        "AND kind = 'identity' "
        "AND batch = ? ORDER BY decision_id DESC LIMIT 1",
        (row_id, batch),
    ).fetchone()
    return None if found is None else (int(found[0]), str(found[1]), str(found[2] or ""))


def _human_after(conn: sqlite3.Connection, row_id: int, decision_id: int) -> bool:
    human = store.human_decision(conn, row_id)
    return human is not None and human[0] > decision_id


def _move_stage(
    conn: sqlite3.Connection, row_id: int, outcome: str, *, was: str | None = None
) -> None:
    status = "needs_review" if outcome == "needs_review" else "done"
    conn.execute(
        "UPDATE case_stage SET status = ?, gate_reason = NULL "
        "WHERE row_id = ? AND stage = 'identity'",
        (status, row_id),
    )
    if was == "confirmed" and outcome != "confirmed":
        store.demote_downstream(conn, row_id)  # what ran on a confirmation is run again
    if outcome == "confirmed":  # rows skipped because identity was unresolved run again
        conn.execute(
            "UPDATE case_stage SET status = 'pending', gate_reason = NULL, input_hash = NULL "
            "WHERE row_id = ? AND stage IN ('news', 'social') AND status = 'skipped' "
            "AND gate_reason LIKE 'identity_%'",
            (row_id,),
        )


def rescore(
    ctx: Context,
    settings: model.Settings,
    *,
    confirm: float | None = None,
    review: float | None = None,
    batch: str = "main",
) -> RescoreSummary:
    conn = ctx.db
    assert conn is not None  # noqa: S101
    ident = ctx.config.identity
    confirm = ident.confirm_threshold if confirm is None else confirm
    review = ident.review_threshold if review is None else review
    if batch == "main" and (confirm, review) != (ident.confirm_threshold, ident.review_threshold):
        batch = "sensitivity"  # thresholds other than the pre-registered ones never touch main
    res = stage.Resources(ctx, settings)
    sha = settings_sha(settings)
    out = RescoreSummary(batch, settings=settings.name)
    ids = [
        int(r[0])
        for r in conn.execute(
            "SELECT DISTINCT row_id FROM identity_candidates WHERE current = 1 ORDER BY row_id"
        )
    ]
    for start in range(0, len(ids), CHUNK):
        chunk = ids[start : start + CHUNK]
        per_row: dict[int, list[tuple[int, Person, Pair]]] = {}
        pairs: list[dict[str, Any]] = []
        next_id = 1
        for row_id in chunk:
            for candidate_id, raw in conn.execute(
                "SELECT candidate_id, fields_json FROM identity_candidates "
                "WHERE row_id = ? AND current = 1 ORDER BY candidate_id",
                (row_id,),
            ).fetchall():
                fields = json.loads(raw)
                inputs = fields.get("prior_inputs") or {}
                prior = res.prior.prior(**inputs) if inputs else Prior(1.0, {"ceiling": True})
                prior.components["cohort_basis"] = fields["prior"].get("cohort_basis")
                prior.components["widen_years"] = fields["prior"].get("widen_years")
                pair = _pair(next_id, fields, prior)
                if inputs:
                    pair.age_boost_bits = res.prior.age_boost_bits(inputs, prior)
                pair.flags = list(fields.get("flags", []))
                next_id += 1
                per_row.setdefault(row_id, []).append((int(candidate_id), _person(fields), pair))
                pairs.append({"pair_id": pair.pair_id, "left": pair.left, "right": pair.right})
        waterfalls = model.score(pairs, settings, tf=res.tf, cap=ident.tf_adjustment_cap)
        for row_id in chunk:
            out.rows += 1
            scored = []
            for candidate_id, person, pair in per_row.get(row_id, []):
                item = dec.score_candidate(person, pair, waterfalls[pair.pair_id])
                item.candidate_id = candidate_id
                scored.append(item)
            decision = dec.decide(scored, confirm=confirm, review=review)
            tag = stage.decided_by(
                settings,
                {s.pair.prior.mode for s in scored} or {res.prior.default_mode},
                batch=batch,
            )
            out.decided_by = tag
            out.outcomes[decision.outcome] += 1
            before = _latest(conn, row_id, batch)
            with store.atomic(conn):
                if batch == "main":
                    if before is not None and _human_after(conn, row_id, before[0]):
                        out.skipped_reviewed += 1
                        continue
                    if before is not None and before[1] == decision.outcome:
                        out.unchanged += 1
                        continue
                    store.write_candidates(conn, row_id, scored, settings_sha=sha)
                    store.write_decision(
                        conn, row_id, decision, decided_by=tag, thresholds=(confirm, review)
                    )
                    _move_stage(conn, row_id, decision.outcome, was=before[1] if before else None)
                    out.changed.append(row_id)
                else:
                    if before is not None and before[1] == decision.outcome:
                        previous = json.loads(before[2] or "{}").get("thresholds", {})
                        if previous == {"confirm": confirm, "review": review}:
                            out.unchanged += 1
                            continue
                    store.write_decision(
                        conn,
                        row_id,
                        decision,
                        decided_by=tag,
                        thresholds=(confirm, review),
                        batch=batch,
                    )
                    out.changed.append(row_id)
    return out

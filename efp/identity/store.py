"""Writing identity results: candidates (with the prior and the waterfall) and decisions.

``decisions`` and ``captures`` are append-only (triggers); ``identity_candidates`` is not, but a
score is never rewritten either: a rescore appends new rows and flips the old ones to
``current = 0``, so the audit can still read what an earlier decision was made from.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from efp.identity.decide import Decision, Scored
from efp.identity.profile import Person


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@contextmanager
def atomic(conn: sqlite3.Connection) -> Iterator[None]:
    """One transaction, whether the connection autocommits (the runner) or not (the CLI)."""
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


HUMAN_OUTCOMES = ("confirmed", "unresolved", "audit_wrong")


def human_decision(conn: sqlite3.Connection, row_id: int) -> tuple[int, str] | None:
    """The operator's latest decision on the row's identity (a review that confirms or leaves it
    unresolved, or an audit verdict that withdraws a confirmation), or None."""
    marks = ",".join("?" * len(HUMAN_OUTCOMES))
    found = conn.execute(
        "SELECT decision_id, outcome FROM decisions WHERE row_id = ? AND batch = 'main' "  # noqa: S608
        f"AND kind IN ('review', 'override') AND stage = 'identity' AND outcome IN ({marks}) "
        "ORDER BY decision_id DESC LIMIT 1",
        (row_id, *HUMAN_OUTCOMES),
    ).fetchone()
    return None if found is None else (int(found[0]), str(found[1]))


def demote_downstream(conn: sqlite3.Connection, row_id: int) -> None:
    """A row that is no longer confirmed: the stages that ran because it was are set to pending so
    they run again (and are skipped at their gate)."""
    conn.execute(
        "UPDATE case_stage SET status = 'pending', gate_reason = NULL, input_hash = NULL "
        "WHERE row_id = ? AND stage IN ('news', 'social') "
        "AND status IN ('done', 'needs_review', 'failed')",
        (row_id,),
    )


def sync_downstream(
    conn: sqlite3.Connection, row_id: int, outcome: str, *, was: str | None
) -> None:
    """Keep the later stages in step with a change of outcome, whoever caused it (a refit, a
    re-score through the runner, the operator): a row no longer confirmed has the stages that ran
    on its confirmation set to pending (they are skipped at their gate), and a row that is now
    confirmed has the news and social rows skipped for lack of an identity set to pending."""
    if was == "confirmed" and outcome != "confirmed":
        demote_downstream(conn, row_id)
    if outcome == "confirmed":
        conn.execute(
            "UPDATE case_stage SET status = 'pending', gate_reason = NULL, input_hash = NULL "
            "WHERE row_id = ? AND stage IN ('news', 'social') AND status = 'skipped' "
            "AND gate_reason LIKE 'identity_%'",
            (row_id,),
        )


def person_json(person: Person) -> dict[str, Any]:
    return {
        "source": person.source,
        "source_id": person.source_id,
        "given": person.given,
        "middle": person.middle,
        "surname": person.surname,
        "suffix": person.suffix,
        "state": person.state,
        "name": person.name_value.as_dict() if person.name_value else None,
        "values": {k: [v.as_dict() for v in vs] for k, vs in person.values.items()},
        "extras": person.extras,
        "alias_index": person.alias_index,
    }


def candidate_fields(scored: Scored) -> dict[str, Any]:
    pair = scored.pair
    return {
        "person": person_json(scored.candidate),
        "prior": pair.prior.as_dict(),
        "prior_inputs": pair.prior_inputs,
        "compared": {"left": pair.left, "right": pair.right},
        "used": pair.used,
        "masked": pair.masked,
        "flags": pair.flags,
    }


def write_candidates(
    conn: sqlite3.Connection, row_id: int, scored: list[Scored], *, settings_sha: str
) -> None:
    """Supersede the row's current candidates and append the new scores."""
    conn.execute(
        "UPDATE identity_candidates SET current = 0 WHERE row_id = ? AND current = 1", (row_id,)
    )
    stamp = now()
    for item in scored:
        cursor = conn.execute(
            "INSERT INTO identity_candidates (row_id, source, fields_json, prior_namesakes, "
            "match_weight, posterior, waterfall_json, settings_sha, scored_at, current, "
            "alias_index) "
            "VALUES (?,?,?,?,?,?,?,?,?,1,?)",
            (
                row_id,
                item.candidate.source,
                json.dumps(candidate_fields(item), sort_keys=True),
                item.pair.prior.n_namesakes,
                item.waterfall["evidence_bits"],
                item.posterior,
                json.dumps(
                    {
                        **item.waterfall,
                        "prior_odds": item.pair.prior.odds,
                        "posterior": item.posterior,
                    },
                    sort_keys=True,
                ),
                settings_sha,
                stamp,
                item.candidate.alias_index,
            ),
        )
        item.candidate_id = int(cursor.lastrowid or 0)


def evidence_ids(scored: Scored | None) -> list[int]:
    if scored is None:
        return []
    ids: set[int] = set()
    for sides in scored.pair.used.values():
        for value in sides.values():
            if value.get("capture_id"):
                ids.add(int(value["capture_id"]))
    name = scored.candidate.name_value
    if name and name.capture_id:
        ids.add(name.capture_id)
    return sorted(ids)


def write_decision(
    conn: sqlite3.Connection,
    row_id: int,
    decision: Decision,
    *,
    decided_by: str,
    thresholds: tuple[float, float],
    batch: str = "main",
) -> int:
    best = decision.best
    rationale = {
        "reason": decision.reason,
        "posterior": best.posterior if best else None,
        "thresholds": {"confirm": thresholds[0], "review": thresholds[1]},
        "agreeing": best.agreeing if best else [],
        "competing": [r.candidate_id for r in decision.competing],
    }
    cursor = conn.execute(
        "INSERT INTO decisions (row_id, kind, outcome, rationale, decided_by, decided_at, "
        "evidence_ids, stage, batch, candidate_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            row_id,
            "identity",
            decision.outcome,
            json.dumps(rationale, sort_keys=True),
            decided_by,
            now(),
            json.dumps(evidence_ids(best)),
            "identity",
            batch,
            best.candidate_id if best else None,
        ),
    )
    return int(cursor.lastrowid or 0)

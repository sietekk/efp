"""The 150-pair labeling queue (spec section 8: m and u from a hand-labeled subsample).

Stratified by surname (common / rare), by whether either side has a date of birth or an age, and by
source (court / reference), drawn with a fixed seed across the whole posterior range (not only near
the thresholds, or the fit would learn only the hard cases). The queue holds candidate ids; the
operator's labels are ``decisions`` of kind ``review`` with outcome ``label=match``,
``label=nonmatch`` or ``label=unsure``. Nothing here labels a pair.
"""

from __future__ import annotations

import json
import random  # seeded sampling for a reproducible queue, not security
import sqlite3
from dataclasses import dataclass
from typing import Any

from efp.identity import store
from efp.identity.strata import Strata


@dataclass
class QueueSummary:
    added: int
    total: int
    by_stratum: dict[str, int]
    short: dict[str, int]


def _stratum(strata: Strata, fields: dict[str, Any], source: str) -> str:
    person = fields["person"]
    compared = fields["compared"]
    has_age = any(compared[side].get("by_lo") is not None for side in ("left", "right"))
    return "/".join(
        (
            strata.label(person["surname"]),
            "dated" if has_age else "undated",
            "court" if source == "court" else "reference",
        )
    )


def build_queue(
    conn: sqlite3.Connection, *, size: int, seed: int, common_rank: int
) -> QueueSummary:
    """Top the queue up to ``size`` pairs (an existing queue is never reshuffled)."""
    strata = Strata(conn, common_rank)
    queued = {int(r[0]) for r in conn.execute("SELECT candidate_id FROM identity_label_queue")}
    pool: dict[str, list[tuple[float, int, int]]] = {}
    for candidate_id, row_id, source, raw, posterior in conn.execute(
        "SELECT candidate_id, row_id, source, fields_json, posterior FROM identity_candidates "
        "WHERE current = 1 ORDER BY candidate_id"
    ):
        if int(candidate_id) in queued:
            continue
        key = _stratum(strata, json.loads(raw), str(source))
        pool.setdefault(key, []).append((float(posterior or 0), int(candidate_id), int(row_id)))
    need = size - len(queued)
    out = QueueSummary(0, len(queued), {}, {})
    if need <= 0 or not pool:
        return out
    rng = random.Random(seed)  # noqa: S311
    # one candidate per row at most, spread over the posterior range inside each stratum
    chosen: dict[str, list[tuple[float, int, int]]] = {}
    for key, items in pool.items():
        rng.shuffle(items)
        seen_rows: set[int] = set()
        unique: list[tuple[float, int, int]] = []
        for item in items:
            if item[2] not in seen_rows:
                seen_rows.add(item[2])
                unique.append(item)
        unique.sort(key=lambda t: t[0])
        chosen[key] = unique
    picks: dict[str, list[tuple[float, int, int]]] = {k: [] for k in chosen}
    keys = sorted(chosen)
    remaining = need
    while remaining > 0 and any(chosen[k] for k in keys):
        for key in keys:
            if remaining <= 0 or not chosen[key]:
                continue
            items = chosen[key]
            # alternate the lowest, the highest and the middle of what is left: the whole range
            slot = len(picks[key]) % 3
            index = 0 if slot == 0 else len(items) - 1 if slot == 1 else len(items) // 2
            picks[key].append(items.pop(index))
            remaining -= 1
    stamp = store.now()
    position = len(queued)
    with store.atomic(conn):
        for key in keys:
            for _, candidate_id, row_id in picks[key]:
                position += 1
                conn.execute(
                    "INSERT INTO identity_label_queue (candidate_id, row_id, stratum, position, "
                    "seed, "
                    "built_at) VALUES (?,?,?,?,?,?)",
                    (candidate_id, row_id, key, position, seed, stamp),
                )
                out.added += 1
                out.by_stratum[key] = out.by_stratum.get(key, 0) + 1
    out.total = len(queued) + out.added
    out.short = {k: len(v) for k, v in chosen.items() if not v}
    return out


def pending(conn: sqlite3.Connection) -> list[tuple[int, int, str]]:
    """``(candidate_id, row_id, stratum)`` of queued pairs with no label yet, in queue order."""
    return [
        (int(c), int(r), str(s))
        for c, r, s in conn.execute(
            "SELECT q.candidate_id, q.row_id, q.stratum FROM identity_label_queue q "
            "WHERE NOT EXISTS (SELECT 1 FROM decisions d WHERE d.kind = 'review' "
            "AND d.stage = 'identity' "
            "AND d.candidate_id = q.candidate_id AND d.outcome LIKE 'label=%') ORDER BY q.position"
        )
    ]


def progress(conn: sqlite3.Connection) -> dict[str, int]:
    labeled = {"match": 0, "nonmatch": 0, "unsure": 0}
    latest: dict[int, str] = {}
    for candidate_id, outcome in conn.execute(
        "SELECT candidate_id, outcome FROM decisions WHERE kind = 'review' AND stage = 'identity' "
        "AND outcome LIKE 'label=%' AND candidate_id IS NOT NULL ORDER BY decision_id"
    ):
        latest[int(candidate_id)] = str(outcome).split("=", 1)[1]
    for value in latest.values():
        labeled[value] = labeled.get(value, 0) + 1
    queued = int(conn.execute("SELECT count(*) FROM identity_label_queue").fetchone()[0])
    return {"queued": queued, **labeled, "waiting": len(pending(conn))}

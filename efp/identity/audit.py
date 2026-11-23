"""The F3 audit: 50 confirmed rows, re-derived by hand, and the false-confirmation rate.

The draw oversamples what is most likely to be wrong: common surnames and rows whose posterior was
ever in the review band. Allocation is equal across the non-empty cells of
(common / rare) x (never in the band / was in the band), capped by each cell's size, with a fixed
seed; the inclusion weight of a draw is cell size over cell sample, so the estimate is corrected for
the oversampling. The operator's verdicts are ``decisions`` of kind ``review`` with outcome
``audit_correct`` or ``audit_wrong`` and a rationale. The rate is reported separately for common
and rare surnames with a percentile bootstrap interval; with no wrong verdict a bootstrap
interval is degenerate, so the exact one-sided 95% upper bound is given as well.
"""

from __future__ import annotations

import json
import random  # seeded sampling for a reproducible audit, not security
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from efp.identity import store
from efp.identity.strata import Strata

CELLS = [("common", 1), ("common", 0), ("rare", 1), ("rare", 0)]


@dataclass
class Eligible:
    row_id: int
    decision_id: int
    stratum: str
    band: int


def confirmed_rows(
    conn: sqlite3.Connection, strata: Strata, review: float, confirm: float
) -> list[Eligible]:
    out = []
    for row_id, decision_id, surname in conn.execute(
        "SELECT d.row_id, d.decision_id, c.fields_json FROM decisions d "
        "LEFT JOIN identity_candidates c ON c.candidate_id = d.candidate_id "
        "WHERE d.kind = 'identity' AND d.batch = 'main' AND d.outcome = 'confirmed' "
        "AND d.decision_id = (SELECT max(decision_id) FROM decisions x WHERE x.row_id = d.row_id "
        "AND x.kind = 'identity' AND x.batch = 'main') ORDER BY d.row_id"
    ):
        name = json.loads(surname)["person"]["surname"] if surname else ""
        band = conn.execute(
            "SELECT 1 FROM identity_candidates WHERE row_id = ? AND posterior >= ? "
            "AND posterior < ? LIMIT 1",
            (row_id, review, confirm),
        ).fetchone()
        was_review = conn.execute(
            "SELECT 1 FROM decisions WHERE row_id = ? AND kind = 'identity' "
            "AND outcome = 'needs_review' LIMIT 1",
            (row_id,),
        ).fetchone()
        out.append(
            Eligible(
                int(row_id), int(decision_id), strata.label(name), int(bool(band or was_review))
            )
        )
    return out


def build_sample(
    conn: sqlite3.Connection,
    *,
    size: int,
    seed: int,
    common_rank: int,
    review: float,
    confirm: float,
) -> list[tuple[Eligible, float, int]]:
    """Draw the next sample; rows already audited in an earlier draw are not drawn again."""
    strata = Strata(conn, common_rank)
    drawn = {int(r[0]) for r in conn.execute("SELECT row_id FROM identity_audit_sample")}
    draw = int(
        conn.execute("SELECT coalesce(max(draw), 0) + 1 FROM identity_audit_sample").fetchone()[0]
    )
    pool = [e for e in confirmed_rows(conn, strata, review, confirm) if e.row_id not in drawn]
    cells = {cell: [e for e in pool if (e.stratum, e.band) == cell] for cell in CELLS}
    live = [c for c in CELLS if cells[c]]
    if not live:
        return []
    rng = random.Random(seed + draw)  # noqa: S311
    quota = {c: 0 for c in live}
    remaining = min(size, sum(len(cells[c]) for c in live))
    while remaining > 0:
        for c in live:
            if remaining > 0 and quota[c] < len(cells[c]):
                quota[c] += 1
                remaining -= 1
    out: list[tuple[Eligible, float, int]] = []
    stamp = store.now()
    with store.atomic(conn):
        for c in live:
            picked = rng.sample(cells[c], quota[c])
            weight = len(cells[c]) / quota[c]
            for e in picked:
                conn.execute(
                    "INSERT INTO identity_audit_sample (row_id, decision_id, stratum, band, "
                    "weight, "
                    "draw, seed, drawn_at) VALUES (?,?,?,?,?,?,?,?)",
                    (e.row_id, e.decision_id, e.stratum, e.band, weight, draw, seed + draw, stamp),
                )
                out.append((e, weight, draw))
    return out


def verdicts(conn: sqlite3.Connection) -> dict[int, str]:
    latest: dict[int, str] = {}
    for row_id, outcome in conn.execute(
        "SELECT row_id, outcome FROM decisions WHERE kind = 'review' AND stage = 'identity' "
        "AND outcome IN ('audit_correct','audit_wrong') ORDER BY decision_id"
    ):
        latest[int(row_id)] = str(outcome)
    return latest


@dataclass
class Rate:
    stratum: str
    n: int
    wrong: int
    rate: float | None
    low: float | None
    high: float | None
    upper95: float | None
    pending: int = 0
    notes: list[str] = field(default_factory=list)


def _weighted(sample: list[tuple[float, int]]) -> float:
    total = sum(w for w, _ in sample)
    return sum(w * x for w, x in sample) / total if total else 0.0


def rates(conn: sqlite3.Connection, *, resamples: int, seed: int) -> list[Rate]:
    done = verdicts(conn)
    rows = conn.execute(
        "SELECT row_id, stratum, weight FROM identity_audit_sample ORDER BY audit_id"
    ).fetchall()
    out = []
    for stratum in ("common", "rare", "all"):
        picked = [r for r in rows if stratum == "all" or r[1] == stratum]
        judged = [
            (float(r[2]), 1 if done[int(r[0])] == "audit_wrong" else 0)
            for r in picked
            if int(r[0]) in done
        ]
        pending = sum(1 for r in picked if int(r[0]) not in done)
        n, wrong = len(judged), sum(x for _, x in judged)
        if n == 0:
            out.append(Rate(stratum, 0, 0, None, None, None, None, pending, ["no verdicts yet"]))
            continue
        rng = random.Random(seed)  # noqa: S311
        draws = sorted(_weighted([rng.choice(judged) for _ in judged]) for _ in range(resamples))
        low, high = draws[int(0.025 * resamples)], draws[min(int(0.975 * resamples), resamples - 1)]
        upper = 1 - 0.05 ** (1 / n) if wrong == 0 else None
        notes = []
        if wrong == 0:
            notes.append(
                "no wrong verdict: the bootstrap interval is degenerate; use the exact upper bound"
            )
        out.append(Rate(stratum, n, wrong, _weighted(judged), low, high, upper, pending, notes))
    return out


def render_rates(items: list[Rate]) -> str:
    lines = ["stratum   judged  wrong   rate    95% bootstrap     exact upper 95%   pending"]
    for r in items:
        if r.rate is None:
            lines.append(
                f"{r.stratum:<9}{r.n:>6}{r.wrong:>7}   -       -                 -"
                f"                 {r.pending}"
            )
            continue
        up = "-" if r.upper95 is None else f"{r.upper95:.3f}"
        lines.append(
            f"{r.stratum:<9}{r.n:>6}{r.wrong:>7}   {r.rate:.3f}   "
            f"[{r.low:.3f}, {r.high:.3f}]     {up:<17} {r.pending}"
        )
    return "\n".join(lines)


def write_sheets(
    conn: sqlite3.Connection, config_data_dir: str, repo_sheet: Path
) -> tuple[Path, Path]:
    """The repository sheet holds ids, strata and commands only; the sheet with the names and the
    verbatim spans is written under the data directory, because it is row-level identity data."""
    rows = conn.execute(
        "SELECT a.audit_id, a.row_id, a.stratum, a.band, a.weight, a.draw, r.name, r.state, r.year "
        "FROM identity_audit_sample a JOIN rows r ON r.row_id = a.row_id ORDER BY a.audit_id"
    ).fetchall()
    done = verdicts(conn)
    head = [
        "# F3 audit sample",
        "",
        "Each confirmed row below is to be re-derived by hand from its captures. Print the",
        "evidence with `efp identity explain <row_id>`, open the captures it lists, decide, and",
        "record the verdict with `efp review identity --audit` (outcome `audit_correct` or",
        "`audit_wrong`, with a rationale).",
        "Then `efp identity audit rate` gives the false-confirmation rate by stratum.",
        "",
        "This file holds row ids only. The names and the verbatim spans are in the data directory",
        "(`exports/identity/f3_audit_full.md`) and never leave it.",
        "",
        "| audit | row | draw | stratum | was in review band | weight | verdict |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for a in rows:
        head.append(
            f"| {a[0]} | {a[1]} | {a[5]} | {a[2]} | {'yes' if a[3] else 'no'} | {a[4]:.2f} | "
            f"{done.get(int(a[1]), 'pending')} |"
        )
    repo_sheet.parent.mkdir(parents=True, exist_ok=True)
    repo_sheet.write_text("\n".join(head) + "\n", encoding="utf-8")
    from efp.identity.explain import explain

    full_path = Path(config_data_dir) / "exports" / "identity" / "f3_audit_full.md"
    full_path.parent.mkdir(parents=True, exist_ok=True)
    body = ["# F3 audit sample (full: names and spans; stays in the data directory)", ""]
    for a in rows:
        body += [
            f"## Audit {a[0]}: row {a[1]} ({a[2]}, draw {a[5]}), "
            f"verdict: {done.get(int(a[1]), 'pending')}",
            "",
            "```",
            explain(conn, int(a[1])),
            "```",
            "",
        ]
    full_path.write_text("\n".join(body), encoding="utf-8")
    full_path.chmod(0o600)
    return repo_sheet, full_path

"""``efp identity explain <row_id>``: everything behind one identity decision.

Prints the decision and the model that made it, the population prior with each of its components,
every comparison's level, m, u and Bayes factor (and the term-frequency adjustment, raw and as
applied), the evidence and the posterior against both thresholds, the verbatim span behind each
field the two sides agreed on (with its capture id), anything dropped under the independence rule,
and the other candidates considered. This is what the F3 audit and the review band both use.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any


def _pct(p: float | None) -> str:
    return "n/a" if p is None else f"{p:.4f}"


def _value(item: dict[str, Any] | None) -> str:
    if not item:
        return "(none)"
    span = f'  "{item["span"]}"' if item.get("span") else ""
    where = f" [capture {item['capture_id']}]" if item.get("capture_id") else " [the row itself]"
    return f"{item['value']}{where}{span}"


def prior_lines(prior: dict[str, Any]) -> list[str]:
    info = prior.get("p_given_info", {})
    n_info = prior.get("n_adults_info", {})
    n_namesakes = prior.get("n_namesakes", 0)
    odds = prior.get("prior_odds", 0)
    sex = info.get("sex", "?") + (", ambiguous: both sexes pooled" if info.get("ambiguous") else "")
    ages = n_info.get("ages", "all adults")
    lines = [
        f"  N_namesakes   {n_namesakes:,.2f}   (prior odds 1/N = {odds:.6g})",
        f"    p(surname)  {prior.get('p_surname', 0):.3e}   ({prior.get('p_surname_how')})",
        f"    p(given)    {prior.get('p_given', 0):.3e}   ({info.get('how')}; sex {sex}; "
        f"birth years {info.get('cohort', 'all')})",
        f"    N_adults    {prior.get('n_adults', 0):,.0f}   ({n_info.get('how')}; ages {ages}; "
        f"vintage {n_info.get('vintage', '?')})",
    ]
    notes = []
    if prior.get("floored_at_one"):
        raw = prior.get("raw_namesakes", 0)
        notes.append(f"raw count {raw:.3g} floored at one (the person is a namesake of themself)")
    if prior.get("ceiling"):
        notes.append(
            "CEILING prior: the Census / SSA files are not loaded, so the most common "
            "surname and given name are assumed"
        )
    if prior.get("cohort_free"):
        notes.append("no age or date of birth: cohort-free prior over all adults (weak)")
    else:
        basis, widen = prior.get("cohort_basis"), prior.get("widen_years", 0)
        notes.append(f"cohort from the {basis}, widened {widen} years")
    return [*lines, *(f"    note        {n}" for n in notes)]


def _recorded_name(person: dict[str, Any]) -> str:
    if person.get("name"):
        return _value(person["name"])
    parts = (person.get("given"), person.get("middle"), person.get("surname"))
    return " ".join(p for p in parts if p)


def _decision_lines(decisions: list[Any]) -> list[str]:
    out = ["Decisions (oldest first):"]
    for d in decisions:
        label = f"{d[1]}/{d[7]}"
        out.append(f"  #{d[0]} {d[5]} {label:<18} {d[2]:<14} by {d[4]}")
        if not d[3]:
            continue
        try:
            r = json.loads(d[3])
        except ValueError:
            out.append(f"      rationale: {d[3]}")
            continue
        out.append(
            f"      reason {r.get('reason')}; posterior {_pct(r.get('posterior'))}; "
            f"thresholds {r.get('thresholds')}; agreeing {r.get('agreeing')}"
        )
    return out


def _evidence_lines(water: dict[str, Any]) -> list[str]:
    head = ("comparison", "level", "m", "u", "BF", "tf raw", "tf used", "bits")
    out = [
        "",
        "Evidence (log2 of the Bayes factor m/u; the name carries none, the prior has it)",
        f"  {head[0]:<12}{head[1]:<18}{head[2]:>10}{head[3]:>12}{head[4]:>12}"
        f"{head[5]:>9}{head[6]:>9}{head[7]:>8}",
    ]
    if not water.get("steps"):
        return [*out, "  (no waterfall stored for this candidate)"]
    for s in water["steps"]:
        m = "-" if s["m"] is None else f"{s['m']:.4g}"
        u = "-" if s["u"] is None else f"{s['u']:.4g}"
        out.append(
            f"  {s['comparison']:<12}{s['level']:<18}{m:>10}{u:>12}{s['bayes_factor']:>12.4g}"
            f"{s['tf_adjustment_raw']:>9.3g}{s['tf_adjustment_used']:>9.3g}{s['log2_weight']:>8.2f}"
        )
        left, right = s.get("left"), s.get("right")
        if s["level"] != "null" and (left is not None or right is not None):
            out.append(f"      row: {left}   candidate: {right}")
    bits = water["evidence_bits"]
    out += [
        f"  evidence {bits:+.2f} bits  (x{2**bits:.4g})",
        f"  posterior = prior odds {water['prior_odds']:.6g} x 2^{bits:.2f} "
        f"-> {_pct(water['posterior'])}",
    ]
    return out


_DECISIONS_SQL = (
    "SELECT decision_id, kind, outcome, rationale, decided_by, decided_at, candidate_id, batch "
    "FROM decisions WHERE (row_id = ? AND stage = 'identity') "
    "OR (row_id = ? AND kind = 'identity') ORDER BY decision_id"
)


def explain(conn: sqlite3.Connection, row_id: int) -> str:
    row = conn.execute("SELECT name, state, year FROM rows WHERE row_id = ?", (row_id,)).fetchone()
    if row is None:
        return f"row {row_id}: no such row"
    out = [f"Row {row_id}: {row[0]} ({row[1]}, {row[2]})"]
    decisions = conn.execute(_DECISIONS_SQL, (row_id, row_id)).fetchall()
    if not decisions:
        out.append("No identity decision yet (the identity stage has not run for this row).")
        return "\n".join(out)
    out += _decision_lines(decisions)
    model_decision = next(
        (d for d in reversed(decisions) if d[1] == "identity" and d[7] == "main"), None
    )
    candidate_id = model_decision[6] if model_decision else None
    cands = conn.execute(
        "SELECT candidate_id, source, fields_json, prior_namesakes, match_weight, posterior, "
        "waterfall_json, settings_sha, current FROM identity_candidates WHERE row_id = ? "
        "ORDER BY posterior DESC",
        (row_id,),
    ).fetchall()
    chosen = next((c for c in cands if c[0] == candidate_id), None) or next(
        (c for c in cands if c[8]), None
    )
    if chosen is None:
        out.append("No candidates were gathered: none of the sources had a person to compare.")
        return "\n".join(out)
    fields, water = json.loads(chosen[2]), json.loads(chosen[6])
    person = fields["person"]
    out += [
        "",
        f"Candidate #{chosen[0]} from {chosen[1]} ({person.get('source_id')}), "
        f"scored with settings {str(chosen[7])[:12]}",
        f"  name as recorded: {_recorded_name(person)}",
    ]
    for kind, vals in person.get("values", {}).items():
        out += [f"  {kind}: {_value(v)}" for v in vals[:2]]
    out += ["", "Population prior", *prior_lines(fields.get("prior", {}))]
    out += _evidence_lines(water)
    spans = fields.get("used", {})
    if spans:
        out += ["", "Fields compared, with the verbatim text:"]
        out += [f"  {k} ({s}): {_value(i)}" for k, sides in spans.items() for s, i in sides.items()]
    if fields.get("masked"):
        out += ["", "Dropped (the same document on both sides is one observation):"]
        out += [
            f"  {m['kind']} ({m['side']}): {m['value']} [capture {m.get('capture_id')}]"
            for m in fields["masked"]
        ]
    others = [c for c in cands if c[0] != chosen[0] and c[8]]
    if others:
        out += ["", "Other candidates:"]
        for c in others[:10]:
            who = json.loads(c[2])["person"].get("source_id")
            out.append(f"  #{c[0]} {c[1]} ({who}) posterior {_pct(c[5])}")
    return "\n".join(out)

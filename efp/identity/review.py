"""The operator's three queues: ``efp review identity`` (the review band), ``--label`` (the 150
pairs that fit m and u) and ``--audit`` (the F3 sample).

Every answer is a new ``decisions`` row of kind ``review`` with the reviewer's initials and a
rationale; the model's own decision is never edited. The loops take their input and output as
functions so the tests can drive them; the CLI passes ``typer`` prompts.

Labeling is blind: a pair is shown with both sides' fields, spans and sources, but not the model's
score or outcome, so the labels do not echo the model they are used to fit.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from efp import runner
from efp.identity import audit, labels, rescore, store
from efp.identity.explain import _value, explain

Ask = Callable[[str, Sequence[str]], str]
AskText = Callable[[str], str]
Say = Callable[[str], None]


@dataclass
class Tally:
    answered: int = 0
    skipped: int = 0
    by_answer: dict[str, int] = field(default_factory=dict)

    def count(self, answer: str) -> None:
        self.answered += 1
        self.by_answer[answer] = self.by_answer.get(answer, 0) + 1


def record(
    conn: sqlite3.Connection,
    row_id: int,
    outcome: str,
    *,
    rationale: str,
    reviewer: str,
    candidate_id: int | None,
) -> int:
    cursor = conn.execute(
        "INSERT INTO decisions (row_id, kind, outcome, rationale, decided_by, decided_at, "
        "evidence_ids, stage, batch, candidate_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            row_id,
            "review",
            outcome,
            rationale,
            reviewer,
            store.now(),
            None,
            "identity",
            "main",
            candidate_id,
        ),
    )
    return int(cursor.lastrowid or 0)


def runner_outcome(conn: sqlite3.Connection, row_id: int) -> str | None:
    return runner._identity_outcome(conn, row_id)


def _name(person: dict) -> str:  # type: ignore[type-arg]
    if person.get("name"):
        return _value(person["name"])
    parts = (person.get("given"), person.get("middle"), person.get("surname"))
    return " ".join(p for p in parts if p)


def render_pair(conn: sqlite3.Connection, candidate_id: int) -> str:
    found = conn.execute(
        "SELECT c.row_id, c.source, c.fields_json, r.name, r.state, r.year, r.details "
        "FROM identity_candidates c JOIN rows r ON r.row_id = c.row_id WHERE c.candidate_id = ?",
        (candidate_id,),
    ).fetchone()
    if found is None:
        return f"candidate {candidate_id}: not found"
    fields = json.loads(found[2])
    person = fields["person"]
    used = fields.get("used", {})
    out = [
        f"ROW {found[0]}: {found[3]}  ({found[4]}, {found[5]})",
        f"  details: {found[6] or '(none)'}",
    ]
    for kind, sides in used.items():
        if "left" in sides:
            out.append(f"  row says {kind}: {_value(sides['left'])}")
    out += [
        "",
        f"CANDIDATE from {found[1]} ({person.get('source_id')})",
        f"  name: {_name(person)}",
        f"  state: {person.get('state')}",
    ]
    extras = person.get("extras", {})
    for key in ("office", "years", "courts", "source_kinds"):
        if extras.get(key):
            out.append(f"  {key}: {extras[key]}")
    for kind, vals in person.get("values", {}).items():
        for v in vals[:3]:
            out.append(f"  {kind}: {_value(v)}")
    return "\n".join(out)


def label_loop(
    conn: sqlite3.Connection, *, reviewer: str, ask: Ask, ask_text: AskText, say: Say,
    limit: int | None = None,
) -> Tally:  # fmt: skip
    tally = Tally()
    queue = labels.pending(conn)
    say(f"{len(queue)} pairs waiting; {labels.progress(conn)}")
    for candidate_id, row_id, stratum in queue:
        if limit is not None and tally.answered + tally.skipped >= limit:
            break
        say("\n" + "=" * 72 + f"\nstratum {stratum}\n" + render_pair(conn, candidate_id))
        choice = ask(
            "Same person? [m]atch  [n]on-match  [u]nsure  [s]kip  [q]uit", ("m", "n", "u", "s", "q")
        )
        if choice == "q":
            break
        if choice == "s":
            tally.skipped += 1
            continue
        word = {"m": "match", "n": "nonmatch", "u": "unsure"}[choice]
        why = ask_text("Why (a line; what in the text decides it)")
        with store.atomic(conn):
            record(
                conn,
                row_id,
                f"label={word}",
                rationale=why,
                reviewer=reviewer,
                candidate_id=candidate_id,
            )
        tally.count(word)
    return tally


def band_rows(conn: sqlite3.Connection) -> list[int]:
    return [
        int(r[0])
        for r in conn.execute(
            "SELECT row_id FROM case_stage WHERE stage = 'identity' "
            "AND status = 'needs_review' ORDER BY row_id"
        )
    ]


def band_loop(
    conn: sqlite3.Connection, *, reviewer: str, ask: Ask, ask_text: AskText, say: Say,
    limit: int | None = None,
) -> Tally:  # fmt: skip
    tally = Tally()
    rows = band_rows(conn)
    say(f"{len(rows)} rows in the review band")
    for row_id in rows:
        if limit is not None and tally.answered + tally.skipped >= limit:
            break
        say("\n" + "=" * 72 + "\n" + explain(conn, row_id))
        choice = ask(
            "Decision: [c]onfirm this person  [u]nresolved  [s]kip  [q]uit", ("c", "u", "s", "q")
        )
        if choice == "q":
            break
        if choice == "s":
            tally.skipped += 1
            continue
        why = ask_text("Rationale (required)")
        while not why.strip():
            why = ask_text("A rationale is required")
        outcome = "confirmed" if choice == "c" else "unresolved"
        prior_outcome = runner_outcome(conn, row_id)
        candidate = conn.execute(
            "SELECT candidate_id FROM decisions WHERE row_id = ? AND kind = 'identity' "
            "AND batch = 'main' "
            "ORDER BY decision_id DESC LIMIT 1",
            (row_id,),
        ).fetchone()
        with store.atomic(conn):
            record(
                conn,
                row_id,
                outcome,
                rationale=why,
                reviewer=reviewer,
                candidate_id=candidate[0] if candidate else None,
            )
            rescore._move_stage(conn, row_id, outcome, was=prior_outcome)
        tally.count(outcome)
    return tally


def audit_rows(conn: sqlite3.Connection) -> list[int]:
    done = audit.verdicts(conn)
    return [
        int(r[0])
        for r in conn.execute("SELECT row_id FROM identity_audit_sample ORDER BY audit_id")
        if int(r[0]) not in done
    ]


def audit_loop(
    conn: sqlite3.Connection, *, reviewer: str, ask: Ask, ask_text: AskText, say: Say,
    limit: int | None = None,
) -> Tally:  # fmt: skip
    tally = Tally()
    rows = audit_rows(conn)
    say(f"{len(rows)} audit rows without a verdict")
    for row_id in rows:
        if limit is not None and tally.answered + tally.skipped >= limit:
            break
        say("\n" + "=" * 72 + "\n" + explain(conn, row_id))
        say("Re-derive this from the captures, not from the score above.")
        choice = ask(
            "Verdict: is it the right person? [c]orrect  [w]rong  [s]kip  [q]uit",
            ("c", "w", "s", "q"),
        )
        if choice == "q":
            break
        if choice == "s":
            tally.skipped += 1
            continue
        why = ask_text("Rationale")
        outcome = "audit_correct" if choice == "c" else "audit_wrong"
        with store.atomic(conn):
            record(conn, row_id, outcome, rationale=why, reviewer=reviewer, candidate_id=None)
            if outcome == "audit_wrong":  # the confirmation is withdrawn: what ran on it runs again
                store.demote_downstream(conn, row_id)
        tally.count(outcome)
    return tally

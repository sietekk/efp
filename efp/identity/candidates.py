"""Candidate modules (``[stages.identity].candidate_modules``, policy ``all``).

Each module gathers person records for one row from one source and returns them as
:class:`~efp.identity.profile.Person` objects; none of them scores anything. A module whose source
is not loaded, or whose flag is off, is skipped with the reason (the chain machinery records it)
and the stage carries on with what is there.

* ``identity.cand.court``: the court records the court stage found for the row, one candidate per
  distinct case, with the identifiers read from them and the name as the court document writes it.
* ``identity.cand.dime`` / ``fec`` / ``ftm`` / ``harvard_local`` / ``legislators``: public-figure
  records from ``ref_candidates_all``, found by surname and state for the row's name and for every
  ``aka`` alias (excluding aliases costs recall at no false-positive benefit, spec section 8), and
  kept when the given name is compatible (equal, a nickname, an initial or Jaro-Winkler >= 0.92).
* ``identity.cand.voter_file``: unavailable in this pass (``first_pass.voter_file_enrichment``).

Reference records carry no date of birth, city or case number, so for them the only possible
evidence beyond the name is a full middle name; the rest stays null, which is no evidence.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from pydantic import ConfigDict
from rapidfuzz.distance import JaroWinkler

from efp.court.identifiers import extract_identifiers
from efp.court.names import NameParts, match_name, parse_name
from efp.identity.normalize import case_key, initial_match, letters, load_nicknames
from efp.identity.profile import CaptureText, Person, Value
from efp.modules import Availability, ModuleResult, Outcome, Registry
from efp.reference.states import state_code
from efp.types import Context, Row

STAGE = "identity"


class IdentityTask(Row):
    """A row passing through the candidate chain with its left-hand persons."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    parts: Any
    people: Any
    texts: Any


def given_compatible(row_given: str, other: str) -> bool:
    a, b = letters(row_given), letters(other)
    if not a or not b:
        return True
    if a == b or initial_match(a, b) or load_nicknames().match(a, b):
        return True
    return float(JaroWinkler.similarity(a, b)) >= 0.92


def _loaded_sources(conn: sqlite3.Connection, sources: tuple[str, ...]) -> int:
    marks = ",".join("?" * len(sources))
    return int(
        conn.execute(
            f"SELECT count(*) FROM ref_candidates_all WHERE source IN ({marks})",  # noqa: S608
            sources,
        ).fetchone()[0]
    )


class _Reference:
    """Shared by the five reference-backed candidate modules."""

    stage = STAGE
    name: str
    sources: tuple[str, ...]
    dataset: str

    def available(self, ctx: Context) -> Availability:
        if ctx.db is None:
            return Availability.unavailable("no database")
        if _loaded_sources(ctx.db, self.sources) == 0:
            return Availability.unavailable(
                f"no {self.dataset} records in ref_candidates_all (dataset not loaded or gated; "
                f"see `efp reference status`)"
            )
        return Availability.available()

    def applies(self, row: Row, ctx: Context) -> bool | str:
        if not isinstance(row, IdentityTask):
            return "not an identity task"
        if not row.state:
            return "the row has no state, and a candidate is never gathered on a name alone"
        return True

    def enrich(self, conn: sqlite3.Connection, person: Person) -> None:
        """What a source carries beyond the index's columns (none by default)."""

    def run(self, row: Row, ctx: Context) -> ModuleResult:
        assert isinstance(row, IdentityTask) and ctx.db is not None  # noqa: S101
        marks = ",".join("?" * len(self.sources))
        found: dict[tuple[str, str], Person] = {}
        for person in row.people:
            key = letters(person.surname)
            if len(key) < 2:
                continue
            for rec in ctx.db.execute(
                "SELECT source, source_id, surname, given, middle, state, office, party, "  # noqa: S608
                "party_imputed, years, first_year, last_year, load_id FROM ref_candidates_all "
                f"WHERE surname_key = ? AND state = ? AND source IN ({marks})",
                (key, row.state, *self.sources),
            ):
                ident = (str(rec[0]), str(rec[1]))
                if ident in found or not given_compatible(person.given, str(rec[3] or "")):
                    continue
                found[ident] = Person(
                    source=str(rec[0]),
                    source_id=str(rec[1]),
                    given=str(rec[3] or ""),
                    middle=str(rec[4] or ""),
                    surname=str(rec[2] or ""),
                    state=str(rec[5]) if rec[5] else row.state,
                    docs=frozenset({f"ref:{rec[0]}:{rec[1]}"}),
                    name_value=Value(
                        " ".join(str(x) for x in (rec[3], rec[4], rec[2]) if x),
                        None,
                        None,
                        frozenset({f"ref:{rec[0]}:{rec[1]}"}),
                    ),
                    extras={
                        "office": rec[6], "party": rec[7], "party_imputed": rec[8], "years": rec[9],
                        "first_year": rec[10], "last_year": rec[11], "load_id": rec[12],
                    },
                    alias_index=person.alias_index,
                )  # fmt: skip
                self.enrich(ctx.db, found[ident])
        people = list(found.values())
        return ModuleResult(
            Outcome.hit if people else Outcome.miss,
            wrote={"candidates": people, "count": len(people)},
        )


class CandDime(_Reference):
    name = "identity.cand.dime"
    sources = ("dime",)
    dataset = "DIME"

    def enrich(self, conn: sqlite3.Connection, person: Person) -> None:
        """From the candidate's DIME rows (one per cycle): the latest suffix (compared with the
        row's), a nickname as an alias of the given name, and outcome, status, party code and
        FEC year kept as facts. Gender is carried and never used by the prior."""
        rows = conn.execute(
            "SELECT party_code, suffix, gender, won, primary_won, ico_status, fecyear, nickname "
            "FROM ref_dime_current WHERE bonica_rid = ? ORDER BY cycle DESC",
            (person.source_id,),
        ).fetchall()
        if not rows:
            return
        person.suffix = next((str(r[1]) for r in rows if r[1]), person.suffix)
        nicknames = sorted({str(r[7]) for r in rows if r[7]})
        extras: dict[str, Any] = {
            "party_code": next((r[0] for r in rows if r[0]), None),
            "gender": next((r[2] for r in rows if r[2]), None),
            "won": any(r[3] == 1 for r in rows),
            "primary_won": any(r[4] == 1 for r in rows),
            "ico_status": next((r[5] for r in rows if r[5]), None),
            "fecyear": max((int(r[6]) for r in rows if r[6]), default=None),
        }
        if nicknames:
            extras["nicknames"] = nicknames
        person.extras.update(extras)


class CandFec(_Reference):
    name = "identity.cand.fec"
    sources = ("fec",)
    dataset = "FEC"


class CandFtm(_Reference):
    name = "identity.cand.ftm"
    sources = ("ftm",)
    dataset = "FollowTheMoney"


class CandHarvardLocal(_Reference):
    name = "identity.cand.harvard_local"
    sources = ("harvard_local",)
    dataset = "Harvard local elections"


class CandLegislators(_Reference):
    name = "identity.cand.legislators"
    sources = ("congress", "state_leg")
    dataset = "congress-legislators or OpenStates"


class CandVoterFile:
    name = "identity.cand.voter_file"
    stage = STAGE

    def available(self, ctx: Context) -> Availability:
        if not ctx.config.first_pass.voter_file_enrichment:
            return Availability.unavailable(
                "first_pass.voter_file_enrichment is false: no voter-file source in this pass"
            )
        return Availability.unavailable("no voter-file source is built (excluded in this pass)")

    def applies(self, row: Row, ctx: Context) -> bool | str:
        return "no voter-file source"

    def run(self, row: Row, ctx: Context) -> ModuleResult:
        return ModuleResult(Outcome.miss)


class CandCourt:
    name = "identity.cand.court"
    stage = STAGE

    def available(self, ctx: Context) -> Availability:
        if ctx.db is None:
            return Availability.unavailable("no database")
        return Availability.available()

    def applies(self, row: Row, ctx: Context) -> bool | str:
        if not isinstance(row, IdentityTask) or ctx.db is None:
            return "not an identity task"
        found = ctx.db.execute(
            "SELECT 1 FROM court_records_current WHERE row_id = ? LIMIT 1", (row.row_id,)
        ).fetchone()
        return True if found else "the court stage found no record for this row"

    def run(self, row: Row, ctx: Context) -> ModuleResult:
        assert isinstance(row, IdentityTask) and ctx.db is not None  # noqa: S101
        people = court_candidates(ctx.db, row.texts, row.row_id, row.state, row.parts)
        return ModuleResult(
            Outcome.hit if people else Outcome.miss,
            wrote={"candidates": people, "count": len(people)},
        )


def court_candidates(
    conn: sqlite3.Connection,
    texts: CaptureText,
    row_id: int,
    row_state: str | None,
    parts: NameParts,
) -> list[Person]:
    """One person per distinct case among the row's current court records."""
    records = conn.execute(
        "SELECT record_id, source_kind, jurisdiction, court, case_number, capture_id, "
        "text_capture_id, weak, document_kind FROM court_records_current "
        "WHERE row_id = ? ORDER BY record_id",
        (row_id,),
    ).fetchall()
    groups: dict[str, list[Any]] = {}
    for rec in records:
        key = case_key(rec[4]) or f"record{rec[0]}"
        groups.setdefault(key, []).append(rec)
    out: list[Person] = []
    for key, recs in groups.items():
        docs: set[str] = set()
        text_ids: list[int] = []
        for rec in recs:
            if rec[5]:
                docs |= texts.docs(int(rec[5]))
            if rec[6]:
                docs |= texts.docs(int(rec[6]))
                text_ids.append(int(rec[6]))
        weak = all(int(r[7]) for r in recs)
        jurisdiction = next((state_code(r[2]) for r in recs if state_code(r[2])), None)
        person = Person(
            source="court", source_id=key, given=parts.given, middle="", surname=parts.surname,
            suffix=parts.suffix, state=jurisdiction or row_state, docs=frozenset(docs),
            extras={
                "weak": weak,
                "name_basis": "row_name",
                "records": [int(r[0]) for r in recs],
                "source_kinds": sorted({str(r[1]) for r in recs}),
                "courts": sorted({str(r[3]) for r in recs if r[3]}),
            },
        )  # fmt: skip
        for rec in recs:  # the name as the court document writes it, with its middle name
            if not rec[6] or person.name_value is not None:
                continue
            text = texts.text(int(rec[6]))
            found = match_name(parts, text) if text else None
            if found and found.full and not found.initial_only:
                written = parse_name(found.span)
                if written.surname:
                    person.given, person.middle = written.given or parts.given, written.middle
                    person.surname = written.surname
                    person.suffix = written.suffix or person.suffix
                    person.name_value = Value(
                        found.span, int(rec[6]), found.span, texts.docs(int(rec[6]))
                    )
                    person.extras["name_basis"] = "court_text"
        placeholders = ",".join("?" * len(text_ids))
        if text_ids:
            for kind, value, capture_id, span in conn.execute(
                "SELECT kind, value, capture_id, span FROM court_identifiers_current "  # noqa: S608
                f"WHERE row_id = ? AND capture_id IN ({placeholders}) "
                "ORDER BY identifier_id",
                (row_id, *text_ids),
            ):
                person.add(
                    str(kind),
                    Value(str(value), int(capture_id), span, texts.docs(int(capture_id))),
                )
        if not key.startswith("record"):
            person.add(
                "case_number", Value(str(recs[0][4]), recs[0][5], str(recs[0][4]), frozenset(docs))
            )
        out.append(person)
    _add_filings(conn, texts, row_id, parts, groups, out)
    return out


def _add_filings(
    conn: sqlite3.Connection,
    texts: CaptureText,
    row_id: int,
    parts: NameParts,
    groups: dict[str, list[Any]],
    people: list[Person],
) -> None:
    """Identifiers read from the documents stage's canonical text of the row's confirmed filings
    (plea agreements, charging documents): a middle name or an age a press release leaves out."""
    by_key = {key: person for (key, _), person in zip(groups.items(), people, strict=False)}
    for text_id, source_id, case_number, record_id in conn.execute(
        "SELECT d.text_capture_id, d.capture_id, cr.case_number, cr.record_id FROM documents d "
        "JOIN court_records_confirmed cr ON cr.record_id = d.record_id "
        "WHERE d.row_id = ? AND d.current = 1 AND d.text_capture_id IS NOT NULL "
        "ORDER BY d.document_id",
        (row_id,),
    ).fetchall():
        person = by_key.get(case_key(case_number) or f"record{record_id}")
        text = texts.text(int(text_id))
        if person is None or not text:
            continue
        docs = texts.docs(int(text_id)) | (texts.docs(int(source_id)) if source_id else frozenset())
        person.docs = person.docs | docs
        for found in extract_identifiers(text, parts):
            person.add(found.kind, Value(found.value, int(text_id), found.span, docs))
        if not person.middle and (named := match_name(parts, text)) and named.full:
            written = parse_name(named.span)
            if written.middle and not named.initial_only:
                person.middle = written.middle
                person.name_value = Value(named.span, int(text_id), named.span, docs)
                person.extras["name_basis"] = "filing_text"


MODULES = (CandCourt, CandDime, CandFec, CandFtm, CandHarvardLocal, CandLegislators, CandVoterFile)


def register_modules(registry: Registry) -> None:
    for module in MODULES:
        if registry.get(module.name) is None:
            registry.register(module)

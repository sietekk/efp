"""``court.state_docket``: court sub-strategy 4, the state docket portals.

What runs for a row is decided by the mode recorded for its state's portal (``state_adapters``,
``kind = 'docket'``), never by the code's own opinion of a portal:

* ``automated``: the state's adapter searches by name and year, but only for a state the operator
  has cleared in ``court.docket_terms_cleared``; a hit becomes a ``state_docket`` court record with
  identifiers, and the documents it lists are captured as PDFs for the documents stage.
* ``assisted``: nothing is searched. The row finishes with the constructed query attached
  (``summary_json.assisted``) and ``court_status = gated_portal``; it is not ``needs_review``, so
  later stages go on provisionally and ``efp review court --assisted`` lists it for the operator.
* ``manual``: the reason is recorded; a portal that wants an account or a fee marks the row
  ``gated_portal``.

A state with no probed portal is skipped with the reason and the chain goes on.
"""

from __future__ import annotations

import csv
import logging
import sqlite3

from efp.capture import CaptureError
from efp.court import identifiers as ident
from efp.court.adapters.ag.base import SiteBlocked
from efp.court.adapters.ag.sync import write as write_adapter
from efp.court.adapters.docket import DocketHit, adapters
from efp.court.adapters.docket.probe import Seed, load_seeds
from efp.court.adapters.docket.query import build_query
from efp.court.doj import normalize_state
from efp.court.located import LOOKBACK_YEARS, STAGE, CourtTask, Located, cap, store_text
from efp.court.names import match_name
from efp.modules import Availability, ModuleResult, Outcome
from efp.types import Context, Row

_SEEDS: list[Seed] | None = None


def seeds() -> list[Seed]:
    """The seed portal list, cached once it has been read. A failure to read it is not cached
    (a later call tries again) and is logged: without the list a county-level state would lose its
    county logic and silently take the statewide path."""
    global _SEEDS
    if _SEEDS is None:
        try:
            _SEEDS = load_seeds()
        except (OSError, KeyError, csv.Error) as exc:
            log.warning("reference/docket_portals.csv could not be read: %s", exc)
            return []
    return _SEEDS


def county_portal(state: str, text: str) -> Seed | None:
    """For a state whose docket portals are county-level (no statewide one in the seed list), the
    county portal that fits the row: the row's own text must name the county. A portal for one
    county is never made to stand in for the whole state."""
    mine = [s for s in seeds() if s.state == state]
    if not mine or any(s.scope == "statewide" for s in mine):
        return None
    haystack = text.lower()
    for seed in mine:
        county = seed.scope.removeprefix("county:").strip().lower()
        if county and f"{county} county" in haystack:
            return seed
    return None


def platform_of(portal_url: str) -> str:
    for seed in seeds():
        if seed.url == portal_url:
            return seed.platform
    return "portal"


log = logging.getLogger("efp.docket")
BLOCK_LIMIT = 3  # refusals within one run before a state's portal is recorded as blocked
MAX_DOCUMENTS = 5
GATING_WANTS = ("account", "fee")
# The one manual reason that is a statement about the world rather than about our probe: the seed
# says the state has no online docket search by name (county clerk by phone). The strategy has
# nothing to search, so the row stays ``not_found`` instead of waiting on a portal that is not
# there.
NO_ONLINE_SEARCH = "no-online-search"


def recorded_portal(conn: sqlite3.Connection | None, state: str) -> tuple[str, str, str] | None:
    """(mode, reason, portal) recorded for a state's docket portal."""
    if conn is None:
        return None
    found = conn.execute(
        "SELECT mode, coalesce(reason, ''), coalesce(portal, '') FROM state_adapters "
        "WHERE state = ? AND kind = 'docket'",
        (state,),
    ).fetchone()
    return None if found is None else (found[0], found[1], found[2])


def wants_of(reason: str) -> str:
    """The reason is written ``<wants>: <detail>`` by the probe."""
    return reason.split(":", 1)[0].strip() if ":" in reason else ""


class CourtStateDocket:
    name = "court.state_docket"
    stage = STAGE

    def available(self, ctx: Context) -> Availability:
        return Availability.available()

    def applies(self, row: Row, ctx: Context) -> bool | str:
        task: CourtTask = row  # type: ignore[assignment]
        cs = task.cs
        if cs.classification.kind == "federal":
            return "classified as a federal case: the federal modules apply"
        if not cs.parts.usable:
            return "no usable name"
        if not row.year:
            return "the row has no year: the filing window cannot be built"
        state = normalize_state(row.state)
        if state is None:
            return "the row's state is not a state or territory"
        found = recorded_portal(ctx.db, state)
        if found is None:
            return f"no docket portal probed for {state}: run `efp court docket probe`"
        if found[0] in ("none", "broken"):
            return f"the {state} docket portal is {found[0]}: {found[1]}"
        return True

    def run(self, row: Row, ctx: Context) -> ModuleResult:
        task: CourtTask = row  # type: ignore[assignment]
        cs = task.cs
        state = normalize_state(row.state) or ""
        found = recorded_portal(ctx.db, state)
        if found is None:
            return ModuleResult(Outcome.miss)
        mode, reason, portal = found
        cfg = ctx.config.court
        if mode in ("assisted", "manual") and not seeds():
            cs.notes.append("the docket portal list could not be read: county logic not applied")
        if mode in ("assisted", "manual"):
            only_counties = bool([s for s in seeds() if s.state == state]) and not any(
                s.scope == "statewide" for s in seeds() if s.state == state
            )
            if only_counties:
                text = f"{row.source.get('details') or ''} {row.source.get('source_raw') or ''}"
                chosen = county_portal(state, text)
                if chosen is None and mode == "manual" and wants_of(reason) in GATING_WANTS:
                    # the state's portals want an account or a fee: gated whatever the county
                    cs.flags["gated_portal"] = True
                    cs.notes.append(
                        f"{state} docket portals are county-level and gated: {reason}"[:200]
                    )
                    return ModuleResult(Outcome.miss)
                if chosen is None:
                    # not searched and not absent: no portal was probed for this county, so the
                    # row is a coverage gap (deferred), never a recorded negative
                    cs.flags["docket_coverage_gap"] = True
                    cs.notes.append(
                        f"{state} has county-level docket portals only and the row names none of "
                        "the probed counties: no docket portal probed for this row's county"
                    )
                    return ModuleResult(Outcome.miss)
                portal = chosen.url  # this county's portal, not the state's best one
        if mode == "manual":
            wants = wants_of(reason)
            if wants in GATING_WANTS:
                cs.flags["gated_portal"] = True  # an account or a fee stands between the row and it
            elif wants != NO_ONLINE_SEARCH:
                # an entry point that is not there (a dead seed address) or any reason that is not a
                # verified "there is no online search": the docket search never ran, which is not
                # an absence, so the row is a coverage gap, never a recorded negative
                cs.flags["docket_coverage_gap"] = True
            cs.notes.append(f"{state} docket portal is manual: {reason}"[:200])
            return ModuleResult(Outcome.miss)
        adapter_cls = adapters().get(state)
        cleared = state in cfg.docket_terms_cleared
        # A surname alone is not a search an automated adapter can make (the portals match on a
        # given name too): the operator runs it, with the query constructed from what the row has.
        needs_person = bool(adapter_cls) and not cs.parts.given
        if mode == "assisted" or adapter_cls is None or not cleared or needs_person:
            cs.flags["gated_portal"] = True
            if needs_person:
                cs.notes.append(
                    f"{state} docket: the row has no given name; assisted, not searched"
                )
            cs.flags["assisted"] = build_query(
                portal_url=portal,
                platform=platform_of(portal),
                parts=cs.parts,
                year=row.year or 0,
                after=cfg.window_years_after,
                row_id=task.row_id,
            )
            if mode == "automated" and not cleared:
                cs.notes.append(f"{state} is no longer in court.docket_terms_cleared: assisted")
            return ModuleResult(Outcome.miss)
        adapter = adapter_cls(ctx)
        try:
            return self._automated(adapter, task, ctx, state, portal)
        except SiteBlocked as exc:
            refusals = cap(ctx).run_state.get(f"docket-refused-{state}", 0) + 1
            cap(ctx).run_state[f"docket-refused-{state}"] = refusals
            if refusals >= BLOCK_LIMIT and ctx.db is not None:
                write_adapter(
                    ctx.db,
                    state,
                    kind="docket",
                    mode="assisted",
                    portal=portal,
                    adapter_module=adapter_cls.__module__,
                    reason=f"blocked: {exc} ({refusals} refusals in one run)"[:200],
                )
            cs.notes.append(f"state docket {state}: {exc}"[:200])
            return ModuleResult(Outcome.error, error=f"state docket {state}: {exc}"[:200])
        except CaptureError as exc:
            cs.notes.append(f"state docket {state}: {exc}"[:200])
            return ModuleResult(Outcome.error, error=f"state docket {state}: {exc}"[:200])

    def _automated(
        self, adapter: object, task: CourtTask, ctx: Context, state: str, portal: str
    ) -> ModuleResult:
        cs = task.cs
        year = task.year or 0
        after = ctx.config.court.window_years_after  # the same window the assisted query uses
        name = f"{cs.parts.given} {cs.parts.surname}".strip()
        hits: list[DocketHit] = adapter.search(name, year, window=after)  # type: ignore[attr-defined]
        matched: list[tuple[DocketHit, bool]] = []
        for hit in hits:
            named = match_name(cs.parts, hit.caption)
            if named is None or not named.full:
                continue
            if hit.filed_date and not (
                year - LOOKBACK_YEARS <= hit.filed_date.year <= year + after
            ):
                continue
            matched.append((hit, named.initial_only))
        cap_hits = ctx.config.court.docket_max_hits
        truncated = bool(getattr(adapter, "search_truncated", False))
        over = len(matched) > cap_hits
        cs.flags["state_docket"] = {
            "state": state,
            "hits": len(hits),
            "matched": len(matched),
            "truncated": truncated,
            "over_cap": over,
        }
        if over:
            cs.notes.append(
                f"{state} docket: {len(matched)} dockets match the name; {cap_hits} recorded from "
                "the search answer, no detail pages or documents fetched (a common name)"
            )
        if truncated:
            cs.notes.append(f"{state} docket: the portal says its answer is incomplete (not paged)")
            if not matched:
                # nothing in a partial answer is no evidence of absence: the row is a coverage gap
                # (deferred) and the operator gets the query to run the whole search themselves
                cs.flags["docket_coverage_gap"] = True
                cs.flags["assisted"] = build_query(
                    portal_url=portal,
                    platform=platform_of(portal),
                    parts=cs.parts,
                    year=year,
                    after=after,
                    row_id=task.row_id,
                )
        any_case = False
        for position, (hit, initial_only) in enumerate(matched):
            if position >= cap_hits:
                break
            source_id = getattr(adapter, "search_capture_id", None)
            if over or source_id is None:
                record = None  # a common name: the search answer alone is the evidence
            else:
                record = adapter.fetch(hit, row_id=task.row_id)  # type: ignore[attr-defined]
                # a browser capture (WACZ) has no HTTP status of its own: its tool is the success
                if not (record.ok or record.tool == "browsertrix"):
                    cs.notes.append(
                        f"{state} docket page {hit.url}: HTTP {record.http_status}"[:200]
                    )
                    continue
            if record is None and source_id is None:
                continue
            evidence = (
                f"{hit.caption}\n{hit.court}\nCase No. {hit.case_number}\n"
                f"Filed {hit.filed_date.isoformat() if hit.filed_date else 'unknown'}"
            )
            # The caption, number and date come from the search answer, so that capture is the
            # record's source; the detail page is kept beside it.
            source_id = source_id or (record.capture_id if record else 0)
            text_id = store_text(
                ctx,
                task.row_id,
                parent_capture_id=source_id,
                url=hit.url,
                text=evidence,
                tool="state_docket_row",
                # derived, not verbatim: composed from the fields of the search answer. Spans
                # cited from this text point at it (its own capture); a later stage must treat it
                # as a derived artifact of the capture named here, not as the portal's own words.
                extra={"derived": True, "derived_from_capture_id": source_id},
            )
            numbers = [
                f.value
                for f in ident.extract_identifiers(evidence, cs.parts)
                if f.kind == "case_number"
            ] or [hit.case_number]
            documents = [] if over else self._documents(adapter, task, hit, cs.notes)
            weak = initial_only or len(matched) > 1 or truncated
            cs.located.append(
                Located(
                    source_kind="state_docket",
                    jurisdiction=state,
                    court=hit.court,
                    case_number=numbers[0],
                    document_kind="docket",
                    url=hit.url,
                    strategy=self.name,
                    capture_id=source_id,
                    text=evidence,
                    text_capture_id=text_id,
                    basis={
                        # the record's identity: case numbers repeat across counties
                        "release_uuid": f"{state}:{hit.court}:{hit.case_number}",
                        "detail_capture": record.capture_id if record else None,
                        "result_truncated": truncated,
                        "evidence": "composed from the fields of the search answer",
                        "initial_only": initial_only,
                        "several_hits": len(matched) > 1,
                        "filed_date": hit.filed_date.isoformat() if hit.filed_date else None,
                        "document_captures": documents,
                    },
                    weak=weak,
                )
            )
            any_case = any_case or not weak
        return ModuleResult(
            Outcome.hit if any_case else Outcome.miss, wrote={"matched": len(matched)}
        )

    @staticmethod
    def _documents(adapter: object, task: CourtTask, hit: DocketHit, notes: list[str]) -> list[int]:
        """Capture the PDFs a docket lists (for the documents stage); a failure is a note."""
        captured: list[int] = []
        for url in hit.document_urls[:MAX_DOCUMENTS]:
            try:
                record = cap(adapter.ctx).fetch(  # type: ignore[attr-defined]
                    url, row_id=task.row_id, stage=STAGE, kind="page"
                )
            except CaptureError as exc:
                notes.append(f"document not captured: {exc}"[:200])
                continue
            if record.ok:
                captured.append(record.capture_id)
        return captured

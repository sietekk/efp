"""``court.state_ag``: court sub-strategy 2, the state AG (and DA) press-release harvester.

The module of the court chain dispatches to the row's state adapter. A state with no adapter, one
that is ``manual`` or ``broken``, or whose archive does not reach the row's years, is skipped with
that reason and the chain continues. A release is a hit when it names the subject (surname and given
name, an alias, or an allowed press-release form), carries an election charge, and falls in the
row's year window; hits become ``court_records`` (``state_ag_release`` / ``da_release``) and go
through the same identifier extraction as the DOJ releases.
"""

from __future__ import annotations

from dataclasses import dataclass

from efp.capture import CaptureError
from efp.court import identifiers as ident
from efp.court.adapters.ag import Release, adapters, sync
from efp.court.adapters.ag.base import SiteBlocked, StateAdapter
from efp.court.doj import CASE_HEAD, ELECTION_TERMS, case_numbers, normalize_state
from efp.court.located import STAGE, CourtTask, Located, cap, store_text
from efp.court.names import NameParts, match_name
from efp.modules import Availability, ModuleResult, Outcome
from efp.resolve_links import text as page_text
from efp.types import Context, Row


def match_release_text(
    parts: NameParts, release: Release, text: str, *, row_year: int, slack: int
) -> tuple[str, str, bool] | None:
    """(name variant, matched span, initial only) if the release is about this subject.

    The subject must be named in the title or the opening of the text (a surname alone is not
    enough), the release must carry an election charge, and it must fall within ``slack`` years of
    the row. A release without a date is not placed in any window, so it never matches."""
    if release.date is None or abs(release.date.year - row_year) > slack:
        return None
    named = match_name(parts, f"{release.title}\n{text[: CASE_HEAD * 2]}")
    if named is None or not named.full:
        return None
    # the charge is stated in the title or the opening, not somewhere on the page (a sidebar, a
    # sex-offender registration notice): the same stretch that must name the subject
    if not ELECTION_TERMS.search(f"{release.title}\n{text[: CASE_HEAD * 2]}"):
        return None
    return named.variant, named.span, named.initial_only


def _adapter_for(ctx: Context, state: str) -> type[StateAdapter] | None:
    found = adapters().get(state)
    return found


BLOCK_LIMIT = 3  # refusals within one run before a state is recorded as manual


@dataclass
class _Hit:
    release: Release
    capture_id: int
    text_id: int
    text: str
    variant: str
    span: str
    initial_only: bool


class CourtStateAg:
    name = "court.state_ag"
    stage = STAGE

    def available(self, ctx: Context) -> Availability:
        automated = [s for s, cls in adapters().items() if getattr(cls, "mode", "") == "automated"]
        if not automated:
            return Availability.unavailable("no state AG adapter is automated")
        return Availability.available()

    def applies(self, row: Row, ctx: Context) -> bool | str:
        task: CourtTask = row  # type: ignore[assignment]
        cs = task.cs
        if cs.classification.kind == "federal":
            return "classified as a federal case: the DOJ and federal modules apply"
        if not cs.parts.usable:
            return "no usable name"
        if not row.year:
            return "the row has no year: the release window cannot be built"
        state = normalize_state(row.state)
        if state is None:
            return "the row's state is not a state or territory"
        cls = _adapter_for(ctx, state)
        if cls is None:
            return f"no state AG adapter for {state}"
        mode, reason = cls.mode, cls.reason
        if ctx.db is not None and (found := sync.recorded(ctx.db, state)) is not None:
            mode, reason = found[0], found[1] or reason  # the recorded state wins (broken, blocked)
        if mode != "automated":
            return f"the {state} AG adapter is {mode}: {reason}"
        slack = ctx.config.court.doj_year_slack
        adapter = cls(ctx)
        try:
            # Whether the archive can ever cover this window is only known once it has been
            # walked, so the first row of a state walks it (bounded; shared by every later row)
            # before the question is asked: a row the archive cannot reach is skipped with that
            # reason, never recorded as a miss.
            if (why := adapter.out_of_reach(row.year - slack)) is None:
                try:
                    adapter.refresh_newest()
                    adapter.harvest(row.year - slack)
                except CaptureError:
                    return True  # the run reports the failure; it is not a reason to skip
                cap(ctx).run_state[f"ag-walked-{adapter.state}"] = True  # this row's budget is used
                why = adapter.out_of_reach(row.year - slack)
        finally:
            adapter.close()
        return why or True

    def run(self, row: Row, ctx: Context) -> ModuleResult:
        task: CourtTask = row  # type: ignore[assignment]
        cs = task.cs
        state = normalize_state(row.state) or ""
        cls = _adapter_for(ctx, state)
        if cls is None:
            return ModuleResult(Outcome.miss)
        slack = ctx.config.court.doj_year_slack
        year = row.year or 0
        adapter = cls(ctx)
        try:
            return self._run(adapter, task, ctx, state, year, slack)
        except SiteBlocked as exc:
            # One refusal says little (a CDN throttle answers 403 too): the row is retried, and the
            # state is recorded as manual only once the site has refused repeatedly in this run.
            refusals = cap(ctx).run_state.get(f"ag-refused-{state}", 0) + 1
            cap(ctx).run_state[f"ag-refused-{state}"] = refusals
            if refusals >= BLOCK_LIMIT and ctx.db is not None:
                sync.mark(ctx.db, state, "manual", f"{exc} ({refusals} refusals in one run)"[:200])
            cs.notes.append(f"state AG {state}: {exc}"[:200])
            return ModuleResult(Outcome.error, error=f"state AG {state}: {exc}"[:200])
        except CaptureError as exc:
            cs.notes.append(f"state AG {state}: {exc}"[:200])
            return ModuleResult(Outcome.error, error=f"state AG {state}: {exc}"[:200])
        finally:
            adapter.close()
            cap(ctx).run_state.pop(f"ag-walked-{state}", None)  # never leaks into the next row

    def _run(
        self,
        adapter: StateAdapter,
        task: CourtTask,
        ctx: Context,
        state: str,
        year: int,
        slack: int,
    ) -> ModuleResult:
        cs = task.cs
        name = f"{cs.parts.given} {cs.parts.surname}".strip()
        releases = adapter.search(name, year, window=slack)
        covered = adapter.covered(year - slack)
        oldest = adapter.index.progress()[1]
        surname = cs.parts.surname.lower()
        # a title or summary that names the surname comes first, then the nearest year: the cap on
        # releases read must not push the subject's release out behind generic election news
        releases.sort(
            key=lambda r: (
                surname not in f"{r.title} {r.snippet}".lower(),
                abs((r.date.year if r.date else 0) - year),
            )
        )
        cap_n = ctx.config.court.ag_max_candidates
        if len(releases) > cap_n:
            cs.flags["ag_candidates_truncated"] = len(releases)
            cs.notes.append(
                f"{state} AG: {len(releases)} election-related releases in the window; read {cap_n}"
            )
        cs.flags["state_ag"] = {
            "state": state,
            "candidates": len(releases),
            "oldest_indexed": oldest,
        }
        if not covered:
            cs.flags["ag_coverage_gap"] = True
            cs.notes.append(
                f"{state} AG index reaches back only to {oldest or 'nothing'}; the row's window "
                f"starts {year - slack}: run `efp court ag harvest`, then `efp run court --recheck`"
            )
        found: list[_Hit] = []
        failed: list[str] = []
        for release in releases[:cap_n]:
            try:
                record = adapter.fetch(release, row_id=task.row_id)
            except CaptureError as exc:
                failed.append(str(exc))
                cs.notes.append(f"state AG release not captured: {exc}"[:200])
                continue
            if not record.ok:
                failed.append(f"HTTP {record.http_status}")
                cs.notes.append(f"state AG release {release.url}: HTTP {record.http_status}"[:200])
                continue
            text, _date = page_text.extract(cap(ctx).read_body(record), record.url_final)
            if not text:
                continue
            hit = match_release_text(cs.parts, release, text, row_year=year, slack=slack)
            if hit is None:
                continue
            text_id = store_text(
                ctx,
                task.row_id,
                parent_capture_id=record.capture_id,
                url=release.url,
                text=text,
                tool="trafilatura",
                version=page_text.TRAFILATURA_VERSION,
            )
            found.append(_Hit(release, record.capture_id, text_id, text, *hit))
        if not found:
            if failed and len(failed) == min(len(releases), cap_n):
                # every release that had to be read failed to load: that is not "no match"
                return ModuleResult(
                    Outcome.error, error=f"state AG {state}: no release could be read ({failed[0]})"
                )
            return ModuleResult(Outcome.miss, wrote={"candidates": len(releases)})
        several_matches = len(found) > 1
        any_case = False
        for h in found:
            release, text = h.release, h.text
            numbers = [
                f.value
                for f in ident.extract_identifiers(text, cs.parts)
                if f.kind == "case_number"
            ]
            several = len({v.lower() for v in case_numbers(text)}) > 1
            weak = h.initial_only or several or several_matches
            cs.located.append(
                Located(
                    source_kind="da_release"
                    if release.office.startswith("DA")
                    else "state_ag_release",
                    jurisdiction=state,
                    court=None,
                    case_number=numbers[0] if numbers else None,
                    document_kind="press_release",
                    url=release.url,
                    strategy=self.name,
                    capture_id=h.capture_id,
                    text=text,
                    text_capture_id=h.text_id,
                    basis={
                        "release_uuid": release.url,  # the stable identity of the record
                        "office": release.office,
                        "name_variant": h.variant,
                        "name_span": h.span,
                        "release_date": release.date.isoformat() if release.date else None,
                        "initial_only": h.initial_only,
                        "document_has_several_case_numbers": several,
                        "several_releases": several_matches,
                    },
                    weak=weak,
                )
            )
            any_case = any_case or (bool(numbers) and not weak)
        return ModuleResult(
            Outcome.hit if any_case else Outcome.miss, wrote={"matched": len(found)}
        )

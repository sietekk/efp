"""CourtListener REST v4 (``court.courtlistener_api``) and the RECAP documents step.

The REST API is the second resort: the bulk index is searched first and the API is used only for
dockets the index lacks (default quota 125 a day, budgeted at 100). When the daily budget is spent
a row is not blocked: the call fails fast, the row completes with that noted, and ``--reset``
re-attempts it another day. Rows classified federal go first in the sense that only they may use
the API at all.

Filings *already in the RECAP archive* are captured as PDFs for build 10. Nothing is ever bought:
no PACER fetch happens here (see ``recap.py``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from efp.capture import CaptureBudgetError, CaptureError
from efp.court.bulk import index_info, state_of_court
from efp.court.doj import normalize_state
from efp.court.located import (
    LOOKBACK_YEARS,
    STAGE,
    CourtTask,
    Located,
    cap,
    docket_is_weak,
    store_text,
)
from efp.court.names import NameParts, match_name
from efp.modules import Availability, ModuleResult, Outcome
from efp.secrets import SecretsError, load_secrets
from efp.types import Context, Row

SEARCH = "https://www.courtlistener.com/api/rest/v4/search/"
STORAGE = "https://storage.courtlistener.com/"
HOST = "https://www.courtlistener.com"
MAX_RECAP_DOCS = 5  # PDFs captured per docket: enough for build 10 to read the charging documents
# The per-request gap (0.8/s is 1.25 s) must fit inside this wait, so only a spent *daily* budget,
# which would take hours, fails fast; a quota is not worth waiting for.
THROTTLE_KEY = "courtlistener"
QUOTA_WAIT = 5.0


@dataclass
class ApiDocket:
    docket_id: int | None
    docket_number: str
    case_name: str
    court: str
    date_filed: str | None
    date_terminated: str | None
    url: str | None
    documents: list[dict[str, Any]] = field(default_factory=list)
    party_names: list[str] = field(default_factory=list)
    court_state: str | None = None  # set by search_party
    initial_only: bool = False  # set by search_party: only an initial and the surname matched


def token(ctx: Context) -> str | None:
    try:
        return load_secrets(ctx.config.paths.env_file).get("COURTLISTENER_TOKEN")
    except SecretsError:
        return None


def parse_dockets(payload: Any) -> list[ApiDocket]:
    out: list[ApiDocket] = []
    results = payload.get("results") if isinstance(payload, dict) else None
    for item in results or []:
        if not isinstance(item, dict):
            continue
        absolute = item.get("docket_absolute_url") or item.get("absolute_url")
        docs = [d for d in (item.get("recap_documents") or []) if isinstance(d, dict)]
        parties = item.get("party") or item.get("parties") or []
        out.append(
            ApiDocket(
                docket_id=int(item["docket_id"])
                if str(item.get("docket_id", "")).isdigit()
                else None,
                docket_number=str(item.get("docketNumber") or item.get("docket_number") or ""),
                case_name=str(item.get("caseName") or item.get("case_name") or ""),
                court=str(item.get("court") or item.get("court_id") or ""),
                date_filed=item.get("dateFiled") or item.get("date_filed"),
                date_terminated=item.get("dateTerminated") or item.get("date_terminated"),
                url=f"{HOST}{absolute}" if absolute else None,
                documents=docs,
                party_names=[str(p) for p in parties if isinstance(p, str)],
            )
        )
    return out


def _search(
    ctx: Context, secret: str, params: dict[str, str], row_id: int
) -> tuple[list[ApiDocket], int]:
    if cap(ctx).is_throttled(THROTTLE_KEY):
        # a 429 earlier in this run: do not spend more of the day's tokens asking again
        raise CaptureBudgetError("CourtListener throttled this run earlier (HTTP 429)")
    with cap(ctx).wait_limit(QUOTA_WAIT):
        record = cap(ctx).fetch(
            SEARCH,
            row_id=row_id,
            stage=STAGE,
            kind="api",
            params=params,
            headers={"Authorization": f"Token {secret}"},
            retry=False,  # a 429 is the answer: no retry, no Retry-After sleep, no second token
        )
    if record.http_status == 429:
        cap(ctx).mark_throttled(THROTTLE_KEY)
        raise CaptureBudgetError("CourtListener throttled the request (HTTP 429)")
    if not record.ok:
        raise CaptureError(f"CourtListener answered HTTP {record.http_status}")
    try:
        payload = json.loads(cap(ctx).read_body(record))
    except ValueError as exc:
        raise CaptureError("CourtListener did not return JSON") from exc
    return parse_dockets(payload), record.capture_id


def search_party(
    ctx: Context, secret: str, parts: NameParts, *, state: str | None, year: int, row_id: int
) -> tuple[list[ApiDocket], int]:
    """The best ``top_candidates`` of :func:`search_party_all`."""
    kept, capture_id = search_party_all(ctx, secret, parts, state=state, year=year, row_id=row_id)
    return kept, capture_id


def search_party_all(
    ctx: Context, secret: str, parts: NameParts, *, state: str | None, year: int, row_id: int
) -> tuple[list[ApiDocket], int]:
    if year <= 0:
        raise CaptureError("the row has no year: the filing window cannot be built")
    state = normalize_state(state)
    params = {
        "type": "r",
        "party_name": f"{parts.given} {parts.surname}".strip(),
        "filed_after": f"{year - LOOKBACK_YEARS}-01-01",
        "filed_before": f"{year + ctx.config.court.window_years_after}-12-31",
        "order_by": "dateFiled desc",
    }
    dockets, capture_id = _search(ctx, secret, params, row_id)
    kept: list[ApiDocket] = []
    for docket in dockets:
        named = match_name(parts, " | ".join([docket.case_name, *docket.party_names]))
        if not (named and named.full):
            continue
        court_state = state_of_court(docket.court)
        if state and court_state and court_state != state:
            continue
        docket.court_state = court_state
        docket.initial_only = named.initial_only
        kept.append(docket)
    return kept[: ctx.config.court.top_candidates], capture_id


class CourtApi:
    name = "court.courtlistener_api"
    stage = STAGE

    def available(self, ctx: Context) -> Availability:
        if not token(ctx):
            return Availability.unavailable(
                "COURTLISTENER_TOKEN not set (a free account's API token)"
            )
        if index_info(ctx.config) is None:
            # Bulk before API is a rule: without the local index the 100-a-day budget would be
            # spent on rows the index could have answered for free.
            return Availability.unavailable(
                "the bulk index is not loaded: bulk before API (efp court bulk fetch, then load)"
            )
        return Availability.available()

    def applies(self, row: Row, ctx: Context) -> bool | str:
        task: CourtTask = row  # type: ignore[assignment]
        cs = task.cs
        if cs.classification.kind != "federal":
            return "only rows classified federal may use the 100-a-day API budget"
        if not cs.parts.usable:
            return "no usable name"
        if not row.year:
            return "the row has no year: the filing window cannot be built"
        return True

    def run(self, row: Row, ctx: Context) -> ModuleResult:
        task: CourtTask = row  # type: ignore[assignment]
        cs = task.cs
        secret = token(ctx)
        if not secret:
            return ModuleResult(Outcome.miss)
        try:
            everything, capture_id = search_party_all(
                ctx, secret, cs.parts, state=row.state, year=row.year or 0, row_id=task.row_id
            )
            matched = len(everything)  # before truncation: top_candidates must not hide ambiguity
            dockets = everything[: ctx.config.court.top_candidates]
        except CaptureError as exc:
            quota = isinstance(exc, CaptureBudgetError)
            cs.flags["api_quota_exhausted"] = quota
            cs.notes.append(
                "CourtListener daily budget spent: re-run later with --reset"
                if quota
                else f"CourtListener: {exc}"[:200]
            )
            if quota:  # not a negative: the stage defers the row (and keeps earlier records)
                return ModuleResult(Outcome.miss)
            # an outage or a bad answer is a failure, not "no docket": the row is retried
            return ModuleResult(Outcome.error, error=f"CourtListener: {exc}"[:200])
        cs.flags["api"] = {"candidates": len(dockets)}
        for docket in dockets:
            evidence = (
                f"{docket.case_name}\ndocket number: {docket.docket_number}\ncourt: {docket.court}"
            )
            text_id = store_text(
                ctx,
                task.row_id,
                parent_capture_id=capture_id,
                url=docket.url or f"courtlistener:docket:{docket.docket_id}",
                text=evidence,
                tool="courtlistener_api_row",
            )
            cs.located.append(
                Located(
                    source_kind="federal_docket",
                    jurisdiction="US",
                    court=docket.court,
                    case_number=docket.docket_number or None,
                    document_kind="docket",
                    url=docket.url,
                    strategy=self.name,
                    capture_id=capture_id,
                    text=evidence,
                    text_capture_id=text_id,
                    basis={"recap_documents": len(docket.documents), "candidates": len(dockets)},
                    weak=docket_is_weak(
                        row_state=normalize_state(row.state),
                        court_state=docket.court_state,
                        candidates=matched,
                        initial_only=docket.initial_only,
                        date_known=bool(docket.date_filed),
                    ),
                    docket_id=docket.docket_id,
                )
            )
        confirmed = any(
            not item.weak for item in cs.located if item.strategy == self.name and item.case_number
        )
        return ModuleResult(Outcome.hit if confirmed else Outcome.miss)


@dataclass
class RecapOutcome:
    checked: bool
    documents: int
    reason: str = ""
    # Not looked up *for now* (no token, an API error, the daily budget spent): the docket's
    # documents are unknown, not absent, so it must not be queued for purchase, and the row is
    # retried by --recheck.
    transient: bool = False
    quota: bool = False  # the daily budget was the reason
    skipped: bool = False  # not asked, by design (not federal, no id): never queued for purchase


def capture_recap_documents(ctx: Context, task: CourtTask, docket: Located) -> RecapOutcome:
    """Capture the filings of a found docket that are already in RECAP (never from PACER)."""
    cs = task.cs
    secret = token(ctx)
    if docket.docket_id is None:
        return RecapOutcome(False, 0, "no CourtListener docket id", skipped=True)
    if cs.classification.kind != "federal":
        return RecapOutcome(
            False,
            0,
            "documents not checked: classification is not positively federal",
            skipped=True,  # RECAP was never asked: not a docket confirmed absent from it
        )
    if not secret:
        return RecapOutcome(
            False, 0, "documents not checked: COURTLISTENER_TOKEN not set", transient=True
        )
    try:
        dockets, _ = _search(
            ctx, secret, {"type": "r", "docket_id": str(docket.docket_id)}, task.row_id
        )
    except CaptureError as exc:
        return RecapOutcome(
            False,
            0,
            f"documents not checked: {exc}"[:200],
            transient=True,
            quota=isinstance(exc, CaptureBudgetError),
        )
    documents = [d for found in dockets for d in found.documents]
    taken = 0
    for doc in documents:
        path = doc.get("filepath_local")
        if not path or not doc.get("is_available", True):
            continue
        try:
            record = cap(ctx).fetch(
                STORAGE + str(path).lstrip("/"), row_id=task.row_id, stage=STAGE, kind="page"
            )
        except CaptureError:
            continue
        if not record.ok:
            continue
        cs.located.append(
            Located(
                source_kind="federal_docket",
                jurisdiction="US",
                court=docket.court,
                case_number=docket.case_number,
                document_kind="filing",
                url=record.url_final or record.url_requested,
                strategy="court.courtlistener_api",
                capture_id=record.capture_id,
                basis={"recap_document_id": doc.get("id"), "description": doc.get("description")},
                docket_id=docket.docket_id,
            )
        )
        taken += 1
        if taken >= MAX_RECAP_DOCS:
            break
    return RecapOutcome(
        True, taken, "" if taken else "the docket has no filings in the RECAP archive"
    )


_CASE_RE = re.compile(r"\d:\d{2}-cr-\d+", re.I)  # imported by tests for a stable shape check

"""Wisconsin: Circuit Court Access (WCCA), https://wcca.wicourts.gov/.

The prompt's expected automated candidate, **but WCCA is not automatable today**: its search is
gated by a CAPTCHA that its own page says "protects websites from abuse and unauthorized harvesting
or mining of data" and recognises "a human user, not an automated program" (read from the site's
client code on 2026-11-22). The seed list therefore marks it ``human-check`` and the probe records
WI ``assisted`` whatever ``court.docket_terms_cleared`` says. The adapter is kept for the day the
portal offers a route that does not need a human; it is not used until then.

The request and answer shapes were checked against WCCA's own client code, not by a search:
``POST jsonPost/advancedCaseSearch`` with the form's fields (``firstName``, ``lastName``,
``filingDate.start`` / ``.end``, ``includeMissingDob`` / ``includeMissingMiddleName`` …) as plain
values, answered with ``result.cases`` items carrying ``caseNo``, ``countyNo``, ``countyName`` and
``caption``. Still unconfirmed: the date format the server expects, whether case items carry a
filing date, and any truncation keys. The adapter fails loudly on an answer it does not recognise.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from urllib.parse import quote

from efp.capture import CaptureError
from efp.court.adapters.docket import DocketHit, register
from efp.court.adapters.docket.base import DocketPortal
from efp.court.located import LOOKBACK_YEARS
from efp.court.names import parse_name

SEARCH = "https://wcca.wicourts.gov/jsonPost/advancedCaseSearch"
DETAIL = "https://wcca.wicourts.gov/caseDetail.html"


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def result_truncated(payload: Any) -> bool:
    """Does the answer say there is more than it returned? (ASSUMED keys: ``more`` / ``hasMore``,
    or a ``totalCount`` above the cases returned. No pagination is implemented: a result set that
    says it is incomplete is never treated as complete.)"""
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        return False
    cases = result.get("cases") or []
    total = result.get("totalCount")
    return bool(
        result.get("more")
        or result.get("hasMore")
        or (isinstance(total, int) and total > len(cases))
    )


def parse_cases(payload: Any) -> list[DocketHit]:
    """The hits in a WCCA search answer; an answer of another shape raises (never guesses)."""
    if not isinstance(payload, dict) or "result" not in payload:
        raise CaptureError("WCCA answered with an unrecognised shape (no 'result')")
    result = payload["result"]
    cases = result.get("cases") if isinstance(result, dict) else None
    if not isinstance(cases, list):
        raise CaptureError("WCCA answered with an unrecognised shape (no 'result.cases' list)")
    out: list[DocketHit] = []
    for item in cases:
        if not isinstance(item, dict) or not item.get("caseNo"):
            continue
        number = str(item["caseNo"])
        county = str(item.get("countyName") or "")
        out.append(
            DocketHit(
                court=f"{county} County Circuit Court".strip()
                if county
                else "Wisconsin Circuit Court",
                case_number=number,
                caption=str(item.get("caption") or ""),
                filed_date=_day(item.get("filingDate")),
                # portal-supplied values go into the query string quoted: a "&" or "#" in a case
                # number must not change the URL (which is also what the browser capture loads)
                url=f"{DETAIL}?caseNo={quote(number, safe='')}"
                f"&countyNo={quote(str(item.get('countyNo', '')), safe='')}",
            )
        )
    return out


@register
class Wisconsin(DocketPortal):
    state = "WI"
    portal = "https://wcca.wicourts.gov/"
    platform = "wcca"

    def search(self, name: str, year: int, window: int = 2) -> list[DocketHit]:
        parts = parse_name(name)
        if not parts.usable or not parts.given:
            return []  # never an empty exact match (its meaning to WCCA is unknown); the module
            # sends a surname-only row down the assisted path instead of calling this
        payload = {
            "lastName": parts.surname,
            "firstName": parts.given,
            "filingDate": {
                "start": f"{year - LOOKBACK_YEARS}-01-01",
                "end": f"{year + window}-12-31",
            },
            "includeMissingDob": True,
            "includeMissingMiddleName": True,
        }
        answer = self.post_json(SEARCH, payload)
        self.search_truncated = result_truncated(answer)
        return parse_cases(answer)

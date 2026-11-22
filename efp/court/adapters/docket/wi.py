"""Wisconsin: Circuit Court Access (WCCA), https://wcca.wicourts.gov/.

The prompt's expected automated candidate. **Written without a live search** (the build's probe
loads one page per portal and never searches), so the request and answer shapes below are ASSUMED
from how WCCA's public JSON search is commonly described and are listed in ``docs/sources.md``;
the adapter fails loudly on an answer it does not recognise and the first live smoke is the check.
It runs only for a state in ``court.docket_terms_cleared``: the operator reads WCCA's terms first.
"""

from __future__ import annotations

from datetime import date
from typing import Any

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
                url=f"{DETAIL}?caseNo={number}&countyNo={item.get('countyNo', '')}",
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
        if not parts.usable:
            return []
        payload = {
            "lastName": {"equalTo": parts.surname},
            "firstName": {"equalTo": parts.given},
            "filingDate": {
                "start": f"{year - LOOKBACK_YEARS}-01-01",
                "end": f"{year + window}-12-31",
            },
            "includeMissingDob": True,
            "includeMissingMiddleName": True,
        }
        return parse_cases(self.post_json(SEARCH, payload))

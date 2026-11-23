"""The assisted query: what the operator runs in a browser, built the same way every time.

For a state whose portal is ``assisted`` the pipeline does not search; it constructs the search
(portal address, the name in the form portals take it, the filing-date window, and what to do with
the result) and attaches it to the row (``summary_json.assisted``), so the operator's manual
session is pre-assembled. It is deterministic: the same row gives the same query, byte for byte.
"""

from __future__ import annotations

from typing import Any

from efp.court.located import LOOKBACK_YEARS
from efp.court.names import NameParts

IMPORT_COMMAND = "efp import --row {row_id} --source portal <file>"


def build_query(
    *, portal_url: str, platform: str, parts: NameParts, year: int, after: int, row_id: int
) -> dict[str, Any]:
    last, first = parts.surname, " ".join(p for p in (parts.given, parts.middle) if p)
    return {
        "portal_url": portal_url,
        "platform": platform,
        "name": f"{last}, {first}".strip(", "),
        "last_name": last,
        "first_name": parts.given,
        "filed_from": f"{year - LOOKBACK_YEARS}-01-01",
        "filed_to": f"{year + after}-12-31",
        "instructions": (
            "Open the portal, search the name as given for the filing window, save each matching "
            "case's page or PDF, then import it."
        ),
        "after": IMPORT_COMMAND.format(row_id=row_id),
    }

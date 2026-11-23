"""Congress.gov API v3 (api.congress.gov/v3), key ``CONGRESS_API_KEY``, 5,000 requests an hour."""

from __future__ import annotations

from typing import Any

from efp.reference.api import ApiUnavailable, ConfirmedMatch, get_json, secret
from efp.types import Context

BASE = "https://api.congress.gov/v3"


def enrich(ctx: Context, match: ConfirmedMatch, *, row_id: int | None = None) -> dict[str, Any]:
    """Fields Congress.gov adds for a confirmed bioguide ID; ``{}`` when the match has none."""
    if not match.bioguide_id:
        return {}
    data = get_json(
        ctx,
        f"{BASE}/member/{match.bioguide_id}",
        params={"api_key": secret(ctx, "CONGRESS_API_KEY"), "format": "json"},
        row_id=row_id,
    )
    member = data.get("member")
    if not isinstance(member, dict):
        raise ApiUnavailable(f"Congress.gov has no member {match.bioguide_id}")
    terms = member.get("terms")
    if isinstance(terms, dict):  # the API nests the list one level down in some answers
        terms = terms.get("item")
    return {
        "source": "congress.gov",
        "bioguide_id": member.get("bioguideId"),
        "name": member.get("directOrderName") or member.get("invertedOrderName"),
        "birth_year": member.get("birthYear"),
        "party_history": member.get("partyHistory") or [],
        "terms": terms or [],
        "current_member": member.get("currentMember"),
    }

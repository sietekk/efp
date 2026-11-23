"""OpenStates API v3 (v3.openstates.org), key ``OPENSTATES_KEY``; its limits are undocumented and
restrictive, so the budget is one request per two seconds."""

from __future__ import annotations

from typing import Any

from efp.reference.api import ApiUnavailable, ConfirmedMatch, get_json, secret
from efp.types import Context

BASE = "https://v3.openstates.org"


def enrich(ctx: Context, match: ConfirmedMatch, *, row_id: int | None = None) -> dict[str, Any]:
    """Fields OpenStates adds for a confirmed person ID; ``{}`` when the match has none."""
    if not match.openstates_id:
        return {}
    data = get_json(
        ctx,
        f"{BASE}/people",
        params={"id": match.openstates_id, "include": "other_names"},
        headers={"X-API-KEY": secret(ctx, "OPENSTATES_KEY")},
        row_id=row_id,
    )
    results = data.get("results") or []
    if not results:
        raise ApiUnavailable(f"OpenStates has no person {match.openstates_id}")
    p = results[0]
    role = p.get("current_role") or {}
    return {
        "source": "openstates",
        "id": p.get("id"),
        "name": p.get("name"),
        "party": p.get("party"),
        "title": role.get("title"),
        "district": role.get("district"),
        "chamber": role.get("org_classification"),
        "jurisdiction": (p.get("jurisdiction") or {}).get("name"),
        "other_names": [n.get("name") for n in p.get("other_names") or []],
    }

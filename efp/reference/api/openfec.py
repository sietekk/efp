"""OpenFEC (api.open.fec.gov/v1), key ``FEC_API_KEY``, 1,000 requests an hour (budget 800)."""

from __future__ import annotations

from typing import Any

from efp.reference.api import ApiUnavailable, ConfirmedMatch, get_json, secret
from efp.types import Context

BASE = "https://api.open.fec.gov/v1"


def enrich(ctx: Context, match: ConfirmedMatch, *, row_id: int | None = None) -> dict[str, Any]:
    """Fields OpenFEC adds for a confirmed FEC candidate ID; ``{}`` when the match has none."""
    if not match.fec_candidate_id:
        return {}
    data = get_json(
        ctx,
        f"{BASE}/candidate/{match.fec_candidate_id}/",
        params={"api_key": secret(ctx, "FEC_API_KEY")},
        row_id=row_id,
    )
    results = data.get("results") or []
    if not results:
        raise ApiUnavailable(f"OpenFEC has no candidate {match.fec_candidate_id}")
    r = results[0]
    return {
        "source": "openfec",
        "candidate_id": r.get("candidate_id"),
        "name": r.get("name"),
        "party": r.get("party_full") or r.get("party"),
        "office": r.get("office_full") or r.get("office"),
        "state": r.get("state"),
        "district": r.get("district"),
        "election_years": r.get("election_years") or [],
        "incumbent_challenge": r.get("incumbent_challenge_full"),
        "first_file_date": r.get("first_file_date"),
        "load_date": r.get("load_date"),
    }

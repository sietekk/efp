"""Enrichment-only API clients: OpenFEC, Congress.gov v3, OpenStates v3.

Bulk comes first: the candidate universe is matched locally (``ref_candidates_all``), so these are
called only for a match that is already confirmed (build 12, and build 19's public-figure columns),
to add fields the bulk files lack. They are never used for discovery: ``enrich`` takes a
:class:`ConfirmedMatch` that carries identifiers, not a name, so there is nothing to search with.
Every call goes through ``Capture.fetch`` (``kind="api"``), is budgeted per host (OpenFEC 0.22/s
= 800 an hour, Congress.gov 1.1/s, OpenStates 0.5/s) and recorded as a ``stage = 'reference'``
capture.
The FollowTheMoney API is not used after its mirror.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from efp.capture import CaptureError
from efp.secrets import SecretsError, load_secrets
from efp.types import Context


class ApiUnavailable(Exception):
    """No key, or the service refused: the caller carries on without the enrichment."""


@dataclass(frozen=True)
class ConfirmedMatch:
    """An identity already confirmed by the matching stage: identifiers only, never a name."""

    fec_candidate_id: str | None = None
    bioguide_id: str | None = None
    openstates_id: str | None = None


def secret(ctx: Context, name: str) -> str:
    try:
        value = load_secrets(ctx.config.paths.env_file).get(name)
    except SecretsError as exc:
        raise ApiUnavailable(f"cannot read the env file: {exc}") from exc
    if not value:
        raise ApiUnavailable(f"{name} is not set")
    return value


def get_json(
    ctx: Context,
    url: str,
    *,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    row_id: int | None = None,
) -> dict[str, Any]:
    """One budgeted GET through the capture layer, parsed. A failure is :class:`ApiUnavailable`."""
    capture = ctx.capture
    if capture is None:
        raise ApiUnavailable("no capture client")
    try:
        record = capture.fetch(
            url, row_id=row_id, stage="reference", kind="api", params=params, headers=headers
        )
    except CaptureError as exc:
        raise ApiUnavailable(str(exc)) from exc
    if not record.ok:
        raise ApiUnavailable(f"HTTP {record.http_status}")
    try:
        data = json.loads(capture.read_body(record).decode("utf-8"))
    except ValueError as exc:
        raise ApiUnavailable("the answer is not JSON") from exc
    if not isinstance(data, dict):
        raise ApiUnavailable("the answer is not a JSON object")
    return data

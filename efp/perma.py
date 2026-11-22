"""Perma.cc: a permanent copy of a URL the paper cites.

Called only by export (build 19), for the URLs actually cited, never for every capture. Needs
``PERMA_API_KEY``; the free tier requires a registrar affiliation, which is a gated item (noted in
the report, never worked around). Without a key this is a documented no-op that returns ``None``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from efp.capture import Capture
from efp.secrets import Secrets

PERMA_ARCHIVES = "https://api.perma.cc/v1/archives/"
PERMA_PUBLIC = "https://perma.cc/{guid}"


@dataclass(frozen=True)
class PermaResult:
    url: str
    perma_url: str | None
    reason: str | None  # why there is no Perma link, when there is none


def available(secrets: Secrets | None) -> tuple[bool, str]:
    key = secrets.get("PERMA_API_KEY") if secrets is not None else None
    if not key:
        return False, "PERMA_API_KEY not set (free tier needs a registrar affiliation: gated)"
    return True, ""


def archive(capture: Capture, url: str, secrets: Secrets | None) -> PermaResult:
    ok, reason = available(secrets)
    if not ok or secrets is None:
        return PermaResult(url, None, reason)
    key = secrets.get("PERMA_API_KEY") or ""
    record = capture.fetch(
        PERMA_ARCHIVES,
        row_id=None,
        stage="archive",
        kind="api",
        method="POST",
        retry=False,
        headers={"Authorization": f"ApiKey {key}", "Content-Type": "application/json"},
        data=json.dumps({"url": url}).encode(),
    )
    if not record.ok:
        return PermaResult(url, None, f"Perma returned HTTP {record.http_status}")
    guid = json.loads(capture.read_body(record)).get("guid")
    return PermaResult(
        url, PERMA_PUBLIC.format(guid=guid) if guid else None, None if guid else "no guid"
    )

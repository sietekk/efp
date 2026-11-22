"""Wayback Machine and Common Crawl: CDX search, availability, and fetching archived bodies.

Every call goes through :meth:`efp.capture.Capture.fetch` (``kind="api"`` for the index and
availability endpoints), so each is budgeted, cached, written to a WARC and recorded in
``captures``. This module only builds the requests and reads the answers; it does not import an
HTTP library. The ``wayback`` and ``cdx-toolkit`` packages are not used here: they make their own
HTTP calls, which would bypass the budget, the cache, the WARC and the secret stripping.

The Wayback index is rate-limited at 80% of its published ~0.4 requests per second
(``rate_limits."web.archive.org"``); Common Crawl is polite by default.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from efp.capture import Capture, CaptureError, CaptureRecord

WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"
WAYBACK_AVAILABLE = "https://archive.org/wayback/available"
WAYBACK_REPLAY = "https://web.archive.org/web/{timestamp}id_/{original}"
COMMONCRAWL_INDEXES = "https://index.commoncrawl.org/collinfo.json"
COMMONCRAWL_DATA = "https://data.commoncrawl.org/"

CDX_FIELDS = ("urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length")
MATCH_TYPES = ("exact", "prefix", "host", "domain")


@dataclass(frozen=True)
class CdxRow:
    """One archived capture as an index lists it."""

    source: str  # "wayback" or "commoncrawl"
    urlkey: str
    timestamp: str  # YYYYMMDDhhmmss
    original: str
    mimetype: str
    statuscode: str
    digest: str
    length: str
    # Common Crawl only: where the record sits in its WARC file.
    filename: str | None = None
    offset: int | None = None
    record_length: int | None = None

    @property
    def replay_url(self) -> str:
        return WAYBACK_REPLAY.format(timestamp=self.timestamp, original=self.original)


@dataclass(frozen=True)
class Availability:
    available: bool
    url: str | None
    timestamp: str | None
    status: str | None


def _json(record: CaptureRecord, capture: Capture) -> Any:
    if not record.ok:
        raise CaptureError(f"index returned HTTP {record.http_status}")
    return json.loads(capture.read_body(record) or b"null")


def cdx(
    capture: Capture,
    url: str,
    *,
    row_id: int | None = None,
    stage: str = "resolve_links",
    from_year: int | None = None,
    to_year: int | None = None,
    match: str = "exact",
    limit: int = 20,
    source: str = "wayback",
) -> list[CdxRow]:
    """List archived captures of ``url`` (newest first), from the Wayback CDX or Common Crawl."""
    if match not in MATCH_TYPES:
        raise ValueError(f"match must be one of {MATCH_TYPES}")
    if source == "commoncrawl":
        return commoncrawl_cdx(
            capture,
            url,
            row_id=row_id,
            stage=stage,
            from_year=from_year,
            to_year=to_year,
            match=match,
            limit=limit,
        )
    if source != "wayback":
        raise ValueError("source must be 'wayback' or 'commoncrawl'")
    params = {
        "url": url,
        "output": "json",
        "matchType": match,
        "limit": str(-limit),  # a negative limit returns the newest captures
        "filter": "statuscode:200",
        "fl": ",".join(CDX_FIELDS),
    }
    if from_year:
        params["from"] = str(from_year)
    if to_year:
        params["to"] = str(to_year)
    record = capture.fetch(WAYBACK_CDX, row_id=row_id, stage=stage, kind="api", params=params)
    rows = _json(record, capture) or []
    # The first row of a JSON CDX answer is the header.
    return [CdxRow("wayback", *r[: len(CDX_FIELDS)]) for r in rows[1:] if len(r) >= len(CDX_FIELDS)]


def availability(
    capture: Capture,
    url: str,
    *,
    timestamp: str | None = None,
    row_id: int | None = None,
    stage: str = "resolve_links",
) -> Availability:
    """The Wayback Availability API: the closest snapshot to ``timestamp`` (or the latest)."""
    params = {"url": url}
    if timestamp:
        params["timestamp"] = timestamp
    record = capture.fetch(WAYBACK_AVAILABLE, row_id=row_id, stage=stage, kind="api", params=params)
    closest = ((_json(record, capture) or {}).get("archived_snapshots") or {}).get("closest")
    if not closest:
        return Availability(False, None, None, None)
    return Availability(
        bool(closest.get("available")),
        closest.get("url"),
        closest.get("timestamp"),
        closest.get("status"),
    )


def fetch_archived(
    capture: Capture, row: CdxRow, *, row_id: int | None, stage: str
) -> CaptureRecord:
    """Fetch an archived body through the same path, recording the original URL as requested.

    ``id_`` in the replay URL asks the Wayback Machine for the original bytes, without its toolbar
    or rewritten links. The capture's ``tool`` is ``wayback`` (or ``commoncrawl``).
    """
    if row.source == "commoncrawl":
        return _fetch_commoncrawl(capture, row, row_id=row_id, stage=stage)
    return capture.fetch(
        row.replay_url,
        row_id=row_id,
        stage=stage,
        kind="api",
        record_url=row.original,
        tool="wayback",
    )


# --- Common Crawl ----------------------------------------------------------------------------


def commoncrawl_cdx(
    capture: Capture,
    url: str,
    *,
    row_id: int | None,
    stage: str,
    from_year: int | None,
    to_year: int | None,
    match: str,
    limit: int,
) -> list[CdxRow]:
    """Search the most recent Common Crawl indexes (a sample of the web, not a mirror)."""
    listing = capture.fetch(COMMONCRAWL_INDEXES, row_id=row_id, stage=stage, kind="api")
    indexes = [i for i in (_json(listing, capture) or []) if i.get("cdx-api")]
    out: list[CdxRow] = []
    for index in indexes[:3]:  # the three newest crawls: bounded politeness
        params = {"url": url, "output": "json", "matchType": match, "limit": str(limit)}
        if from_year:
            params["from"] = str(from_year)
        if to_year:
            params["to"] = str(to_year)
        record = capture.fetch(
            index["cdx-api"], row_id=row_id, stage=stage, kind="api", params=params
        )
        if record.http_status == 404:  # "no captures" is a 404 from this API
            continue
        for line in capture.read_body(record).decode("utf-8", "replace").splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            if str(d.get("status")) != "200":
                continue
            out.append(
                CdxRow(
                    "commoncrawl",
                    d.get("urlkey", ""),
                    d.get("timestamp", ""),
                    d.get("url", url),
                    d.get("mime", ""),
                    str(d.get("status", "")),
                    d.get("digest", ""),
                    str(d.get("length", "")),
                    d.get("filename"),
                    int(d["offset"]),
                    int(d["length"]),
                )
            )
        if len(out) >= limit:
            break
    return out[:limit]


def _fetch_commoncrawl(
    capture: Capture, row: CdxRow, *, row_id: int | None, stage: str
) -> CaptureRecord:
    if row.filename is None or row.offset is None or row.record_length is None:
        raise CaptureError("a Common Crawl row needs filename, offset and length")
    last = row.offset + row.record_length - 1
    return capture.fetch(
        COMMONCRAWL_DATA + quote(row.filename),
        row_id=row_id,
        stage=stage,
        kind="api",
        headers={"Range": f"bytes={row.offset}-{last}"},
        record_url=row.original,
        tool="commoncrawl",
    )

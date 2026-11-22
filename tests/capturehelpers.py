"""Shared pieces for the capture tests: a Capture with fast budgets, and WARC reading."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from warcio.archiveiterator import ArchiveIterator

from efp.capture import Capture
from efp.config import Config
from efp.ratelimit import DomainLimiter


def make_capture(
    config: Config,
    conn: sqlite3.Connection,
    *,
    hosts: tuple[str, ...] = ("example.test", "other.test", "web.archive.org", "archive.org"),
    rps: float = 1000.0,
    sleeps: list[float] | None = None,
) -> Capture:
    limiter = DomainLimiter(config)
    for host in hosts:
        limiter.define(host, rps)
    recorded = sleeps if sleeps is not None else []
    return Capture(config, conn, limiter=limiter, sleep=recorded.append)


def captures(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM captures ORDER BY capture_id").fetchall()
    finally:
        conn.row_factory = None


def warc_records(config: Config) -> list[dict[str, Any]]:
    """Every record in every WARC file under paths.warc, as plain dicts."""
    out: list[dict[str, Any]] = []
    for path in sorted(Path(config.paths.warc).rglob("*.warc.gz")):
        with path.open("rb") as handle:
            for record in ArchiveIterator(handle):
                payload = record.content_stream().read()
                out.append(
                    {
                        "type": record.rec_type,
                        "uri": record.rec_headers.get_header("WARC-Target-URI"),
                        "version": record.rec_headers.protocol,
                        "payload": payload,
                        "payload_sha256": hashlib.sha256(payload).hexdigest(),
                        "efp_sha256": record.rec_headers.get_header("EFP-Content-SHA256"),
                        "status": record.http_headers.get_statuscode()
                        if record.http_headers
                        else None,
                        "headers": dict(record.http_headers.headers) if record.http_headers else {},
                        "file": path,
                    }
                )
    return out


def headers_of(row: sqlite3.Row) -> dict[str, Any]:
    return json.loads(row["headers_json"])

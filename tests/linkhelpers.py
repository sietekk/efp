"""Shared pieces for the link-resolution tests: synthetic rows, pages and a fast capture client."""

from __future__ import annotations

import sqlite3
from io import BytesIO

from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter

from efp.capture import Capture
from efp.config import Config
from efp.modules import Registry
from efp.resolve_links.modules import register_modules
from efp.runner import load_row, make_context
from efp.types import Context, Row
from tests.capturehelpers import make_capture
from tests.dbhelpers import STAMP, ZERO

HOSTS = (
    "herit.ag",
    "bit.ly",
    "news.test",
    "justice.test",
    "web.archive.org",
    "archive.org",
    "index.commoncrawl.org",
    "data.commoncrawl.org",
    "other.test",
)
SENTENCE = "The county clerk confirmed the filing and the court record shows the plea entered. "
# A page with enough article text to count as a real page (more than 512 characters).
LONG_HTML = (
    "<html><head><title>Story</title></head><body><article><h1>Story</h1><p>"
    + SENTENCE * 12
    + "</p><p>"
    + SENTENCE * 6
    + "</p></article></body></html>"
)
STUB_HTML = "<html><body><p>Not found.</p></body></html>"


def add_row(
    conn: sqlite3.Connection, row_id: int, *, year: int = 2012, source: str | None, sha: str = ZERO
) -> None:
    conn.execute(
        "INSERT INTO rows (row_id, year, state, name, source_raw, in_scope, row_sha256, ingested_at) "
        "VALUES (?, ?, 'CT', 'Synthetic Person', ?, 1, ?, ?)",
        (row_id, year, source, sha, STAMP),
    )
    conn.execute(
        "INSERT INTO case_stage (row_id, stage, status) VALUES (?, 'resolve_links', 'pending')",
        (row_id,),
    )
    conn.commit()


def make_ctx(config: Config, conn: sqlite3.Connection) -> tuple[Context, Capture, Registry]:
    cap = make_capture(config, conn, hosts=HOSTS)
    ctx = make_context(config, conn).model_copy(update={"capture": cap})
    registry = Registry()
    register_modules(registry)
    return ctx, cap, registry


def row_of(conn: sqlite3.Connection, row_id: int) -> Row:
    return load_row(conn, row_id)


def links_of(conn: sqlite3.Connection, row_id: int) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM links WHERE row_id = ? ORDER BY link_id", (row_id,)
        ).fetchall()
    finally:
        conn.row_factory = None


def common_crawl_member(url: str, html: str, status: int = 200) -> bytes:
    """One gzipped WARC response record, as a Common Crawl range request returns it."""
    out = BytesIO()
    writer = WARCWriter(out, gzip=True)
    http = StatusAndHeaders(
        f"{status} OK", [("Content-Type", "text/html; charset=utf-8")], protocol="HTTP/1.1"
    )
    record = writer.create_warc_record(
        url, "response", payload=BytesIO(html.encode()), http_headers=http
    )
    writer.write_record(record)
    return out.getvalue()

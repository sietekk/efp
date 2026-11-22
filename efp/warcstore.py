"""WARC 1.1 writing: one gzip file per UTC day, request and response records as a pair.

Records are reconstructed from the prepared request and the response object, not copied from the
wire: ``warcio.capture_http`` records the wire request, which would put an ``Authorization``
header or an API key into the capture store. Callers pass already-redacted headers and URLs. The
response payload is the decoded body, and ``WARC-Payload-Digest`` is the SHA-256 that
``captures.content_sha256`` records, so a stored body can be matched to its WARC record.

Several processes append to the same day's file under an advisory lock; each record is its own
gzip member, so appends never disturb earlier records. The returned ``warc_path`` is relative to
``paths.warc`` and carries the record's byte offset: ``2026/11/2026-11-22.warc.gz#12345``.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

from warcio.statusandheaders import StatusAndHeaders
from warcio.utils import Digester
from warcio.warcwriter import WARCWriter

Headers = Sequence[tuple[str, str]]

WARC_VERSION = "1.1"
# Body-describing headers that no longer describe the decoded payload we store.
_DROP_RESPONSE_HEADERS = {"content-encoding", "transfer-encoding", "content-length"}


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    lock_path = path.with_name(path.name + ".lock")
    with open(lock_path, "a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class _Sha256Writer(WARCWriter):  # type: ignore[misc]
    """warcio hardcodes SHA-1 for its digests; this project's content identity is SHA-256."""

    @staticmethod
    def _create_digester() -> Digester:
        return Digester("sha256")


def warc_date(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class WarcStore:
    def __init__(self, root: Path, *, software: str) -> None:
        self.root = root
        self.software = software

    def path_for(self, when: datetime) -> Path:
        day = when.astimezone(UTC)
        return self.root / f"{day:%Y}" / f"{day:%m}" / f"{day:%Y-%m-%d}.warc.gz"

    def resolve(self, warc_path: str) -> tuple[Path, int | None]:
        """Map a recorded ``<relative path>#<offset>`` back to a file and offset."""
        name, _, offset = warc_path.partition("#")
        return self.root / name, int(offset) if offset else None

    def write_pair(
        self,
        *,
        when: datetime,
        url: str,
        method: str,
        request_headers: Headers,
        request_body: bytes | None,
        status: int,
        reason: str,
        response_headers: Headers,
        body: bytes,
        protocol: str = "HTTP/1.1",
    ) -> str:
        """Append the request and response records; return ``warc_path#offset`` of the response."""
        path = self.path_for(when)
        path.parent.mkdir(parents=True, exist_ok=True)
        date = warc_date(when)
        hex_digest = hashlib.sha256(body).hexdigest()
        target = urlparse_path(url, method)
        with _locked(path), open(path, "ab") as handle:
            writer = _Sha256Writer(handle, gzip=True, warc_version=WARC_VERSION)
            req_http = StatusAndHeaders(
                f"{method.upper()} {target} HTTP/1.1", list(request_headers), is_http_request=True
            )
            request = writer.create_warc_record(
                url,
                "request",
                payload=BytesIO(request_body or b""),
                http_headers=req_http,
                warc_headers_dict={"WARC-Date": date, "WARC-Software": self.software},
            )
            res_headers = [
                (k, v) for k, v in response_headers if k.lower() not in _DROP_RESPONSE_HEADERS
            ]
            res_headers.append(("Content-Length", str(len(body))))
            res_http = StatusAndHeaders(
                f"{status} {reason}".strip(), res_headers, protocol=protocol
            )
            response = writer.create_warc_record(
                url,
                "response",
                payload=BytesIO(body),
                http_headers=res_http,
                warc_headers_dict={
                    "WARC-Date": date,
                    "WARC-Software": self.software,
                    # The standard WARC-Payload-Digest (sha256, base32) is added by the writer;
                    # this header carries the same digest in the hex form captures stores.
                    "EFP-Content-SHA256": hex_digest,
                },
            )
            response.rec_headers.add_header(
                "WARC-Concurrent-To", request.rec_headers.get_header("WARC-Record-ID")
            )
            writer.write_record(request)
            handle.flush()
            offset = handle.tell()
            writer.write_record(response)
            handle.flush()
            os.fsync(handle.fileno())
            for record in (request, response):  # warcio buffers each payload in a temp file
                record.raw_stream.close()
        return f"{path.relative_to(self.root).as_posix()}#{offset}"


def urlparse_path(url: str, method: str) -> str:
    """The request target for the request line: path and query, as an origin-form request."""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    target = parts.path or "/"
    return f"{target}?{parts.query}" if parts.query else target

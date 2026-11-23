"""Getting a reference file onto disk and recording how: download through the capture layer,
check what arrived, import by hand when a site refuses.

Downloads go through :class:`efp.capture.Capture` (rate-limited, logged, a ``stage = 'reference'``
capture with ``row_id`` NULL whose manifest holds the file's SHA-256 and time). A file is checked by
its leading bytes before anything reads it: a web page where a ZIP should be is a refusal, never
data. After a refusal, and only for hosts named in ``reference.download_user_agent_hosts``, the
download is tried once more with the configured ``User-Agent`` (``reference.download_user_agent``,
empty by default, the operator's decision; see docs/sources.md). Nothing else about the request
changes, and no challenge is solved. A file that cannot be had is ``gated``: it is recorded with
the reason, and ``efp reference import`` takes a file the operator downloaded by hand.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from efp.capture import CaptureError, CaptureRedirect
from efp.config import Config
from efp.types import Context

SNIFF_KINDS = ("zip", "sqlite", "json", "yaml", "csv", "text")


@dataclass(frozen=True)
class Part:
    """One file of a dataset (a dataset may have several: SSA national and state, FEC per cycle)."""

    key: str  # '' for a single file
    url: str
    filename: str
    kind: str  # one of SNIFF_KINDS


@dataclass
class Fetched:
    part: Part
    path: Path | None
    status: str  # ok | gated | failed | skipped
    reason: str = ""
    capture_id: int | None = None
    sha256: str | None = None
    size: int | None = None
    ua_override: bool = False
    from_cache: bool = False


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def dataset_dir(config: Config, dataset: str) -> Path:
    return config.paths.bulk / dataset


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sniff(path: Path, kind: str) -> str | None:
    """None when the file looks like ``kind``; otherwise why not (a web page, the wrong format)."""
    with path.open("rb") as handle:
        head = handle.read(4096)
    lowered = head.lstrip().lower()
    if lowered.startswith((b"<!doctype html", b"<html")) or b"<html" in lowered[:600]:
        return "the host returned a web page, not the file"
    if kind == "zip" and not head.startswith(b"PK\x03\x04"):
        return "not a ZIP file"
    if kind == "sqlite" and not head.startswith(b"SQLite format 3\x00"):
        return "not a SQLite file"
    if kind == "json" and lowered[:1] not in (b"{", b"["):
        return "not JSON"
    if kind in ("yaml", "csv", "text") and b"\x00" in head:
        return "not a text file"
    if not head:
        return "empty file"
    return None


def earlier_capture(conn: sqlite3.Connection, dest: Path, sha256: str) -> int | None:
    """The capture that recorded this exact file (a download or a manual import), if any."""
    for capture_id, body in conn.execute(
        "SELECT capture_id, headers_json FROM captures WHERE stage = 'reference' "
        "AND tool IN ('bulk_download', 'manual_import') ORDER BY capture_id DESC"
    ):
        try:
            manifest = json.loads(body or "{}")
        except ValueError:
            continue
        if manifest.get("sha256") == sha256 and Path(str(manifest.get("path", ""))).name in (
            dest.name,
            dest.name + ".new",
        ):
            return int(capture_id)
    return None


def _ua_allowed_for(config: Config, url: str) -> bool:
    """The browser-style User-Agent may go only to a host named in the config, and is tested for
    each address a request is actually sent to (a redirect can lead elsewhere)."""
    ref = config.reference
    host = (urlsplit(url).hostname or "").lower()
    return bool(ref.download_user_agent) and host in {
        h.lower() for h in ref.download_user_agent_hosts
    }


_REFUSAL_STATUS = re.compile(r"HTTP (401|403|406)\b")


def _is_refusal(reason: str) -> bool:
    """A refusal (401, 403 or 406, or a web page where a file should be): the only things a
    different User-Agent could change. A timeout, a budget wait or a 5xx is not one."""
    return bool(_REFUSAL_STATUS.search(reason)) or "web page" in reason


def fetch(ctx: Context, dataset: str, part: Part, *, refresh: bool = False) -> Fetched:
    """Download one file (or find it already there) and say what came of it.

    The download goes to ``<file>.new`` and replaces the loaded file only after it has passed the
    leading-bytes check, so a refusal (or a web page) never touches a good copy already on disk."""
    config, conn, capture = ctx.config, ctx.db, ctx.capture
    if conn is None or capture is None:
        raise RuntimeError("the reference loaders need the database and the capture client")
    dest = dataset_dir(config, dataset) / part.filename
    if dest.is_file() and not refresh:
        sha = sha256_file(dest)
        known = earlier_capture(conn, dest, sha)
        if known is not None and sniff(dest, part.kind) is None:
            return Fetched(part, dest, "ok", capture_id=known, sha256=sha, size=dest.stat().st_size,
                           from_cache=True)  # fmt: skip
    if not part.url:
        return Fetched(part, None, "gated", "no URL is configured: `efp reference import` a file")
    try:
        resolved = capture.resolve_url(part.url)
    except CaptureError as exc:
        resolved = None
        first_error = str(exc)
    else:
        first_error = ""
    url = part.url
    if resolved is not None and 200 <= resolved.status < 300:
        url = resolved.url
        limit = int(config.reference.max_gb * 1e9)
        if resolved.size is not None and resolved.size > limit:
            return Fetched(
                part, None, "gated",
                f"{resolved.size / 1e9:.1f} GB is over reference.max_gb "
                f"({config.reference.max_gb:g})",
            )  # fmt: skip
    staged = dest.with_name(dest.name + ".new")
    last_reason = first_error
    for override in (False, True):
        if override and not (_is_refusal(last_reason) and _ua_allowed_for(config, part.url)):
            break  # the second attempt is for a refusal from a listed host, nothing else
        target, record, used = url, None, False
        try:
            for _hop in range(4):  # a download does not follow redirects: ask again from each
                headers = None
                if override and _ua_allowed_for(config, target):
                    headers, used = {"User-Agent": config.reference.download_user_agent}, True
                try:
                    record = capture.download(
                        target, staged, stage="reference", row_id=None, extra_headers=headers
                    )
                    break
                except CaptureRedirect as moved:
                    target = moved.location
            if record is None:
                raise CaptureError("too many redirects")
        except CaptureError as exc:
            last_reason = str(exc)
            staged.unlink(missing_ok=True)
            continue
        problem = sniff(staged, part.kind)
        if problem is None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            staged.replace(dest)  # only now does the new file replace the old one
            return Fetched(part, dest, "ok", capture_id=int(record.capture_id),
                           sha256=sha256_file(dest), size=dest.stat().st_size,
                           ua_override=used)  # fmt: skip
        last_reason = problem
        shutil.move(str(staged), str(dest.with_name(dest.name + ".rejected")))  # kept to inspect
    return Fetched(part, None, "gated", f"refused: {last_reason}")


def import_file(ctx: Context, dataset: str, part: Part, source: Path) -> Fetched:
    """Take a file the operator downloaded by hand: copied under ``paths.bulk``, checked, and
    recorded as a ``manual_import`` capture."""
    config, conn, capture = ctx.config, ctx.db, ctx.capture
    if conn is None or capture is None:
        raise RuntimeError("the reference loaders need the database and the capture client")
    if not source.is_file():
        return Fetched(part, None, "failed", f"{source} is not a file")
    problem = sniff(source, part.kind)
    if problem is not None:
        return Fetched(part, None, "failed", f"{source.name}: {problem}")
    dest = dataset_dir(config, dataset) / part.filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() != dest.resolve():
        shutil.copyfile(source, dest)
    sha = sha256_file(dest)
    manifest: dict[str, Any] = {
        "url": f"manual:{dataset}/{part.filename}",
        "path": str(dest),
        "size": dest.stat().st_size,
        "sha256": sha,
        "imported_from": source.name,
    }
    record = capture.store_bytes(
        json.dumps(manifest, sort_keys=True).encode(),
        row_id=None,
        stage="reference",
        url_requested=manifest["url"],
        tool="manual_import",
        tool_version="1",
        content_type="application/json",
        manual_source="reference",
        headers=manifest,
    )
    return Fetched(part, dest, "ok", capture_id=int(record.capture_id), sha256=sha,
                   size=manifest["size"])  # fmt: skip

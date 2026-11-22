"""Save Page Now 2: independent archival of every captured page, in the background.

``Capture.fetch`` queues each successful page in ``spn_queue``; :func:`drain` submits the queue at
no more than ``capture.spn_per_minute`` and records the resulting Wayback URL in
``captures.wayback_url``. Throttling (HTTP 429, "too many requests") is deferral, never an error:
the row goes back in the queue for later. Without ``IA_ACCESS`` and ``IA_SECRET`` nothing is
submitted and the queue simply accumulates; ``efp selftest`` reports how many are waiting.

The local WARC is the evidentiary copy. Nothing about a fetch depends on this succeeding.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from efp.capture import Capture, CaptureError, CaptureRecord
from efp.logging import redact
from efp.secrets import Secrets

SPN_SAVE = "https://web.archive.org/save"
SPN_STATUS = "https://web.archive.org/save/status/{job_id}"
MAX_ATTEMPTS = 5
JOB_TIMEOUT = timedelta(hours=24)  # a submitted job that never finishes goes back in the queue
# The one permitted update to captures for a Save Page Now result: NULL to a value, once.
WAYBACK_SQL = "UPDATE captures SET wayback_url = ? WHERE capture_id = ? AND wayback_url IS NULL"
BACKOFF_MINUTES = 15


@dataclass
class DrainResult:
    submitted: int = 0
    completed: int = 0
    deferred: int = 0
    failed: int = 0
    pending_polls: int = 0
    blocked_reason: str | None = None
    notes: list[str] = field(default_factory=list)


def credentials(secrets: Secrets | None) -> tuple[str, str] | None:
    if secrets is None:
        return None
    access, secret = secrets.get("IA_ACCESS"), secrets.get("IA_SECRET")
    return (access, secret) if access and secret else None


def queue_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts = {"pending": 0, "submitted": 0, "done": 0, "deferred": 0, "failed": 0}
    for status, n in conn.execute("SELECT status, count(*) FROM spn_queue GROUP BY status"):
        counts[str(status)] = int(n)
    return counts


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(UTC)).astimezone(UTC)


def _stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def drain(
    capture: Capture,
    conn: sqlite3.Connection,
    secrets: Secrets | None,
    *,
    limit: int | None = None,
    now: datetime | None = None,
) -> DrainResult:
    """Poll submitted jobs, then submit due rows, within the Save Page Now budget."""
    result = DrainResult()
    creds = credentials(secrets)
    if creds is None:
        result.blocked_reason = "IA_ACCESS / IA_SECRET not set: the queue accumulates"
        return result
    access, secret = creds
    headers = {"Accept": "application/json", "Authorization": f"LOW {access}:{secret}"}
    capture.limiter.define("spn", capture.config.capture.spn_per_minute / 60)
    moment = _now(now)

    for queue_id, capture_id, job_id, attempts, submitted_at in conn.execute(
        "SELECT queue_id, capture_id, job_id, attempts, next_attempt_at FROM spn_queue "
        "WHERE status = 'submitted' ORDER BY queue_id LIMIT ?",
        (limit or -1,),
    ).fetchall():
        _poll(
            capture,
            conn,
            headers,
            queue_id,
            capture_id,
            job_id,
            attempts,
            submitted_at,
            moment,
            result,
        )

    due = conn.execute(
        "SELECT queue_id, capture_id, url, attempts FROM spn_queue "
        "WHERE status IN ('pending', 'deferred') "
        "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
        "ORDER BY queue_id LIMIT ?",
        (_stamp(moment), limit or -1),
    ).fetchall()
    for queue_id, capture_id, url, attempts in due:
        if not _submit(capture, conn, headers, queue_id, capture_id, url, attempts, moment, result):
            break  # throttled: stop for now, everything left stays queued
    return result


def _defer(
    conn: sqlite3.Connection,
    queue_id: int,
    attempts: int,
    moment: datetime,
    note: str,
    *,
    fail: bool,
) -> None:
    if fail:
        conn.execute(
            "UPDATE spn_queue SET status = 'failed', attempts = ?, last_error = ?, finished_at = ? "
            "WHERE queue_id = ?",
            (attempts, redact(note)[:300], _stamp(moment), queue_id),
        )
    else:
        wait = timedelta(minutes=BACKOFF_MINUTES * max(attempts, 1))
        conn.execute(
            "UPDATE spn_queue SET status = 'deferred', attempts = ?, last_error = ?, "
            "next_attempt_at = ?, job_id = NULL WHERE queue_id = ?",
            (attempts, redact(note)[:300], _stamp(moment + wait), queue_id),
        )
    conn.commit()


def _submit(
    capture: Capture,
    conn: sqlite3.Connection,
    headers: dict[str, str],
    queue_id: int,
    capture_id: int,
    url: str,
    attempts: int,
    moment: datetime,
    result: DrainResult,
) -> bool:
    """Submit one row. Returns False when throttled, so the caller stops."""
    capture.limiter.acquire("spn")
    try:
        record = capture.fetch(
            SPN_SAVE,
            row_id=None,
            stage="archive",
            kind="api",
            method="POST",
            headers=headers,
            data={"url": url, "capture_all": "1"},
            retry=False,
        )
    except CaptureError as exc:
        _defer(conn, queue_id, attempts + 1, moment, str(exc), fail=attempts + 1 >= MAX_ATTEMPTS)
        result.deferred += 1
        return True
    body = _body_json(capture, record)
    if _throttled(record.http_status, body):
        # Throttling is deferral, never an error, whether SPN says it with a 429 or with a 200
        # that carries a too-many-* status: no attempt is counted and the queue stops for now.
        _defer(conn, queue_id, attempts, moment, "throttled by Save Page Now", fail=False)
        result.deferred += 1
        result.notes.append("Save Page Now throttled the queue; the rest stays queued")
        return False
    job_id = body.get("job_id")
    if record.ok and job_id:
        conn.execute(
            "UPDATE spn_queue SET status = 'submitted', attempts = ?, job_id = ?, "
            "next_attempt_at = ?, last_error = NULL WHERE queue_id = ?",
            (attempts + 1, job_id, _stamp(moment), queue_id),  # next_attempt_at: submitted at
        )
        conn.commit()
        result.submitted += 1
        return True
    message = (
        body.get("message") if isinstance(body, dict) else None
    ) or f"HTTP {record.http_status}"
    _defer(conn, queue_id, attempts + 1, moment, str(message), fail=attempts + 1 >= MAX_ATTEMPTS)
    result.failed += int(attempts + 1 >= MAX_ATTEMPTS)
    result.deferred += int(attempts + 1 < MAX_ATTEMPTS)
    return True


def _poll(
    capture: Capture,
    conn: sqlite3.Connection,
    headers: dict[str, str],
    queue_id: int,
    capture_id: int,
    job_id: str,
    attempts: int,
    submitted_at: str | None,
    moment: datetime,
    result: DrainResult,
) -> None:
    try:
        record = capture.fetch(
            SPN_STATUS.format(job_id=job_id),
            row_id=None,
            stage="archive",
            kind="api",
            headers=headers,
            retry=False,
            no_cache=True,
        )
    except CaptureError as exc:
        result.notes.append(f"status poll failed: {exc}")
        return
    body = _body_json(capture, record)
    status = body.get("status")
    if status == "success" and body.get("timestamp"):
        original = body.get("original_url") or ""
        wayback = f"https://web.archive.org/web/{body['timestamp']}/{original}"
        conn.execute(
            "UPDATE spn_queue SET status = 'done', wayback_url = ?, finished_at = ?, "
            "last_error = NULL WHERE queue_id = ?",
            (wayback, _stamp(moment), queue_id),
        )
        # The one permitted update to captures: wayback_url from NULL, once (migration 0006).
        conn.execute(WAYBACK_SQL, (wayback, capture_id))
        conn.commit()
        result.completed += 1
    elif status == "error":
        message = str(body.get("message") or body.get("status_ext") or "SPN job error")
        throttled = _throttled(None, body) or "429" in message
        _defer(conn, queue_id, attempts + int(not throttled), moment, message, fail=not throttled)
        result.deferred += int(throttled)
        result.failed += int(not throttled)
    elif submitted_at and moment - datetime.fromisoformat(submitted_at) > JOB_TIMEOUT:
        _defer(conn, queue_id, attempts, moment, "job did not finish within 24 h", fail=False)
        result.deferred += 1
    else:
        result.pending_polls += 1


def _throttled(http_status: int | None, body: dict[str, Any]) -> bool:
    text = f"{body.get('status_ext', '')} {body.get('message', '')}".lower()
    return http_status == 429 or "too-many" in text or "too many" in text


def _body_json(capture: Capture, record: CaptureRecord) -> dict[str, Any]:
    try:
        body = json.loads(capture.read_body(record))
    except (ValueError, OSError):
        return {}
    return body if isinstance(body, dict) else {}

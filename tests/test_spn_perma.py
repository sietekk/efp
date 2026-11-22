from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import responses
import stamina
from pydantic import SecretStr

from efp import perma, spn
from efp.capture import Capture
from efp.config import Config, load_config
from efp.db import connect
from efp.secrets import Secrets
from tests.capturehelpers import captures, make_capture
from tests.dbhelpers import insert_row

KEYS = Secrets(values={"IA_ACCESS": SecretStr("ACCESS123"), "IA_SECRET": SecretStr("SECRET456")})


@pytest.fixture(autouse=True)
def _fast_retries() -> Iterator[None]:
    stamina.set_testing(
        True, attempts=1, cap=False
    )  # testing mode overrides attempts: 1 = no retry
    yield
    stamina.set_testing(False)


@pytest.fixture
def rsps() -> Iterator[responses.RequestsMock]:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield mock


@pytest.fixture
def setup(
    write_config: Callable[..., Path],
) -> Iterator[tuple[Config, sqlite3.Connection, Capture]]:
    config = load_config(write_config({"spn_per_minute = 5": "spn_per_minute = 60000"}))
    conn = connect(config)
    insert_row(conn, 1)
    conn.commit()
    cap = make_capture(config, conn, hosts=("example.test", "web.archive.org", "api.perma.cc"))
    yield config, conn, cap
    cap.close()
    conn.close()


def _queue(cap: Capture, conn: sqlite3.Connection, *urls: str) -> list[int]:
    ids = []
    for url in urls:
        record = cap.store_bytes(
            url.encode(),
            row_id=1,
            stage="resolve_links",
            url_requested=url,
            tool="t",
            http_status=200,
        )
        cap.enqueue_spn(record.capture_id, url)
        ids.append(record.capture_id)
    return ids


def test_without_keys_the_queue_accumulates_and_nothing_is_sent(
    setup: tuple[Config, sqlite3.Connection, Capture], rsps: responses.RequestsMock
) -> None:
    _, conn, cap = setup
    _queue(cap, conn, "https://example.test/a")
    result = spn.drain(cap, conn, Secrets())
    assert result.blocked_reason and "IA_ACCESS" in result.blocked_reason
    assert len(rsps.calls) == 0
    assert spn.queue_counts(conn)["pending"] == 1


def test_a_page_is_submitted_polled_and_its_wayback_url_recorded(
    setup: tuple[Config, sqlite3.Connection, Capture], rsps: responses.RequestsMock
) -> None:
    _, conn, cap = setup
    (capture_id,) = _queue(cap, conn, "https://example.test/a")
    rsps.post(
        "https://web.archive.org/save", json={"url": "https://example.test/a", "job_id": "spn2-j1"}
    )
    first = spn.drain(cap, conn, KEYS)
    assert (first.submitted, first.completed) == (1, 0)
    sent = rsps.calls[0].request
    assert sent.headers["Authorization"] == "LOW ACCESS123:SECRET456"  # really sent
    rsps.get(
        "https://web.archive.org/save/status/spn2-j1",
        json={
            "status": "success",
            "timestamp": "20261122120000",
            "original_url": "https://example.test/a",
        },
    )
    second = spn.drain(cap, conn, KEYS)
    assert second.completed == 1
    expected = "https://web.archive.org/web/20261122120000/https://example.test/a"
    row = next(r for r in captures(conn) if r["capture_id"] == capture_id)
    assert row["wayback_url"] == expected
    assert conn.execute("SELECT status, wayback_url FROM spn_queue").fetchone() == (
        "done",
        expected,
    )
    # the credential never reached anything stored
    everything = " ".join(str(v) for c in captures(conn) for v in dict(c).values())
    assert "SECRET456" not in everything and "ACCESS123" not in everything


def test_throttling_is_deferral_not_an_error(
    setup: tuple[Config, sqlite3.Connection, Capture], rsps: responses.RequestsMock
) -> None:
    _, conn, cap = setup
    _queue(cap, conn, "https://example.test/a", "https://example.test/b")
    rsps.post("https://web.archive.org/save", status=429, json={"message": "too many"})
    result = spn.drain(cap, conn, KEYS)
    assert result.deferred == 1 and result.failed == 0
    posts = [c for c in rsps.calls if c.request.method == "POST"]
    assert len(posts) == 1  # no hammering, no retry, and the rest of the queue was left alone
    counts = spn.queue_counts(conn)
    assert counts["deferred"] == 1 and counts["pending"] == 1 and counts["failed"] == 0
    due = conn.execute("SELECT status FROM spn_queue WHERE next_attempt_at IS NOT NULL").fetchall()
    assert due == [("deferred",)]  # the deferred row waits for its backoff


def test_a_deferred_row_is_retried_once_it_is_due(
    setup: tuple[Config, sqlite3.Connection, Capture], rsps: responses.RequestsMock
) -> None:
    _, conn, cap = setup
    _queue(cap, conn, "https://example.test/a")
    rsps.post("https://web.archive.org/save", status=429, json={})
    spn.drain(cap, conn, KEYS)
    rsps.replace(responses.POST, "https://web.archive.org/save", json={"job_id": "j2"})
    later = datetime.now(UTC) + timedelta(hours=2)
    assert spn.drain(cap, conn, KEYS, now=later).submitted == 1


def test_repeated_rejections_end_in_failed(
    setup: tuple[Config, sqlite3.Connection, Capture], rsps: responses.RequestsMock
) -> None:
    _, conn, cap = setup
    _queue(cap, conn, "https://example.test/a")
    rsps.post("https://web.archive.org/save", status=400, json={"message": "blocked by robots"})
    moment = datetime.now(UTC)
    for _ in range(spn.MAX_ATTEMPTS):
        moment += timedelta(days=1)
        spn.drain(cap, conn, KEYS, now=moment)
    assert conn.execute("SELECT status, last_error FROM spn_queue").fetchone() == (
        "failed",
        "blocked by robots",
    )


def test_the_same_url_is_queued_once(setup: tuple[Config, sqlite3.Connection, Capture]) -> None:
    _, conn, cap = setup
    _queue(cap, conn, "https://example.test/a")
    record = cap.store_bytes(b"again", row_id=1, stage="x", url_requested="u", tool="t")
    assert cap.enqueue_spn(record.capture_id, "https://example.test/a") is False
    assert conn.execute("SELECT count(*) FROM spn_queue").fetchone()[0] == 1


# --- Perma.cc ---------------------------------------------------------------------------------


def test_perma_is_a_documented_no_op_without_a_key(
    setup: tuple[Config, sqlite3.Connection, Capture], rsps: responses.RequestsMock
) -> None:
    _, _, cap = setup
    result = perma.archive(cap, "https://example.test/cited", Secrets())
    assert result.perma_url is None and "PERMA_API_KEY" in (result.reason or "")
    assert len(rsps.calls) == 0
    ok, why = perma.available(Secrets())
    assert not ok and "registrar" in why


def test_perma_archives_a_cited_url_with_the_key_kept_out_of_storage(
    setup: tuple[Config, sqlite3.Connection, Capture], rsps: responses.RequestsMock
) -> None:
    _, conn, cap = setup
    secrets = Secrets(values={"PERMA_API_KEY": SecretStr("PERMAKEY789")})
    rsps.post("https://api.perma.cc/v1/archives/", status=201, json={"guid": "AB12-CD34"})
    result = perma.archive(cap, "https://example.test/cited", secrets)
    assert result.perma_url == "https://perma.cc/AB12-CD34"
    assert rsps.calls[0].request.headers["Authorization"] == "ApiKey PERMAKEY789"
    assert "PERMAKEY789" not in " ".join(str(v) for c in captures(conn) for v in dict(c).values())


# --- review follow-ups ------------------------------------------------------------------------


def test_a_200_that_says_too_many_is_throttling_not_a_failure(
    setup: tuple[Config, sqlite3.Connection, Capture], rsps: responses.RequestsMock
) -> None:
    _, conn, cap = setup
    _queue(cap, conn, "https://example.test/a", "https://example.test/b")
    rsps.post(
        "https://web.archive.org/save",
        status=200,
        json={"status": "error", "status_ext": "error:too-many-daily-captures", "message": "limit"},
    )
    moment = datetime.now(UTC)
    for _ in range(spn.MAX_ATTEMPTS + 2):  # far more drains than the attempt limit
        moment += timedelta(days=1)
        spn.drain(cap, conn, KEYS, now=moment)
    counts = spn.queue_counts(conn)
    assert counts["failed"] == 0
    assert conn.execute("SELECT max(attempts) FROM spn_queue").fetchone()[0] == 0  # none counted
    assert len([c for c in rsps.calls if c.request.method == "POST"]) == spn.MAX_ATTEMPTS + 2


def test_a_job_that_never_finishes_goes_back_in_the_queue_with_its_attempts_kept(
    setup: tuple[Config, sqlite3.Connection, Capture], rsps: responses.RequestsMock
) -> None:
    _, conn, cap = setup
    _queue(cap, conn, "https://example.test/a")
    rsps.post("https://web.archive.org/save", json={"job_id": "stuck"})
    start = datetime.now(UTC)
    spn.drain(cap, conn, KEYS, now=start)
    rsps.get("https://web.archive.org/save/status/stuck", json={"status": "pending"})
    still = spn.drain(cap, conn, KEYS, now=start + timedelta(hours=1))
    assert still.pending_polls == 1 and spn.queue_counts(conn)["submitted"] == 1
    late = spn.drain(cap, conn, KEYS, now=start + spn.JOB_TIMEOUT + timedelta(minutes=5))
    assert late.deferred == 1
    assert conn.execute("SELECT status, attempts FROM spn_queue").fetchone() == ("deferred", 1)


def test_a_job_error_counts_the_stored_attempts_not_one(
    setup: tuple[Config, sqlite3.Connection, Capture], rsps: responses.RequestsMock
) -> None:
    _, conn, cap = setup
    _queue(cap, conn, "https://example.test/a")
    conn.execute("UPDATE spn_queue SET attempts = 3")
    conn.commit()
    rsps.post("https://web.archive.org/save", json={"job_id": "j9"})
    spn.drain(cap, conn, KEYS)
    rsps.get(
        "https://web.archive.org/save/status/j9", json={"status": "error", "message": "blocked"}
    )
    spn.drain(cap, conn, KEYS)
    assert conn.execute("SELECT status, attempts FROM spn_queue").fetchone() == ("failed", 5)

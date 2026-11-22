"""``state_adapters``: what each state's AG adapter is, whether it works, and why not.

``kind = 'ag'`` rows are written by ``efp court ag verify`` (and by the live selftest, which turns
a failing module into ``broken``) and read by the harvester and the coverage matrix.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from efp.court.adapters.ag import adapters


def today() -> str:
    return datetime.now(UTC).date().isoformat()


def write(
    conn: sqlite3.Connection,
    state: str,
    *,
    mode: str,
    portal: str | None,
    adapter_module: str | None,
    reason: str | None,
    verified: str | None = None,
) -> None:
    """Insert or update one ``kind = 'ag'`` row (update first: the schema guard forbids upserts)."""
    stamp = verified or today()
    updated = conn.execute(
        "UPDATE state_adapters SET portal = ?, adapter_module = ?, mode = ?, reason = ?, "
        "last_verified = ? WHERE state = ? AND kind = 'ag'",
        (portal, adapter_module, mode, reason, stamp, state),
    ).rowcount
    if not updated:
        conn.execute(
            "INSERT INTO state_adapters (state, kind, portal, adapter_module, mode, reason, "
            "last_verified) VALUES (?, 'ag', ?, ?, ?, ?, ?)",
            (state, portal, adapter_module, mode, reason, stamp),
        )
    conn.commit()


def mark(conn: sqlite3.Connection, state: str, mode: str, reason: str) -> None:
    """Change a registered state's mode (a site that blocked us, a smoke test that failed)."""
    cls = adapters().get(state)
    write(
        conn,
        state,
        mode=mode,
        portal=getattr(cls, "portal", None),
        adapter_module=getattr(cls, "__module__", None),
        reason=reason,
    )


def seed(conn: sqlite3.Connection) -> int:
    """Record every registered adapter as declared (its mode and reason); returns how many.

    A state that is already recorded keeps what was recorded (a state turned ``manual`` after
    repeated refusals, or ``broken`` by a smoke test, stays so until a probe says otherwise),
    unless its adapter is declared ``manual``: those facts are the code's, so they are refreshed."""
    for state, cls in adapters().items():
        if cls.mode != "manual" and recorded(conn, state) is not None:
            continue
        write(
            conn,
            state,
            mode=cls.mode,
            portal=cls.portal,
            adapter_module=cls.__module__,
            reason=cls.reason or None,
        )
    return len(adapters())


def recorded(conn: sqlite3.Connection, state: str) -> tuple[str, str | None] | None:
    """(mode, reason) recorded for a state's AG adapter, if any."""
    found = conn.execute(
        "SELECT mode, reason FROM state_adapters WHERE state = ? AND kind = 'ag'", (state,)
    ).fetchone()
    return None if found is None else (found[0], found[1])

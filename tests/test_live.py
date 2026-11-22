"""Live checks: the real network, never run in CI (``-m 'not live'``). Run by ``efp selftest --live``.

Each is a few requests through the capture layer at its normal budget.
"""

from __future__ import annotations

import sqlite3

import pytest

from efp.config import Config
from efp.livecheck import live_checks


@pytest.mark.live
def test_live_wayback_cdx_and_rfc3161(config: Config, conn: sqlite3.Connection) -> None:
    results = {c.name: c for c in live_checks(config, conn)}
    assert results["live: wayback cdx"].ok, results["live: wayback cdx"].detail
    tsa = [c for name, c in results.items() if name.startswith("live: rfc3161")]
    assert tsa and all(c.ok for c in tsa), [c.detail for c in tsa]


@pytest.mark.live
def test_live_state_ag_adapters_parse_their_first_listing_page(
    config: Config, conn: sqlite3.Connection
) -> None:
    """Each automated state AG adapter fetches and parses its real first listing page. A failure
    is recorded as ``broken`` in state_adapters by the check itself."""
    results = [c for c in live_checks(config, conn) if c.name.startswith("live: state ag")]
    assert results and all(c.ok for c in results), [(c.name, c.detail) for c in results]

"""``efp selftest --reference``: every dataset's row count, source hash and date, a few known
values recomputed from the loaded data, and the candidate index's per-source counts.

A dataset that is not loaded is reported (not a failure); a loaded one whose rows, file hash or a
known value is wrong is a failure.
"""

from __future__ import annotations

import sqlite3

from efp import db as database
from efp import runner
from efp.config import Config
from efp.reference import datasets, index
from efp.selftest import Check


def reference_checks(config: Config, conn: sqlite3.Connection | None = None) -> list[Check]:
    own = conn is None
    connection = conn or database.connect(config)
    try:
        ctx = runner.make_context(config, connection)
        checks: list[Check] = []
        for dataset in datasets().values():
            part = dataset.verify(ctx)
            checks.extend(part)
            if all(c.ok for c in part) and not any(c.detail == "not loaded" for c in part):
                checks.extend(dataset.check_known(ctx))
        counts = index.counts(connection)
        total = sum(counts.values())
        detail = ", ".join(f"{k} {v:,}" for k, v in sorted(counts.items())) or "empty"
        checks.append(
            Check(
                "reference.candidates_all",
                total > 0,
                f"{total:,} candidates ({detail})",
                required=False,
            )
        )
        return checks
    finally:
        if own:
            connection.close()

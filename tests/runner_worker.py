"""A worker process for the concurrency and kill tests: runs a stage with a recording component.

Usage: python -m tests.runner_worker CONFIG STAGE OUT_FILE [--die-after N] [--recover] [--go FILE]

Every row the component finishes is appended to OUT_FILE (one id per line), after the batch has
been processed. ``--die-after N`` exits the process hard (no cleanup) on the Nth batch, so that
batch's rows are left ``running``, which is what a kill mid-batch does.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

from efp import db as database
from efp.config import load_config
from efp.runner import RunOptions, make_context, run_stage
from efp.types import Context, Row, StageResult


class Recording:
    def __init__(self, stage: str, out: Path, die_after: int | None) -> None:
        self.stage = stage
        self.out = out
        self.die_after = die_after
        self.batches = 0

    def gate(self, row: Row) -> bool | str:
        return True

    def run(self, batch: list[Row], ctx: Context) -> list[StageResult]:
        self.batches += 1
        time.sleep(0.05)
        if self.die_after is not None and self.batches >= self.die_after:
            os._exit(9)
        with self.out.open("a", encoding="utf-8") as handle:
            handle.writelines(f"{r.row_id}\n" for r in batch)
        return [StageResult(row_id=r.row_id, stage=self.stage, status="done") for r in batch]


def main(argv: list[str]) -> int:
    config = load_config(Path(argv[0]))
    stage, out = argv[1], Path(argv[2])
    die_after = int(argv[argv.index("--die-after") + 1]) if "--die-after" in argv else None
    if "--go" in argv:  # a start barrier, so two workers really do contend for the same rows
        go = Path(argv[argv.index("--go") + 1])
        while not go.exists():
            time.sleep(0.01)
    conn: sqlite3.Connection = database.connect(config, migrate=False, create=False)
    ctx = make_context(config, conn).model_copy(update={"log": logging.getLogger("worker")})
    options = RunOptions(stage=stage, recover="--recover" in argv)
    run_stage(conn, config, options, ctx, component=Recording(stage, out, die_after))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

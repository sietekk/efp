"""Synthetic fixtures for the runner tests: a seeded database and a configurable fake component."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

from efp.config import Config
from efp.runner import RunOptions, RunSummary, make_context, run_stage
from efp.types import Context, Row, StageResult
from tests.dbhelpers import STAMP, insert_capture, insert_row

STAGES = ("resolve_links", "court", "identity", "news", "documents", "social", "extract", "code")

# (row_id, year, state): ordered so that year DESC, row_id gives 1, 2, 3, 4, 5, 6.
ROWS: list[tuple[int, int, str]] = [
    (1, 2020, "CT"),
    (2, 2019, "OH"),
    (3, 2019, "CT"),
    (4, 2010, "OH"),
    (5, 2005, "CT"),
    (6, 2000, "OH"),
]


ALL_STAGES_LINE = 'stages = ["resolve_links", "court"]'
ALL_STAGES = 'stages = ["resolve_links", "court", "identity", "documents", "news", "social", "extract", "code"]'


def seed(conn: sqlite3.Connection, rows: list[tuple[int, int, str]] | None = None) -> list[int]:
    ids: list[int] = []
    for row_id, year, state in rows or ROWS:
        insert_row(conn, row_id, year=year, state=state)
        for stage in STAGES:
            conn.execute(
                "INSERT INTO case_stage (row_id, stage, status) VALUES (?, ?, 'pending')",
                (row_id, stage),
            )
        ids.append(row_id)
    conn.commit()
    return ids


def set_stage(
    conn: sqlite3.Connection, row_id: int, stage: str, status: str, **columns: object
) -> None:
    columns["status"] = status
    assignments = ", ".join(f"{name} = ?" for name in columns)
    conn.execute(
        f"UPDATE case_stage SET {assignments} WHERE row_id = ? AND stage = ?",
        (*columns.values(), row_id, stage),
    )
    conn.commit()


def stage_of(conn: sqlite3.Connection, row_id: int, stage: str) -> sqlite3.Row:
    conn.row_factory = sqlite3.Row
    try:
        found = conn.execute(
            "SELECT * FROM case_stage WHERE row_id = ? AND stage = ?", (row_id, stage)
        ).fetchone()
    finally:
        conn.row_factory = None
    assert found is not None
    return found


def statuses(conn: sqlite3.Connection, stage: str) -> dict[int, str]:
    return {
        int(r[0]): str(r[1])
        for r in conn.execute("SELECT row_id, status FROM case_stage WHERE stage = ?", (stage,))
    }


def decide(
    conn: sqlite3.Connection, row_id: int, outcome: str, decision_id: int | None = None
) -> None:
    conn.execute(
        "INSERT INTO decisions (decision_id, row_id, kind, outcome, decided_by, decided_at) "
        "VALUES (?, ?, 'identity', ?, 'test', ?)",
        (decision_id, row_id, outcome, STAMP),
    )
    conn.commit()


def add_identifier(conn: sqlite3.Connection, row_id: int, kind: str, value: str = "x") -> None:
    capture_id = 1000 + row_id
    insert_capture(conn, capture_id, row_id, content_sha256=f"{capture_id:064d}")
    conn.execute(
        "INSERT INTO identifiers (row_id, kind, value, capture_id) VALUES (?, ?, ?, ?)",
        (row_id, kind, value, capture_id),
    )
    conn.commit()


@dataclass
class FakeComponent:
    """Records every batch; fails the rows it is told to; hashes inputs by a settable value."""

    stage: str = "court"
    fail: set[int] = field(default_factory=set)
    gate_reasons: dict[int, str] = field(default_factory=dict)
    needs_review: set[int] = field(default_factory=set)
    hash_value: str | None = "h1"
    raise_error: bool = False
    skip: dict[int, str] = field(default_factory=dict)  # row -> gate_reason, returned as skipped
    omit: set[int] = field(default_factory=set)  # rows the component forgets to answer
    error_text: str = "boom"
    batches: list[list[int]] = field(default_factory=list)

    @property
    def seen(self) -> list[int]:
        return [r for batch in self.batches for r in batch]

    def gate(self, row: Row) -> bool | str:
        return self.gate_reasons.get(row.row_id, True)

    def input_hash(self, row: Row, ctx: Context) -> str | None:
        return self.hash_value

    def run(self, batch: list[Row], ctx: Context) -> list[StageResult]:
        self.batches.append([r.row_id for r in batch])
        if self.raise_error:
            raise RuntimeError("component exploded")
        out: list[StageResult] = []
        for row in batch:
            if row.row_id in self.omit:
                continue
            if row.row_id in self.skip:
                out.append(
                    StageResult(
                        row_id=row.row_id,
                        stage=self.stage,
                        status="skipped",
                        gate_reason=self.skip[row.row_id],
                    )
                )
                continue
            status = "failed" if row.row_id in self.fail else "done"
            if row.row_id in self.needs_review:
                status = "needs_review"
            out.append(
                StageResult(
                    row_id=row.row_id,
                    stage=self.stage,
                    status=status,  # type: ignore[arg-type]
                    input_hash=self.hash_value,
                    tool_version="fake-1",
                    error=self.error_text if status == "failed" else None,
                    summary_json={"ok": True},
                )
            )
        return out


def run(
    conn: sqlite3.Connection,
    config: Config,
    stage: str,
    component: FakeComponent | None,
    **options: object,
) -> RunSummary:
    ctx = make_context(config, conn)
    ctx = ctx.model_copy(update={"log": logging.getLogger("efp.test")})
    return run_stage(conn, config, RunOptions(stage=stage, **options), ctx, component=component)  # type: ignore[arg-type]

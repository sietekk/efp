"""Ingest: load the Heritage workbook into ``rows`` once, immutably (build 03).

Ingest is a command (``efp ingest``), not a runner stage: it has no predecessor and does not appear
in ``runner.stages``. The component interface is kept so every package under ``efp/`` looks alike:
``gate`` is always open (the row exists once ingested) and ``run`` has no batch work to do.
"""

from efp.ingest.loader import (
    IngestError,
    IngestRefused,
    Prepared,
    Result,
    check_only,
    ingest,
    prepare,
)
from efp.types import Context, Row, StageResult

BUILD = "03"

__all__ = [
    "BUILD",
    "IngestError",
    "IngestRefused",
    "Prepared",
    "Result",
    "check_only",
    "gate",
    "ingest",
    "prepare",
    "run",
]


def run(batch: list[Row], ctx: Context) -> list[StageResult]:
    """Ingest has no per-row stage work; use :func:`prepare` and :func:`ingest`."""
    return []


def gate(row: Row) -> bool | str:
    """True: a row that exists has, by definition, been ingested."""
    return True

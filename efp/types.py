"""Shared types every component uses. Builds 02 and 03 refine ``Row`` against the schema."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from efp.config import Config

StageStatus = Literal["pending", "running", "done", "failed", "needs_review", "skipped"]


class Row(BaseModel):
    """One workbook row. ``source`` holds the nine source columns and is read-only by contract."""

    model_config = ConfigDict(frozen=True)

    row_id: int
    year: int | None = None
    state: str | None = None
    source: Mapping[str, Any] = Field(default_factory=dict)


class StageResult(BaseModel):
    """The outcome of one stage for one row."""

    model_config = ConfigDict(frozen=True)

    row_id: int
    stage: str
    status: StageStatus
    gate_reason: str | None = None
    module: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    # Written to case_stage by the runner (build 04). ``summary_json`` is free-form and is what
    # the report aggregates; module chains put their per-module outcomes under ``modules``.
    input_hash: str | None = None
    tool_version: str | None = None
    error: str | None = None
    summary_json: dict[str, Any] = Field(default_factory=dict)


class Context(BaseModel):
    """What a component receives: configuration, database, capture client and a logger.

    ``capture`` is None until build 05 adds the capture client.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    config: Config
    db: sqlite3.Connection | None = None
    capture: Any | None = None
    log: logging.Logger

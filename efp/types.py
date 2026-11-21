"""Shared types every component uses. Builds 02 and 03 refine ``Row`` against the schema."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from efp.config import Config

StageStatus = Literal["done", "failed", "needs_review", "skipped", "gated"]


class Row(BaseModel):
    """One workbook row. ``source`` holds the nine source columns and is read-only by contract."""

    model_config = ConfigDict(frozen=True)

    row_id: str
    year: int | None = None
    state: str | None = None
    source: Mapping[str, Any] = Field(default_factory=dict)


class StageResult(BaseModel):
    """The outcome of one stage for one row."""

    model_config = ConfigDict(frozen=True)

    row_id: str
    stage: str
    status: StageStatus
    gate_reason: str | None = None
    module: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class Context(BaseModel):
    """What a component receives: configuration, database, capture client and a logger.

    ``capture`` is None until build 05 adds the capture client.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    config: Config
    db: sqlite3.Connection | None = None
    capture: Any | None = None
    log: logging.Logger

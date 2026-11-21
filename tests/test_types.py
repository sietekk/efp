from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

from efp.config import load_config
from efp.types import Context, Row, StageResult


def test_row_is_frozen() -> None:
    row = Row(row_id=1, year=2010, state="CT")
    with pytest.raises(ValidationError):
        row.year = 2011  # type: ignore[misc]


def test_stage_result_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        StageResult(row_id=1, stage="court", status="maybe")  # type: ignore[arg-type]


def test_context_carries_config_db_capture_and_logger(write_config: Callable[..., Path]) -> None:
    cfg = load_config(write_config())
    ctx = Context(config=cfg, log=logging.getLogger("efp.test"))
    assert ctx.config is cfg
    assert ctx.db is None
    assert ctx.capture is None
    assert ctx.log.name == "efp.test"

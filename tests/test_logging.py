from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path

from efp.config import load_config
from efp.logging import bind, setup_logging


def _lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_json_lines_carry_row_and_stage_when_bound(
    write_config: Callable[..., Path], data_dir: Path
) -> None:
    (data_dir / "logs").mkdir(parents=True)
    cfg = load_config(write_config())
    log = setup_logging(cfg)
    with bind(row_id="R-1", stage="court"):
        log.info("inside")
    log.info("outside")
    for handler in log.handlers:
        handler.flush()
    records = _lines(data_dir / "logs" / "efp.log")
    assert records[0]["msg"] == "inside"
    assert records[0]["row_id"] == "R-1"
    assert records[0]["stage"] == "court"
    assert records[1]["row_id"] is None
    assert records[1]["stage"] is None


def test_file_handler_is_skipped_when_logs_dir_is_absent(
    write_config: Callable[..., Path], data_dir: Path
) -> None:
    cfg = load_config(write_config())
    log = setup_logging(cfg)
    assert not any(isinstance(h, logging.FileHandler) for h in log.handlers)
    assert not data_dir.exists()


def test_setup_is_idempotent(write_config: Callable[..., Path]) -> None:
    cfg = load_config(write_config())
    setup_logging(cfg)
    log = setup_logging(cfg)
    ours = [h for h in log.handlers if getattr(h, "_efp_handler", False)]
    assert len(ours) == 1


def test_level_comes_from_config(write_config: Callable[..., Path]) -> None:
    cfg = load_config(write_config({'level = "INFO"': 'level = "ERROR"'}))
    assert setup_logging(cfg).level == logging.ERROR


def test_exceptions_are_logged_with_redaction(
    write_config: Callable[..., Path], data_dir: Path
) -> None:
    (data_dir / "logs").mkdir(parents=True)
    cfg = load_config(write_config())
    log = setup_logging(cfg, secret_values=["hunter2-sentinel"])
    try:
        raise ValueError("boom hunter2-sentinel")
    except ValueError:
        log.exception("failed with hunter2-sentinel")
    for handler in log.handlers:
        handler.flush()
    text = (data_dir / "logs" / "efp.log").read_text(encoding="utf-8")
    assert "hunter2-sentinel" not in text
    assert "***" in text

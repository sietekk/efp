"""Structured logging: JSON lines to ``paths.logs/efp.log`` and a readable console stream.

Every record carries ``row_id`` and ``stage`` when known (set with :func:`bind`). Loaded secret
values are redacted from messages and exception text as a second line of defence; the first is
that secrets are never passed to the logger.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import sys
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path

from efp.config import Config

_row_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("efp_row_id", default=None)
_stage: contextvars.ContextVar[str | None] = contextvars.ContextVar("efp_stage", default=None)
_secret_values: set[str] = set()

_MARK = "_efp_handler"


@contextlib.contextmanager
def bind(row_id: str | None = None, stage: str | None = None) -> Iterator[None]:
    """Attach ``row_id`` and/or ``stage`` to every record logged inside the block."""
    tokens = []
    if row_id is not None:
        tokens.append((_row_id, _row_id.set(row_id)))
    if stage is not None:
        tokens.append((_stage, _stage.set(stage)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def register_secret_values(values: Iterable[str]) -> None:
    """Tell the redaction filter which values must never appear in log output."""
    _secret_values.update(v for v in values if v)


def redact(text: str) -> str:
    for value in sorted(_secret_values, key=len, reverse=True):
        text = text.replace(value, "***")
    return text


class _ContextFilter(logging.Filter):
    """Adds row_id/stage and redacts secret values from the message and exception text."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.row_id = _row_id.get()
        record.stage = _stage.get()
        record.msg = redact(record.getMessage())
        record.args = None
        if record.exc_info:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "row_id": getattr(record, "row_id", None),
            "stage": getattr(record, "stage", None),
            "exc": record.exc_text,
        }
        return json.dumps(payload, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        when = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        ctx = " ".join(
            f"{k}={v}"
            for k, v in (
                ("stage", getattr(record, "stage", None)),
                ("row", getattr(record, "row_id", None)),
            )
            if v
        )
        text = f"{when} {record.levelname:<7} {f'[{ctx}] ' if ctx else ''}{record.getMessage()}"
        if record.exc_text:
            text += "\n" + record.exc_text
        return text


def setup_logging(config: Config | None, secret_values: Iterable[str] = ()) -> logging.Logger:
    """Configure the ``efp`` logger. Idempotent. Never creates the log directory.

    The JSON file handler is added only when ``paths.logs`` already exists; selftest reports
    when it does not. With no config the console stream runs at INFO.
    """
    register_secret_values(secret_values)
    logger = logging.getLogger("efp")
    for handler in list(logger.handlers):
        if getattr(handler, _MARK, False):
            logger.removeHandler(handler)
            handler.close()
    level = getattr(logging, config.logging.level) if config is not None else logging.INFO
    logger.setLevel(level)
    logger.propagate = False

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(ConsoleFormatter())
    handlers: list[logging.Handler] = [console]

    if config is not None:
        log_dir: Path = config.paths.logs
        if log_dir.is_dir():
            file_handler = logging.FileHandler(log_dir / "efp.log", encoding="utf-8")
            file_handler.setFormatter(JsonFormatter())
            handlers.append(file_handler)

    for handler in handlers:
        handler.addFilter(_ContextFilter())
        setattr(handler, _MARK, True)
        logger.addHandler(handler)
    return logger

"""Model spend: the monthly cap, the per-call cost, and the ``llm_calls`` log.

The cap is ``llm.max_monthly_usd`` over the calendar month (UTC). A call is made only when the
month's spend plus the call's worst-case cost fits under the cap, so the cap is never passed by a
single call. Prices come from config (``llm.price_*_per_mtok``). Every call, successful or not, is
a row in ``llm_calls``; ``temperature`` is NULL where the model rejects it.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from efp.capture import CaptureError, LlmCallError
from efp.config import Config

TEMPERATURE_NOTE = (
    "not sent: claude-sonnet-5-5 and API version 2026-10-21 reject temperature "
    "(llm.send_temperature = false)"
)
# An image costs about width x height / 750 tokens; the page is shrunk to 1,568 px on its longer
# side, so a square page is the worst case (~3,300), plus the prompt and slack.
PROMPT_TOKENS_ESTIMATE = 3_600


def _month_start(now: datetime | None = None) -> str:
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(
        timespec="seconds"
    )


def spent_this_month(
    conn: sqlite3.Connection, now: datetime | None = None, purpose: str | None = None
) -> float:
    """The month's logged spend: every purpose for the cap, one purpose for a report line."""
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE called_at >= ? "
        "AND (? IS NULL OR purpose = ?)",
        (_month_start(now), purpose, purpose),
    ).fetchone()
    return float(row[0])


def cost_usd(config: Config, input_tokens: int, output_tokens: int) -> float:
    llm = config.llm
    return (
        input_tokens * llm.price_input_per_mtok + output_tokens * llm.price_output_per_mtok
    ) / 1e6


def worst_case_usd(config: Config, max_tokens: int) -> float:
    return cost_usd(config, PROMPT_TOKENS_ESTIMATE, max_tokens)


def room_for_call(conn: sqlite3.Connection, config: Config, max_tokens: int) -> tuple[bool, str]:
    spent = spent_this_month(conn)
    need = worst_case_usd(config, max_tokens)
    cap = config.llm.max_monthly_usd
    if spent + need > cap:
        return (
            False,
            f"llm.max_monthly_usd ({cap:g}) would be passed: ${spent:.2f} spent this month",
        )
    return True, ""


def log_call(
    conn: sqlite3.Connection,
    *,
    row_id: int | None,
    stage: str,
    purpose: str,
    capture_id: int | None,
    model_id: str,
    prompt_sha256: str | None,
    temperature: float | None,
    thinking: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    cost: float,
    request_id: str | None,
    ok: bool,
    error: str | None = None,
) -> int:
    cursor = conn.execute(
        "INSERT INTO llm_calls (row_id, stage, purpose, capture_id, model_id, prompt_sha256, "
        "temperature, temperature_note, thinking, input_tokens, output_tokens, cost_usd, "
        "request_id, ok, error, called_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            row_id,
            stage,
            purpose,
            capture_id,
            model_id,
            prompt_sha256,
            temperature,
            None if temperature is not None else TEMPERATURE_NOTE,
            thinking,
            input_tokens,
            output_tokens,
            cost,
            request_id,
            1 if ok else 0,
            error,
            datetime.now(UTC).isoformat(timespec="seconds"),
        ),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def failure_cost(config: Config, exc: CaptureError, max_tokens: int) -> float:
    """What a failed call is logged as costing. An HTTP error answer was not billed (0). A call
    that may have been billed (a timeout or dropped connection after the request went out, or an
    answer that could not be recorded) is logged at the tokens it used when they are known, else
    at the worst case, so the cap never undercounts."""
    if not isinstance(exc, LlmCallError):
        return 0.0
    if exc.input_tokens is not None and exc.output_tokens is not None:
        return cost_usd(config, exc.input_tokens, exc.output_tokens)
    return worst_case_usd(config, max_tokens)

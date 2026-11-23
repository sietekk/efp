"""Every model call the pipeline makes goes through :func:`call_model`: one place that shapes the
request, makes the call, logs it and prices it, so no caller can get any of those wrong on its own.

**Temperature is never sent.** The project rule is temperature 0, but API version 2026-10-21 (the
one the installed SDK speaks) rejects ``temperature`` (HTTP 400 "Extra inputs are not permitted",
confirmed live for claude-sonnet-5-5) and the SDK's ``messages.create`` no longer has the argument.
``Capture.llm_message`` has no temperature parameter, so there is no path that sends one;
reproducibility rests on the pinned model id, the hashed prompt file, thinking turned off and low
effort, all logged per call. ``llm_calls.temperature`` is NULL with :data:`TEMPERATURE_NOTE`.
``llm.temperature = 0.0`` stays in the config as the rule's value, not as something that is sent.

**The cap.** ``llm.max_monthly_usd`` over the calendar month (UTC), from the logged ``llm_calls``. A
call is made only when the month's spend plus its worst-case cost fits. A call that may have been
billed although no usable answer came back is logged at its tokens when known, else at the worst
case, so the cap never undercounts; an HTTP error answer is not billed (0).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from efp.capture import Capture, CaptureError, LlmCallError, LlmHttpError, LlmResult
from efp.config import Config

TEMPERATURE_POLICY = "omitted"  # in every request signature: a change here revisits rows
TEMPERATURE_NOTE = (
    "not sent: API version 2026-10-21 rejects temperature (HTTP 400, confirmed live for "
    "claude-sonnet-5-5); reproducibility rests on the model id, the prompt hash, thinking off, "
    "low effort"
)
# An image costs about width x height / 750 tokens; a page is shrunk to 1,568 px on its longer
# side, so a square page is the worst case (~3,300), plus the prompt and slack.
PROMPT_TOKENS_ESTIMATE = 3_600


def request_shape(model: str) -> tuple[dict[str, str] | None, str | None]:
    """``(thinking, effort)`` for a model. Sonnet 5.5 has thinking on by default and rejects
    ``disabled``; ``between_tools`` is how it is turned off, accepted at effort ``high`` or below
    (verified live). Any other model gets neither field."""
    if model.startswith("claude-sonnet-5-5"):
        return {"type": "between_tools"}, "low"
    return None, None


def request_signature(model: str, max_tokens: int) -> list[Any]:
    """Everything that shapes a model's answer apart from the prompt text and the image: part of a
    stage's input hash, so changing any of it makes ``--recheck`` revisit the rows it affects."""
    thinking, effort = request_shape(model)
    return [model, thinking, effort, max_tokens, TEMPERATURE_POLICY]


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


def failure_cost(config: Config, exc: BaseException, max_tokens: int) -> float:
    """What a failed call is logged as costing. An HTTP error answer (and anything that never left
    the machine) was not billed. A call that may have been billed (a timeout or dropped connection
    after the request went out, or an answer that could not be recorded) is logged at the tokens it
    used when they are known, else at the worst case."""
    if not isinstance(exc, LlmCallError):
        return 0.0
    if exc.input_tokens is not None and exc.output_tokens is not None:
        return cost_usd(config, exc.input_tokens, exc.output_tokens)
    return worst_case_usd(config, max_tokens)


def log_call(
    conn: sqlite3.Connection,
    *,
    row_id: int | None,
    stage: str,
    purpose: str,
    capture_id: int | None,
    model_id: str,
    prompt_sha256: str | None,
    thinking: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    cost: float,
    request_id: str | None,
    ok: bool,
    error: str | None = None,
) -> int:
    """One ``llm_calls`` row. There is no temperature argument: none is ever sent."""
    cursor = conn.execute(
        "INSERT INTO llm_calls (row_id, stage, purpose, capture_id, model_id, prompt_sha256, "
        "temperature, temperature_note, thinking, input_tokens, output_tokens, cost_usd, "
        "request_id, ok, error, called_at) VALUES (?,?,?,?,?,?,NULL,?,?,?,?,?,?,?,?,?)",
        (
            row_id,
            stage,
            purpose,
            capture_id,
            model_id,
            prompt_sha256,
            TEMPERATURE_NOTE,
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


@dataclass
class CallResult:
    """What :func:`call_model` made of one call. ``reply`` is set when an answer came back."""

    reply: LlmResult | None
    error: str | None
    cost: float
    possibly_billed: bool = False


def call_model(
    capture: Capture,
    conn: sqlite3.Connection,
    config: Config,
    *,
    api_key: str,
    row_id: int | None,
    stage: str,
    purpose: str,
    system: str,
    content: list[dict[str, Any]],
    max_tokens: int,
    prompt_sha256: str | None,
    model: str | None = None,
) -> CallResult:
    """Make one model call and log it. Never raises: a failure of any kind (an API error, a refused
    parameter, an SDK ``TypeError``, a store that failed) comes back as ``CallResult.error`` and is
    in ``llm_calls`` with the cost it may have had."""
    model = model or config.llm.transcription_model
    thinking, effort = request_shape(model)
    shape = thinking["type"] if thinking else None
    common: dict[str, Any] = {
        "row_id": row_id,
        "stage": stage,
        "purpose": purpose,
        "model_id": model,
        "prompt_sha256": prompt_sha256,
        "thinking": shape,
    }
    try:
        reply = capture.llm_message(
            api_key=api_key,
            row_id=row_id,
            stage=stage,
            purpose=purpose,
            model=model,
            system=system,
            content=content,
            max_tokens=max_tokens,
            thinking=thinking,
            effort=effort,
        )
    except Exception as exc:  # whatever went wrong, the call is logged and the caller carries on
        billed = isinstance(exc, LlmCallError)
        cost = failure_cost(config, exc, max_tokens)
        message = f"{type(exc).__name__}: {exc}" if not isinstance(exc, CaptureError) else str(exc)
        log_call(
            conn,
            capture_id=getattr(exc, "capture_id", None),
            input_tokens=getattr(exc, "input_tokens", None),
            output_tokens=getattr(exc, "output_tokens", None),
            cost=cost,
            request_id=getattr(exc, "request_id", None),
            ok=False,
            error=message[:280] + (" [billed or possibly billed]" if billed else ""),
            **common,
        )
        return CallResult(None, message[:300], cost, billed)
    refused = reply.stop_reason == "refusal"
    cost = cost_usd(config, reply.input_tokens, reply.output_tokens)
    log_call(
        conn,
        capture_id=reply.capture_id,
        input_tokens=reply.input_tokens,
        output_tokens=reply.output_tokens,
        cost=cost,
        request_id=reply.request_id,
        ok=not refused,
        error="refusal" if refused else None,
        **common,
    )
    return CallResult(reply, "refusal" if refused else None, cost)


__all__ = [
    "CallResult",
    "LlmHttpError",
    "call_model",
    "cost_usd",
    "failure_cost",
    "log_call",
    "request_shape",
    "request_signature",
    "room_for_call",
    "spent_this_month",
    "worst_case_usd",
]

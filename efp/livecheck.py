"""``efp selftest --live``: the checks that need the real network (never run in CI).

Each goes through the capture layer, so each is budgeted, logged and recorded under
``stage = 'selftest'`` like every other call.
"""

from __future__ import annotations

import hashlib
import sqlite3

from rfc3161_client import (
    HashAlgorithm,
    PKIStatus,
    TimestampRequestBuilder,
    decode_timestamp_response,
)

from efp import wayback
from efp.capture import Capture, CaptureError
from efp.config import Config
from efp.manifest import CaptureTsa, ManifestError
from efp.selftest import Check

CDX_TARGET = "https://www.iana.org/"  # a stable public page with a long Wayback history


def _court_live(capture: Capture, config: Config) -> list[Check]:
    """One DOJ press-release query and one CourtListener search."""
    import json

    from efp.court import doj
    from efp.court.courtlistener import SEARCH, token
    from efp.runner import make_context

    out: list[Check] = []
    try:
        record = capture.fetch(
            doj.API,
            row_id=None,
            stage="selftest",
            kind="api",
            params={"pagesize": "1", "sort": "date", "direction": "DESC"},
        )
        found = (
            doj.parse_releases(json.loads(capture.read_body(record)), record.capture_id)
            if record.ok
            else []
        )
        out.append(
            Check("live: doj press releases", bool(found), f"{len(found)} release(s) returned")
        )
    except (CaptureError, ValueError) as exc:
        out.append(Check("live: doj press releases", False, str(exc)))
    secret = token(make_context(config, None))
    if not secret:
        out.append(
            Check("live: courtlistener search", False, "COURTLISTENER_TOKEN not set; skipped")
        )
        return out
    try:
        with capture.wait_limit(1.0):
            record = capture.fetch(
                SEARCH,
                row_id=None,
                stage="selftest",
                kind="api",
                params={"type": "r", "q": "United States v.", "page_size": "1"},
                headers={"Authorization": f"Token {secret}"},
            )
        out.append(Check("live: courtlistener search", record.ok, f"HTTP {record.http_status}"))
    except CaptureError as exc:
        out.append(Check("live: courtlistener search", False, str(exc)))
    return out


def _ag_live(capture: Capture, config: Config, conn: sqlite3.Connection) -> list[Check]:
    """One listing page per automated state AG adapter. A failing adapter is recorded as ``broken``
    in ``state_adapters`` (so the coverage matrix says so), never silently skipped."""
    import logging

    from efp.court.adapters.ag import adapters, sync
    from efp.court.adapters.ag.base import SiteBlocked, is_transient
    from efp.types import Context

    ctx = Context(config=config, db=conn, capture=capture, log=logging.getLogger("efp.selftest"))
    out: list[Check] = []
    for state, cls in adapters().items():
        if cls.mode != "automated":
            continue
        adapter = cls(ctx)
        name = f"live: state ag {state}"
        try:
            found = adapter.probe()
            ok = bool(found) and all(r.date for r in found)
            detail = f"{len(found)} dated release(s) on the first page"
            if ok:
                sync.write(
                    conn,
                    state,
                    mode="automated",
                    portal=cls.portal,
                    adapter_module=cls.__module__,
                    reason=None,
                )
            else:
                sync.mark(
                    conn, state, "broken", "live smoke: the first page parsed to no dated release"
                )
        except SiteBlocked as exc:
            # a refusal during a smoke test is reported, not recorded: `efp court ag verify` is
            # where a state becomes manual
            ok, detail = False, f"{exc} (not recorded)"
        except CaptureError as exc:
            ok, detail = False, str(exc)
            if is_transient(exc):
                detail += " (transient; not recorded)"
            else:
                sync.mark(conn, state, "broken", f"live smoke: {exc}"[:200])
        finally:
            adapter.close()
        out.append(Check(name, ok, detail))
    return out


def _docket_live(capture: Capture, config: Config) -> list[Check]:
    """One load of each cleared automated docket portal's entry point (never a search)."""
    from efp.court.adapters.docket import adapters

    out: list[Check] = []
    for state, cls in adapters().items():
        if state not in config.court.docket_terms_cleared:
            continue
        name = f"live: state docket {state}"
        try:
            record = capture.fetch(cls.portal, row_id=None, stage="selftest", kind="page")
            out.append(Check(name, record.ok, f"entry point answered HTTP {record.http_status}"))
        except CaptureError as exc:
            out.append(Check(name, False, str(exc)))
    return out


def _model_live(capture: Capture, config: Config, conn: sqlite3.Connection) -> list[Check]:
    """One model call with the exact request shape page transcription uses (thinking setting,
    effort, no temperature) on a synthetic page image, so a rejected parameter shows up here and
    not as a silently dead fallback. Runs only when the operator has enabled the fallback and the
    key is set; the call is logged in ``llm_calls`` (purpose ``selftest``) and costs a fraction of
    a cent."""
    import base64

    from efp.documents import llm, pdfio
    from efp.documents.modules import PROMPT, thinking_for
    from efp.secrets import SecretsError, load_secrets

    name = "live: model transcription request"
    if not config.documents.llm_transcription_fallback:
        return [Check(name, True, "skipped: documents.llm_transcription_fallback is false")]
    try:
        key = load_secrets(config.paths.env_file).get("ANTHROPIC_API_KEY")
    except SecretsError as exc:
        return [Check(name, False, f"cannot read the env file: {exc}")]
    if key is None:
        return [Check(name, False, "ANTHROPIC_API_KEY is not set")]
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return [Check(name, False, "Pillow is not installed (the documents extra)")]
    image = Image.new("RGB", (800, 120), "white")
    ImageDraw.Draw(image).text((20, 40), "The court accepted the plea.", fill="black")
    data = base64.standard_b64encode(pdfio.png_bytes(image)).decode("ascii")
    model = config.llm.transcription_model
    thinking, effort = thinking_for(model)
    temperature = config.llm.temperature if config.llm.send_temperature else None
    system = PROMPT.read_text(encoding="utf-8")
    try:
        reply = capture.llm_message(
            api_key=key, row_id=None, stage="selftest", purpose="selftest", model=model,
            system=system, max_tokens=200, thinking=thinking, effort=effort,
            temperature=temperature,
            content=[
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": data},
                },
                {"type": "text", "text": "Transcribe this page."},
            ],
        )  # fmt: skip
    except CaptureError as exc:
        llm.log_call(
            conn, row_id=None, stage="selftest", purpose="selftest", capture_id=None,
            model_id=model, prompt_sha256=None, temperature=temperature,
            thinking=thinking["type"] if thinking else None, input_tokens=None,
            output_tokens=None, cost=0.0, request_id=None, ok=False, error=str(exc)[:300],
        )  # fmt: skip
        return [Check(name, False, str(exc))]
    llm.log_call(
        conn, row_id=None, stage="selftest", purpose="selftest", capture_id=reply.capture_id,
        model_id=model, prompt_sha256=None, temperature=temperature,
        thinking=thinking["type"] if thinking else None, input_tokens=reply.input_tokens,
        output_tokens=reply.output_tokens,
        cost=llm.cost_usd(config, reply.input_tokens, reply.output_tokens),
        request_id=reply.request_id, ok=True,
    )  # fmt: skip
    read = " ".join(reply.text.split())[:60]
    return [Check(name, bool(read), f"{model} answered ({reply.output_tokens} tokens): {read!r}")]


def live_checks(config: Config, conn: sqlite3.Connection) -> list[Check]:
    capture = Capture(config, conn)
    checks: list[Check] = []
    try:
        try:
            rows = wayback.cdx(capture, CDX_TARGET, stage="selftest", limit=3)
            checks.append(
                Check(
                    "live: wayback cdx",
                    bool(rows),
                    f"{len(rows)} capture(s) listed for {CDX_TARGET}",
                )
            )
        except CaptureError as exc:
            checks.append(Check("live: wayback cdx", False, str(exc)))
        checks.extend(_court_live(capture, config))
        checks.extend(_ag_live(capture, config, conn))
        checks.extend(_docket_live(capture, config))
        checks.extend(_model_live(capture, config, conn))
        digest = hashlib.sha256(b"efp selftest").digest()
        request = (
            TimestampRequestBuilder()
            .data(b"efp selftest")
            .hash_algorithm(HashAlgorithm.SHA256)
            .nonce(nonce=True)
            .cert_request(cert_request=True)
            .build()
        )
        for url in config.capture.tsa_urls:
            try:
                der = CaptureTsa(capture).post(url, request.as_bytes())
                response = decode_timestamp_response(der)
                ok = (
                    response.status in (PKIStatus.GRANTED, PKIStatus.GRANTED_WITH_MODS)
                    and response.tst_info.message_imprint.message == digest
                )
                checks.append(
                    Check(
                        f"live: rfc3161 {url}",
                        ok,
                        "token granted, imprint matches"
                        if ok
                        else "TSA answered but the token is not usable",
                    )
                )
            except (CaptureError, ManifestError, ValueError) as exc:
                checks.append(Check(f"live: rfc3161 {url}", False, str(exc)))
    finally:
        capture.close()
    return checks

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
    """One model call with the exact request ``efp.llm.call_model`` makes for page transcription
    (thinking setting, effort, no temperature) on a synthetic page image, so a rejected parameter
    shows up here and not as a silently dead fallback. Runs only when the operator has enabled the
    fallback and the key is set; the call is logged in ``llm_calls`` (purpose ``selftest``) and
    costs a fraction of a cent. Any failure, of any kind, is a failed check, never a crash."""
    name = "live: model transcription request"
    if not config.documents.llm_transcription_fallback:
        return [Check(name, True, "skipped: documents.llm_transcription_fallback is false")]
    try:
        return [_model_check(name, capture, config, conn)]
    except Exception as exc:  # the live run reports every check, whatever one of them hits
        return [Check(name, False, f"{type(exc).__name__}: {exc}"[:300])]


def _model_check(name: str, capture: Capture, config: Config, conn: sqlite3.Connection) -> Check:
    import base64

    from PIL import Image, ImageDraw

    from efp import llm
    from efp.documents import pdfio
    from efp.documents.modules import PROMPT
    from efp.secrets import load_secrets

    key = load_secrets(config.paths.env_file).get("ANTHROPIC_API_KEY")
    if key is None:
        return Check(name, False, "ANTHROPIC_API_KEY is not set")
    image = Image.new("RGB", (800, 120), "white")
    ImageDraw.Draw(image).text((20, 40), "The court accepted the plea.", fill="black")
    data = base64.standard_b64encode(pdfio.png_bytes(image)).decode("ascii")
    done = llm.call_model(
        capture,
        conn,
        config,
        api_key=key,
        row_id=None,
        stage="selftest",
        purpose="selftest",
        system=PROMPT.read_text(encoding="utf-8"),
        content=[
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": data},
            },
            {"type": "text", "text": "Transcribe this page."},
        ],
        max_tokens=200,
        prompt_sha256=None,
    )
    if done.reply is None or done.error is not None:
        return Check(name, False, str(done.error))
    read = " ".join(done.reply.text.split())[:60]
    model = done.reply.model
    return Check(
        name,
        bool(read),
        f"{model} answered ({done.reply.output_tokens} tokens): {read!r}",
    )


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

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

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

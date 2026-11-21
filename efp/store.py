"""Content-addressed store: ``<store>/<sha256[:2]>/<sha256>``.

Bodies of captured pages and documents live here, never in the database. ``captures.body_path``
holds the spec's relative form ``store/<aa>/<sha256>``, resolved against ``paths.store`` so the
stored value does not depend on where the data directory is. Build 05's capture layer builds on
these functions; nothing else should compute a store path.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import tempfile
from pathlib import Path

from efp.config import Config

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class StoreError(Exception):
    """A hash is malformed, a body is missing, or a body does not match its hash."""


def _checked(sha256: str) -> str:
    if not _SHA256.match(sha256):
        raise StoreError("not a SHA-256 digest (64 lowercase hex characters)")
    return sha256


def store_path(sha256: str, store_root: Path) -> Path:
    """Pure: where the body with this digest lives. Rejects anything that is not a digest."""
    digest = _checked(sha256)
    return store_root / digest[:2] / digest


def config_store_path(sha256: str, config: Config) -> Path:
    return store_path(sha256, config.paths.store)


def body_relpath(sha256: str) -> str:
    """The value written to ``captures.body_path``."""
    digest = _checked(sha256)
    return f"store/{digest[:2]}/{digest}"


def resolve_body_path(store_root: Path, body_path: str) -> Path:
    """Map a recorded ``store/<aa>/<sha256>`` back to a file under ``store_root``."""
    parts = body_path.split("/")
    if len(parts) != 3 or parts[0] != "store":
        raise StoreError("body_path is not of the form store/<aa>/<sha256>")
    digest = _checked(parts[2])
    if parts[1] != digest[:2]:
        raise StoreError("body_path directory does not match its digest")
    return store_path(digest, store_root)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_body(store_root: Path, data: bytes) -> str:
    """Store ``data`` under its digest and return the digest. Idempotent; never overwrites.

    The file is written to a temporary name in the same directory and linked into place, so a
    reader never sees a partial body and an existing body is never replaced.
    """
    digest = sha256_hex(data)
    target = store_path(digest, store_root)
    if target.exists():
        return digest
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        with contextlib.suppress(FileExistsError):  # a concurrent writer stored it first
            os.link(tmp_name, target)
    finally:
        os.unlink(tmp_name)
    return digest


def read_verified(store_root: Path, sha256: str) -> bytes:
    """Read a body and confirm it still hashes to its name."""
    path = store_path(sha256, store_root)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise StoreError(f"body not found in store: {sha256[:12]}...") from None
    if sha256_hex(data) != sha256:
        raise StoreError(f"stored body does not match its hash: {sha256[:12]}...")
    return data

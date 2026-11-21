from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest

from efp.config import Config
from efp.store import (
    StoreError,
    body_relpath,
    config_store_path,
    read_verified,
    resolve_body_path,
    sha256_hex,
    store_path,
    write_body,
)

BODY = b"synthetic captured page body\n"
DIGEST = hashlib.sha256(BODY).hexdigest()


def test_store_path_is_pure_and_content_addressed(tmp_path: Path) -> None:
    path = store_path(DIGEST, tmp_path)
    assert path == tmp_path / DIGEST[:2] / DIGEST
    assert store_path(DIGEST, tmp_path) == path
    assert not path.exists()  # pure: nothing was created


def test_config_store_path_uses_paths_store(config: Config) -> None:
    assert config_store_path(DIGEST, config) == config.paths.store / DIGEST[:2] / DIGEST


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "abc",
        "../../etc/passwd",
        DIGEST.upper(),
        DIGEST[:-1],
        DIGEST + "0",
        "g" * 64,
        DIGEST[:2] + "/" + DIGEST[3:],
    ],
)
def test_malformed_digests_are_rejected(tmp_path: Path, bad: str) -> None:
    with pytest.raises(StoreError):
        store_path(bad, tmp_path)
    with pytest.raises(StoreError):
        body_relpath(bad)


def test_body_relpath_matches_the_spec_form() -> None:
    assert body_relpath(DIGEST) == f"store/{DIGEST[:2]}/{DIGEST}"


def test_resolve_body_path_round_trips_and_rejects_other_forms(tmp_path: Path) -> None:
    assert resolve_body_path(tmp_path, body_relpath(DIGEST)) == store_path(DIGEST, tmp_path)
    for bad in (
        DIGEST,
        f"store/{DIGEST}",
        f"elsewhere/{DIGEST[:2]}/{DIGEST}",
        f"store/zz/{DIGEST}",
        f"store/{DIGEST[:2]}/../{DIGEST}",
        f"/abs/store/{DIGEST[:2]}/{DIGEST}",
    ):
        with pytest.raises(StoreError):
            resolve_body_path(tmp_path, bad)


def test_write_read_hash_round_trip(tmp_path: Path) -> None:
    assert write_body(tmp_path, BODY) == DIGEST == sha256_hex(BODY)
    assert store_path(DIGEST, tmp_path).read_bytes() == BODY
    assert read_verified(tmp_path, DIGEST) == BODY


def test_write_is_idempotent_and_never_overwrites(tmp_path: Path) -> None:
    write_body(tmp_path, BODY)
    target = store_path(DIGEST, tmp_path)
    inode = target.stat().st_ino
    assert write_body(tmp_path, BODY) == DIGEST
    assert target.stat().st_ino == inode


def test_no_temporary_files_are_left_behind(tmp_path: Path) -> None:
    write_body(tmp_path, BODY)
    write_body(tmp_path, BODY)
    names = [p.name for p in (tmp_path / DIGEST[:2]).iterdir()]
    assert names == [DIGEST]


def test_modes_are_private(tmp_path: Path) -> None:
    write_body(tmp_path, BODY)
    assert stat.S_IMODE(os.stat(store_path(DIGEST, tmp_path)).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(store_path(DIGEST, tmp_path).parent).st_mode) == 0o700


def test_tampering_is_detected(tmp_path: Path) -> None:
    write_body(tmp_path, BODY)
    path = store_path(DIGEST, tmp_path)
    path.chmod(0o600)
    path.write_bytes(BODY + b"edited")
    with pytest.raises(StoreError, match="does not match its hash"):
        read_verified(tmp_path, DIGEST)


def test_missing_body_is_reported(tmp_path: Path) -> None:
    with pytest.raises(StoreError, match="not found"):
        read_verified(tmp_path, DIGEST)


def test_a_dangling_symlink_at_the_target_is_an_error_not_a_silent_success(tmp_path: Path) -> None:
    target = store_path(DIGEST, tmp_path)
    target.parent.mkdir(parents=True)
    target.symlink_to(tmp_path / "nowhere")
    with pytest.raises(StoreError, match="not a regular file"):
        write_body(tmp_path, BODY)


def test_a_symlink_to_valid_content_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.write_bytes(BODY)
    target = store_path(DIGEST, tmp_path)
    target.parent.mkdir(parents=True)
    target.symlink_to(real)
    with pytest.raises(StoreError, match="not a regular file"):
        write_body(tmp_path, BODY)


def test_a_corrupt_existing_entry_is_reported_not_trusted(tmp_path: Path) -> None:
    target = store_path(DIGEST, tmp_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"corrupt")
    with pytest.raises(StoreError, match="does not match its hash"):
        write_body(tmp_path, BODY)
    assert target.read_bytes() == b"corrupt"  # never overwritten

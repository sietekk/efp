from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import bagit
import pytest

from efp import manifest as mf
from efp.capture import Capture
from efp.config import Config, load_config
from tests.capturehelpers import make_capture
from tests.dbhelpers import insert_row
from tests.fakets import make_tsr

WHEN = datetime(2026, 11, 22, 9, 30, tzinfo=UTC)


@pytest.fixture
def config(write_config: Callable[..., Path]) -> Config:
    """The suite's config with two independent TSAs, as production has."""
    return load_config(
        write_config(
            {
                'tsa_urls = ["http://tsa.example.test"]': (
                    'tsa_urls = ["http://tsa-a.example.test", "http://tsa-b.example.test"]'
                )
            }
        )
    )


@pytest.fixture
def cap(config: Config, conn: sqlite3.Connection) -> Iterator[Capture]:
    insert_row(conn, 1)
    conn.commit()
    client = make_capture(config, conn)
    yield client
    client.close()


def _add(cap: Capture, *bodies: bytes) -> list[int]:
    return [
        cap.store_bytes(
            b,
            row_id=1,
            stage="resolve_links",
            url_requested=f"https://e.test/{i}",
            tool="t",
            http_status=200,
        ).capture_id
        for i, b in enumerate(bodies)
    ]


class FakeTsa:
    """Answers every request with a granted token for a fixed imprint (or fails the named URLs)."""

    def __init__(self, imprint: bytes, fail: frozenset[str] = frozenset()) -> None:
        self.imprint, self.fail, self.calls = imprint, fail, []

    def post(self, url: str, request_der: bytes) -> bytes:
        self.calls.append(url)
        if url in self.fail:
            raise mf.ManifestError(f"{url} unreachable")
        return make_tsr(self.imprint, WHEN)


def test_the_manifest_lists_every_new_body_once_sorted_with_a_header(
    cap: Capture, config: Config, conn: sqlite3.Connection
) -> None:
    ids = _add(cap, b"bravo", b"alpha", b"alpha")  # a duplicate body is listed once
    result = mf.build_manifest(config, conn, now=WHEN)
    assert result is not None and result.entry_count == 2
    text = result.path.read_text()
    lines = text.splitlines()
    assert lines[0] == "# efp-manifest 1" and "# iso-week: 2026-W47" in lines
    assert "# previous: none" in lines and f"# captures: first={ids[0]} last={ids[-1]}" in lines
    body = [ln for ln in lines if not ln.startswith("#")]
    shas = [ln.split("  ")[0] for ln in body]
    assert (
        shas == sorted(shas) == sorted(hashlib.sha256(b).hexdigest() for b in (b"alpha", b"bravo"))
    )
    assert body[0].endswith(f"store/{shas[0][:2]}/{shas[0]}")
    assert result.sha256 == hashlib.sha256(result.path.read_bytes()).hexdigest()
    assert (result.path.stat().st_mode & 0o222) == 0  # written once, read-only
    assert conn.execute("SELECT count(*) FROM manifest_captures").fetchone()[0] == 3


def test_a_second_manifest_covers_only_what_is_new_and_chains_to_the_first(
    cap: Capture, config: Config, conn: sqlite3.Connection
) -> None:
    _add(cap, b"one")
    first = mf.build_manifest(config, conn, now=WHEN)
    assert mf.build_manifest(config, conn, now=WHEN) is None  # nothing new
    _add(cap, b"two")
    second = mf.build_manifest(config, conn, now=WHEN)
    assert first is not None and second is not None and first.path != second.path
    assert second.entry_count == 1
    assert f"# previous: {first.sha256}" in second.path.read_text().splitlines()


def test_two_tsas_stamp_the_manifest_and_verify_accepts_the_tokens(
    cap: Capture, config: Config, conn: sqlite3.Connection
) -> None:
    ids = _add(cap, b"alpha", b"bravo")
    built = mf.build_manifest(config, conn, now=WHEN)
    assert built is not None
    tsa = FakeTsa(hashlib.sha256(built.path.read_bytes()).digest())
    outcome = mf.timestamp_manifest(config, conn, built.manifest_id, tsa, now=WHEN)
    assert outcome.errors == [] and len(outcome.tokens) == len(config.capture.tsa_urls)
    assert tsa.calls == list(config.capture.tsa_urls)
    for token in outcome.tokens:
        assert (
            token.path.parent == built.path.parent and token.gen_time == "2026-11-22T09:30:00+00:00"
        )
    paths = {
        r[0]
        for r in conn.execute("SELECT tsa_token_path FROM captures WHERE capture_id IN (?, ?)", ids)
    }
    assert paths == {outcome.tokens[0].path.name}  # set once for every covered row
    verdicts = mf.verify_manifest(config, conn, built.manifest_id)
    assert all(v.granted and v.imprint_matches for v in verdicts) and len(verdicts) == len(
        outcome.tokens
    )
    assert all("signature NOT checked" in v.detail for v in verdicts)  # no trust anchors pinned
    assert all(v.signature is None for v in verdicts)


def test_a_manifest_edited_after_timestamping_fails_verification(
    cap: Capture, config: Config, conn: sqlite3.Connection
) -> None:
    _add(cap, b"alpha")
    built = mf.build_manifest(config, conn, now=WHEN)
    assert built is not None
    mf.timestamp_manifest(
        config, conn, built.manifest_id, FakeTsa(hashlib.sha256(built.path.read_bytes()).digest())
    )
    built.path.chmod(0o644)
    built.path.write_text(built.path.read_text() + "0" * 64 + "  store/00/tampered\n")
    verdicts = mf.verify_manifest(config, conn, built.manifest_id)
    assert not any(v.ok for v in verdicts) and not any(v.imprint_matches for v in verdicts)


def test_timestamping_refuses_a_manifest_that_no_longer_matches_its_hash(
    cap: Capture, config: Config, conn: sqlite3.Connection
) -> None:
    _add(cap, b"alpha")
    built = mf.build_manifest(config, conn, now=WHEN)
    assert built is not None
    built.path.chmod(0o644)
    built.path.write_text("changed\n")
    with pytest.raises(mf.ManifestError, match="no longer matches"):
        mf.timestamp_manifest(config, conn, built.manifest_id, FakeTsa(b"\0" * 32))


def test_one_tsa_down_is_reported_and_all_down_is_an_error(
    cap: Capture, config: Config, conn: sqlite3.Connection
) -> None:
    _add(cap, b"alpha")
    built = mf.build_manifest(config, conn, now=WHEN)
    assert built is not None
    imprint = hashlib.sha256(built.path.read_bytes()).digest()
    urls = frozenset(config.capture.tsa_urls)
    one_down = frozenset(sorted(urls)[:1])
    outcome = mf.timestamp_manifest(
        config, conn, built.manifest_id, FakeTsa(imprint, fail=one_down)
    )
    assert outcome.independent_tsas == 1 and len(outcome.errors) == 1
    assert "unreachable" in outcome.errors[0]
    with pytest.raises(mf.ManifestError, match="no TSA granted"):
        mf.timestamp_manifest(config, conn, built.manifest_id, FakeTsa(imprint, fail=urls))


def test_the_sole_permitted_capture_updates_are_one_shot(
    cap: Capture, config: Config, conn: sqlite3.Connection
) -> None:
    (capture_id,) = _add(cap, b"alpha")
    conn.execute(
        "UPDATE captures SET wayback_url = 'https://w/1' WHERE capture_id = ?", (capture_id,)
    )
    for sql in (
        "UPDATE captures SET wayback_url = 'https://w/2' WHERE capture_id = {id}",  # not twice
        "UPDATE captures SET wayback_url = NULL WHERE capture_id = {id}",  # not cleared
        "UPDATE captures SET content_sha256 = 'x' WHERE capture_id = {id}",
        "UPDATE captures SET http_status = 500 WHERE capture_id = {id}",
        "UPDATE captures SET tsa_token_path = 'a', url_final = 'b' WHERE capture_id = {id}",
        "DELETE FROM captures WHERE capture_id = {id}",
    ):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute(sql.format(id=capture_id))
    conn.execute("UPDATE captures SET tsa_token_path = 't.tsr' WHERE capture_id = ?", (capture_id,))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute(
            "UPDATE captures SET tsa_token_path = 'other' WHERE capture_id = ?", (capture_id,)
        )


# --- BagIt ----------------------------------------------------------------------------------


def test_the_bag_is_built_validated_updated_and_detects_a_modified_body(
    cap: Capture, config: Config
) -> None:
    _add(cap, b"alpha", b"bravo")
    created = mf.build_bag(config, now=WHEN)
    assert created.created and created.valid and created.files >= 2
    bag = bagit.Bag(str(created.path))
    assert set(bag.algorithms) >= {"sha256", "sha512"}
    assert (created.path / "manifest-sha256.txt").exists()
    assert (created.path / "manifest-sha512.txt").exists()
    _add(cap, b"charlie")
    updated = mf.build_bag(config, now=WHEN)
    assert not updated.created and updated.valid and updated.files == created.files + 1
    digest = hashlib.sha256(b"alpha").hexdigest()
    store_file = config.paths.store / digest[:2] / digest
    payload = created.path / "data" / digest[:2] / digest
    assert payload.stat().st_ino == store_file.stat().st_ino  # hard links: the store never moved
    payload.chmod(0o644)
    payload.write_bytes(b"corrupted")  # same inode, so the store's file changed too
    broken = mf.build_bag(config, now=WHEN)  # an update validates first, and refuses to bless it
    assert not broken.valid and "invalid" in broken.detail
    with pytest.raises(bagit.BagValidationError):
        bagit.Bag(str(created.path)).validate()  # the manifests were left alone


def test_bagging_needs_a_store(config: Config) -> None:
    with pytest.raises(mf.ManifestError, match="nothing to bag"):
        mf.build_bag(config)


# --- review follow-ups ------------------------------------------------------------------------


def test_a_token_for_other_data_is_not_stored(
    cap: Capture, config: Config, conn: sqlite3.Connection
) -> None:
    _add(cap, b"alpha")
    built = mf.build_manifest(config, conn, now=WHEN)
    assert built is not None
    with pytest.raises(mf.ManifestError, match="different data"):
        mf.timestamp_manifest(
            config, conn, built.manifest_id, FakeTsa(hashlib.sha256(b"x").digest())
        )
    assert conn.execute("SELECT count(*) FROM manifest_tokens").fetchone()[0] == 0
    assert (
        conn.execute("SELECT count(*) FROM captures WHERE tsa_token_path IS NOT NULL").fetchone()[0]
        == 0
    )


def test_a_capture_cannot_be_in_two_manifests_and_a_failed_build_leaves_nothing(
    cap: Capture, config: Config, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    (capture_id,) = _add(cap, b"alpha")
    built = mf.build_manifest(config, conn, now=WHEN)
    assert built is not None
    conn.execute(
        "INSERT INTO manifests (path, sha256, entry_count, created_at) VALUES ('x', 'y', 1, 't')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO manifest_captures (manifest_id, capture_id) VALUES (2, ?)", (capture_id,)
        )
    conn.rollback()
    # a build that fails after writing the file removes it and rolls back its rows
    _add(cap, b"bravo")
    real = mf._build_manifest

    def writes_then_fails(*args: object, **kwargs: object) -> None:
        real(*args, **kwargs)  # type: ignore[arg-type]
        raise RuntimeError("disk full")

    monkeypatch.setattr(mf, "_build_manifest", writes_then_fails)
    before = sorted(p.name for p in config.paths.manifests.glob("*.txt"))
    with pytest.raises(RuntimeError):
        mf.build_manifest(config, conn, now=WHEN)
    assert sorted(p.name for p in config.paths.manifests.glob("*.txt")) == before
    assert conn.execute("SELECT count(*) FROM manifests").fetchone()[0] == 1
    monkeypatch.undo()
    assert mf.build_manifest(config, conn, now=WHEN) is not None  # and it can simply be run again


def test_the_open_days_warc_is_left_out_of_the_bag_until_the_day_is_over(
    config: Config, conn: sqlite3.Connection
) -> None:
    import responses as responses_lib

    insert_row(conn, 1)
    conn.commit()
    day = datetime(2026, 11, 22, 10, 0, tzinfo=UTC)
    cap = make_capture(config, conn)
    cap._clock = lambda: day  # type: ignore[method-assign]
    with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.get("https://example.test/a", body=b"first")
        rsps.get("https://example.test/b", body=b"second")
        cap.fetch("https://example.test/a", row_id=1, stage="resolve_links")
        first = mf.build_bag(config, now=day)
        assert first.valid and not list((first.path / "data").rglob("*.warc.gz"))
        cap.fetch("https://example.test/b", row_id=1, stage="resolve_links")  # the WARC grows
        same_day = mf.build_bag(config, now=day)
    assert same_day.valid, same_day.detail  # growth of the open file does not break the bag
    next_day = mf.build_bag(config, now=day + timedelta(days=1))
    assert next_day.valid, next_day.detail
    bagged = list((next_day.path / "data").rglob("*.warc.gz"))
    assert len(bagged) == 1 and bagged[0].name == "2026-11-22.warc.gz"  # closed: now bagged
    cap.close()


def test_a_manifest_can_be_limited_to_an_iso_week(
    cap: Capture, config: Config, conn: sqlite3.Connection
) -> None:
    _add(cap, b"alpha")
    assert mf.build_manifest(config, conn, now=WHEN, week="2000-W01") is None  # nothing that week
    year, week, _ = datetime.now(UTC).isocalendar()
    built = mf.build_manifest(config, conn, now=WHEN, week=f"{year}-W{week:02d}")
    assert built is not None and built.iso_week == f"{year}-W{week:02d}"

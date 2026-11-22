"""Regression tests for the races that made the two-ingests test flaky.

Each one simulates the losing side of a race deterministically, so none of them depends on timing.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from efp import db as database
from efp.config import Config
from efp.ingest import loader
from efp.ingest import sample as sampling
from efp.ingest.loader import IngestError


class _FlakyWal:
    """A connection whose WAL switch is refused ``refusals`` times, like a concurrent switch."""

    def __init__(self, refusals: int) -> None:
        self.refusals = refusals
        self.switch_attempts = 0

    def execute(self, sql: str) -> _FlakyWal:
        if sql == "PRAGMA journal_mode":
            self._row = ("delete",)
        elif sql == "PRAGMA journal_mode = WAL":
            self.switch_attempts += 1
            if self.switch_attempts <= self.refusals:
                raise sqlite3.OperationalError("database is locked")
            self._row = ("wal",)
        return self

    def fetchone(self) -> tuple[str]:
        return self._row


def test_a_refused_wal_switch_is_retried_until_it_succeeds() -> None:
    fake = _FlakyWal(refusals=3)
    database._enable_wal(fake, 5000)  # type: ignore[arg-type]
    assert fake.switch_attempts == 4


def test_a_wal_switch_that_never_succeeds_still_raises_after_the_timeout() -> None:
    fake = _FlakyWal(refusals=10_000)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        database._enable_wal(fake, 50)  # type: ignore[arg-type]


def test_an_error_that_is_not_a_lock_is_not_retried() -> None:
    class Broken(_FlakyWal):
        def execute(self, sql: str) -> Broken:
            if sql.endswith("= WAL"):
                raise sqlite3.OperationalError("disk I/O error")
            return super().execute(sql)  # type: ignore[return-value]

    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        database._enable_wal(Broken(0), 5000)  # type: ignore[arg-type]


def test_a_data_directory_created_by_another_process_is_not_an_error(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = config.paths.database.parent
    parent.mkdir(parents=True)
    real_exists = Path.exists

    def stale_check(self: Path, *a: object, **k: object) -> bool:
        return False if self == parent else real_exists(self, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "exists", stale_check)  # the check said "missing", then it appeared
    conn = database.connect(config)
    conn.close()
    assert config.paths.database.is_file()


def _lose_the_sample_race(
    monkeypatch: pytest.MonkeyPatch, config: Config, *, winner_content_differs: bool
) -> None:
    """Make write_sample behave as if another ingest wrote the file a moment earlier."""
    path = sampling.resolve_sample_path(config)

    def lost(sample: sampling.Sample, target: Path) -> None:
        text = sampling.to_json(sample)
        target.write_text(text.replace('"seed"', '"seed_x"') if winner_content_differs else text)
        raise sampling.SampleError(f"{target} already exists; the sample is frozen")

    monkeypatch.setattr(sampling, "write_sample", lost)
    assert not path.exists()


def test_losing_the_sample_write_race_to_an_identical_sample_is_not_an_error(
    workbook: Path, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = loader.prepare(config, workbook)
    _lose_the_sample_race(monkeypatch, config, winner_content_differs=False)
    result = loader.ingest(config, prepared)
    assert result.status == "ingested" and result.sample_file == "verified"


def test_losing_the_sample_write_race_to_a_different_sample_is_still_refused(
    workbook: Path, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = loader.prepare(config, workbook)
    _lose_the_sample_race(monkeypatch, config, winner_content_differs=True)
    with pytest.raises(IngestError, match="could not be written"):
        loader.ingest(config, prepared)

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import openpyxl
import pytest

from efp import db as database
from efp.config import Config
from efp.export.roundtrip import RoundTripError, verify_round_trip
from efp.ingest import loader
from efp.ingest.loader import sha256_file
from tests.fixtures.make_workbook import ROWS_20, make_workbook, write_manifest


@pytest.fixture
def ingested(workbook: Path, config: Config) -> sqlite3.Connection:
    loader.ingest(config, loader.prepare(config, workbook))
    conn = database.connect(config, migrate=False, create=False)
    yield conn  # type: ignore[misc]
    conn.close()


def _snapshot(conn: sqlite3.Connection) -> list[tuple[object, ...]]:
    return conn.execute("SELECT * FROM rows ORDER BY row_id").fetchall()


def test_f1_passes_on_the_synthetic_workbook(
    ingested: sqlite3.Connection, config: Config, workbook: Path
) -> None:
    result = verify_round_trip(config, ingested, workbook)
    assert (result.rows_verified, result.rows_in_window, result.source_nonempty) == (20, 19, 18)
    assert result.source_hyperlinks == 0


def test_the_dry_run_leaves_no_file_and_no_database_change(
    ingested: sqlite3.Connection, config: Config, workbook: Path
) -> None:
    before = _snapshot(ingested)
    verify_round_trip(config, ingested, workbook)
    assert _snapshot(ingested) == before
    assert list(config.paths.exports.iterdir()) == []  # the temporary copy is gone


def test_the_original_file_is_never_touched(
    ingested: sqlite3.Connection, config: Config, workbook: Path
) -> None:
    before = sha256_file(workbook)
    verify_round_trip(config, ingested, workbook)
    assert sha256_file(workbook) == before


def test_a_changed_file_is_caught_before_anything_else(
    ingested: sqlite3.Connection, config: Config, workbook: Path, tmp_path: Path
) -> None:
    other = tmp_path / "changed.xlsx"
    shutil.copy(workbook, other)
    with other.open("ab") as handle:
        handle.write(b"x")
    with pytest.raises(RoundTripError, match="no longer hashes"):
        verify_round_trip(config, ingested, other)


def _tampered(workbook: Path, tmp_path: Path, cell: str, value: object) -> Path:
    path = tmp_path / "tampered.xlsx"
    wb = openpyxl.load_workbook(workbook)
    wb["Cases"][cell] = value
    wb.save(path)
    return path


def test_a_changed_cell_is_caught_at_row_level(
    ingested: sqlite3.Connection, config: Config, workbook: Path, tmp_path: Path
) -> None:
    path = _tampered(workbook, tmp_path, "F7", "Details were edited.")  # row_id 3
    with pytest.raises(RoundTripError, match=r"row_id 3 \(sheet row 7\).*no longer hash"):
        verify_round_trip(config, ingested, path, check_file_hash=False)


def test_a_text_cell_turned_into_a_number_is_caught(
    ingested: sqlite3.Connection, config: Config, workbook: Path, tmp_path: Path
) -> None:
    """The F1 trap: the year text '2016' (row 11) rewritten as the number 2016 must not pass."""
    path = _tampered(workbook, tmp_path, "A15", 2016)
    with pytest.raises(RoundTripError, match=r"row_id 11"):
        verify_round_trip(config, ingested, path, check_file_hash=False)


def test_a_blank_cell_filled_in_is_caught(
    ingested: sqlite3.Connection, config: Config, workbook: Path, tmp_path: Path
) -> None:
    path = _tampered(workbook, tmp_path, "G9", "https://example.test/added")  # row 5 had no Source
    with pytest.raises(RoundTripError, match="row_id 5"):
        verify_round_trip(config, ingested, path, check_file_hash=False)


def test_a_dropped_row_is_caught(
    ingested: sqlite3.Connection, config: Config, workbook: Path, tmp_path: Path
) -> None:
    path = tmp_path / "short.xlsx"
    make_workbook(path, rows=list(ROWS_20[:19]))
    with pytest.raises(RoundTripError, match="row count"):
        verify_round_trip(config, ingested, path, check_file_hash=False)


def test_nothing_ingested_is_reported(config: Config, workbook: Path) -> None:
    conn = database.connect(config)
    try:
        with pytest.raises(RoundTripError, match="nothing has been ingested"):
            verify_round_trip(config, conn, workbook)
    finally:
        conn.close()


def test_a_missing_workbook_is_reported(
    ingested: sqlite3.Connection, config: Config, tmp_path: Path
) -> None:
    with pytest.raises(RoundTripError, match="workbook not found"):
        verify_round_trip(config, ingested, tmp_path / "gone.xlsx")


def test_source_hyperlinks_are_counted(source_dir: Path, config: Config) -> None:
    path = source_dir / "links.xlsx"
    make_workbook(path)
    wb = openpyxl.load_workbook(path)
    ws = wb["Cases"]
    ws["G6"].hyperlink = "https://example.test/target-one"
    ws["G8"].hyperlink = "https://example.test/target-two"
    wb.save(path)
    write_manifest(path)
    loader.ingest(config, loader.prepare(config, path))
    conn = database.connect(config, migrate=False, create=False)
    try:
        assert verify_round_trip(config, conn, path).source_hyperlinks == 2
    finally:
        conn.close()


def test_openpyxl_keeps_the_dropdown_when_it_saves_a_copy(workbook: Path, tmp_path: Path) -> None:
    """Build 19 relies on this: the Ideological Motivation dropdown survives a load/save."""
    copy = tmp_path / "copy.xlsx"
    wb = openpyxl.load_workbook(workbook)
    wb.save(copy)
    validations = openpyxl.load_workbook(copy)["Cases"].data_validations.dataValidation
    assert len(validations) == 1
    assert "liberal" in str(validations[0].formula1)

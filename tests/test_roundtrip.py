from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import openpyxl
import pytest

from efp import db as database
from efp.config import Config
from efp.export.roundtrip import RoundTripError, Validations, count_validations, verify_round_trip
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
    assert (result.rows_verified, result.rows_in_window) == (20, 19)
    assert (result.source_nonempty, result.source_nonempty_in_window) == (18, 17)  # 1998 row out
    assert (result.source_with_url, result.source_with_url_in_window) == (18, 17)
    assert result.source_hyperlinks == 0
    assert (result.validations_original, result.validations_copy) == (
        Validations(1, 0),
        Validations(1, 0),
    )
    assert not result.validations_lost


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


@pytest.mark.filterwarnings("ignore:Data Validation extension:UserWarning")
def test_an_x14_dropdown_is_counted_and_its_loss_is_reported(
    source_dir: Path, config: Config
) -> None:
    """The real Heritage workbook stores its dropdown as an x14 extension, which openpyxl drops."""
    path = source_dir / "x14.xlsx"
    make_workbook(path, dropdown="x14")
    write_manifest(path)
    assert count_validations(path) == Validations(0, 1)
    loader.ingest(config, loader.prepare(config, path))
    conn = database.connect(config, migrate=False, create=False)
    try:
        result = verify_round_trip(config, conn, path)
    finally:
        conn.close()
    assert result.rows_verified == 20  # F1 itself still holds: the cell values are intact
    assert result.validations_original == Validations(0, 1)
    assert result.validations_copy == Validations(0, 0)
    assert result.validations_lost


def test_counting_validations_on_a_plain_workbook(workbook: Path, source_dir: Path) -> None:
    assert count_validations(workbook) == Validations(1, 0)
    bare = source_dir / "bare.xlsx"
    make_workbook(bare, dropdown="none")
    assert count_validations(bare) == Validations(0, 0)

"""F1, early: the nine source columns survive a round trip through openpyxl's writer unchanged.

This is the minimal export (build 19 completes it). It loads the original workbook with formatting
(not read-only), saves a copy, re-reads the copy and checks every row's nine cells against
``rows.row_sha256`` and the typed ``rows`` columns. The copy holds real names, so it is written
only under ``paths.exports`` and deleted afterwards; nothing leaves the data directory.

It does not write database values over the cells: ``rows`` stores a year as an integer, which cannot
reproduce an original text cell ``"2016"``. The hash, checked against the original file,
is the proof.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

import openpyxl

from efp.config import Config
from efp.ingest.loader import sha256_file
from efp.ingest.reader import HEADERS, WorkbookError, read_workbook


class RoundTripError(Exception):
    """F1 does not hold (or cannot be checked). ``problems`` lists what differs."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        shown = problems[:20]
        more = len(problems) - len(shown)
        tail = f"\n  ... and {more} more" if more > 0 else ""
        super().__init__("F1 round trip failed:\n  - " + "\n  - ".join(shown) + tail)


@dataclass(frozen=True)
class RoundTripResult:
    rows_verified: int
    rows_in_window: int
    source_nonempty: int
    source_hyperlinks: int


def _count_source_hyperlinks(workbook: Path, sheet_name: str, header_row: int, letter: str) -> int:
    wb = openpyxl.load_workbook(workbook)  # not read-only: hyperlinks are invisible there
    try:
        sheet = wb[sheet_name]
        last = sheet.max_row or header_row
        return sum(
            1
            for r in range(header_row + 1, last + 1)
            if sheet[f"{letter}{r}"].hyperlink is not None
        )
    finally:
        wb.close()


def verify_round_trip(
    config: Config,
    conn: sqlite3.Connection,
    workbook: Path,
    *,
    check_file_hash: bool = True,
) -> RoundTripResult:
    record = conn.execute(
        "SELECT sha256, sheet_name, header_row, data_rows FROM source_workbook"
    ).fetchone()
    if record is None:
        raise RoundTripError(["nothing has been ingested yet (no source_workbook record)"])
    sha, sheet_name, header_row, data_rows = record
    if not workbook.is_file():
        raise RoundTripError([f"workbook not found: {workbook}"])
    if check_file_hash and sha256_file(workbook) != sha:
        raise RoundTripError(["the workbook file no longer hashes to the hash recorded at ingest"])

    exports = config.paths.exports
    if not exports.exists():
        exports.mkdir(parents=True, mode=0o700)
        os.chmod(exports, 0o700)

    try:
        original = read_workbook(workbook)
    except WorkbookError as exc:
        raise RoundTripError([str(exc)]) from exc
    hyperlinks = _count_source_hyperlinks(
        workbook, original.sheet_name, original.header_row, original.column_letters[6]
    )

    with tempfile.TemporaryDirectory(dir=exports, prefix=".f1-") as scratch:
        copy = Path(scratch) / "roundtrip.xlsx"
        wb = openpyxl.load_workbook(workbook)  # keeps formatting and data validation
        try:
            wb.save(copy)
        finally:
            wb.close()
        try:
            again = read_workbook(copy)
        except WorkbookError as exc:
            raise RoundTripError([f"the copy cannot be read back: {exc}"]) from exc

    problems: list[str] = []
    if again.sheet_name != sheet_name or again.header_row != header_row:
        problems.append(
            f"sheet/header moved: recorded {sheet_name!r} row {header_row}, "
            f"copy has {again.sheet_name!r} row {again.header_row}"
        )
    if len(again.rows) != data_rows:
        problems.append(f"row count: recorded {data_rows}, copy has {len(again.rows)}")
    stored = {
        r[0]: r[1:]
        for r in conn.execute(
            "SELECT row_id, row_sha256, year, state, name, case_type, fraud_type, details, "
            "source_raw, ideological_motivation, ideological_source FROM rows"
        )
    }
    for row in again.rows:
        have = stored.get(row.row_id)
        if have is None:
            problems.append(f"row_id {row.row_id}: not in the database")
            continue
        if have[0] != row.sha256:
            problems.append(
                f"row_id {row.row_id} (sheet row {row.sheet_row}): the nine source cells no longer "
                "hash to rows.row_sha256"
            )
            continue
        for header, column_value, cell_value in zip(
            HEADERS, have[1:], (str(row.year), *row.values[1:]), strict=True
        ):
            if (None if column_value is None else str(column_value)) != cell_value:
                problems.append(f"row_id {row.row_id}: {header} differs from rows")
    if problems:
        raise RoundTripError(problems)

    in_window = conn.execute("SELECT count(*) FROM rows WHERE in_scope = 1").fetchone()[0]
    nonempty = conn.execute(
        "SELECT count(*) FROM rows WHERE source_raw IS NOT NULL AND trim(source_raw) != ''"
    ).fetchone()[0]
    return RoundTripResult(len(again.rows), in_window, nonempty, hyperlinks)

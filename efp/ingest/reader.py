"""Read the Heritage workbook: find the header by content, map the nine columns, validate.

The export does not start with the header row (a title block comes first), so the header is the
first row, within the first ``HEADER_SCAN_ROWS`` rows of a sheet, that contains ``Year``, ``State``
and ``Name``; never a fixed position. All nine expected headers must then be present.

Nothing is guessed. Every problem found (a missing column, a stray extra column, an unparseable
year, a blank row in the middle of the data, a reserved character in a cell) is collected and
reported together, and the caller writes nothing. The workbook is opened read-only and closed.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.utils import column_index_from_string, get_column_letter

from efp.rowhash import CellError, canonical_cell, row_sha256

# Source headers in logical order, with the rows column each maps to.
HEADERS: tuple[str, ...] = (
    "Year",
    "State",
    "Name",
    "Case Type",
    "Fraud Type",
    "Details",
    "Source",
    "Ideological Motivation",
    "Ideological Source",
)
COLUMNS: tuple[str, ...] = (
    "year",
    "state",
    "name",
    "case_type",
    "fraud_type",
    "details",
    "source_raw",
    "ideological_motivation",
    "ideological_source",
)
HEADER_KEYS = ("year", "state", "name")  # a row with these three is the header row
HEADER_SCAN_ROWS = 50
MAX_LISTED_PROBLEMS = 20
DUPLICATE = "\x00dup"  # suffix marking a repeated header in the header map
YEAR_MIN, YEAR_MAX = 1000, 9999


class WorkbookError(Exception):
    """The workbook cannot be ingested. ``problems`` lists everything found."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        shown = self.problems[:MAX_LISTED_PROBLEMS]
        more = len(self.problems) - len(shown)
        text = "\n  - ".join(shown)
        suffix = f"\n  ... and {more} more" if more > 0 else ""
        super().__init__(f"workbook cannot be ingested:\n  - {text}{suffix}")


@dataclass(frozen=True)
class SourceRow:
    """One data row. ``cells`` are the nine raw openpyxl values in logical column order."""

    row_id: int
    sheet_row: int
    cells: tuple[object, ...]
    sha256: str
    year: int
    values: tuple[str | None, ...]  # the nine ``rows`` column values (year as text)


@dataclass(frozen=True)
class WorkbookData:
    sheet_name: str
    header_row: int
    column_letters: tuple[str, ...]  # sheet column letter of each header, in logical order
    rows: list[SourceRow] = field(default_factory=list)


def normalise_header(value: object) -> str:
    """Compare headers ignoring case and runs of whitespace (the stored hash never does)."""
    return " ".join(value.split()).casefold() if isinstance(value, str) else ""


def plain_text(value: object) -> str | None:
    """The value as stored in a text column: the string itself, or None, or a plain rendering."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    return str(value)


def parse_year(value: object) -> int:
    """A Year cell as an integer: an int, an integral float, or a digit string."""
    if isinstance(value, bool) or value is None:
        raise ValueError("Year is missing or not a number")
    if isinstance(value, int):
        year = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"Year {value!r} is not a whole number")
        year = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not (text.isascii() and text.isdigit()):
            raise ValueError(f"Year {value!r} is not a number")
        year = int(text)
    else:
        raise ValueError(f"Year has unsupported type {type(value).__name__}")
    if not YEAR_MIN <= year <= YEAR_MAX:
        raise ValueError(f"Year {year} is not a four-digit year")
    return year


def _is_blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and value == "")


def _padded(row: Sequence[Any], width: int) -> tuple[object, ...]:
    return tuple(row) + (None,) * max(0, width - len(row))


def find_header(
    rows: Iterable[Sequence[Any]],
) -> tuple[int, dict[str, int]] | None:
    """Find the header: (1-based row number, normalised header -> column index), or None."""
    for number, row in enumerate(rows, start=1):
        if number > HEADER_SCAN_ROWS:
            break
        keys = {normalise_header(c) for c in row}
        if all(k in keys for k in HEADER_KEYS):
            mapping: dict[str, int] = {}
            for index, cell in enumerate(row):
                key = normalise_header(cell)
                if not key:
                    continue
                if key in mapping:
                    mapping[key + DUPLICATE] = index  # same header twice: reported, not guessed
                else:
                    mapping[key] = index
            return number, mapping
    return None


def _check_header(mapping: dict[str, int]) -> tuple[list[int], list[str]]:
    """Column indexes of the nine headers in logical order, and any problems with the header."""
    problems: list[str] = []
    expected = {normalise_header(h): h for h in HEADERS}
    missing = [h for k, h in expected.items() if k not in mapping]
    if missing:
        problems.append(f"missing expected column(s): {', '.join(missing)}")
    duplicated = [k.removesuffix(DUPLICATE) for k in mapping if k.endswith(DUPLICATE)]
    if duplicated:
        problems.append(f"column header(s) appear twice: {', '.join(sorted(set(duplicated)))}")
    extra = [
        get_column_letter(i + 1)
        for k, i in mapping.items()
        if k.removesuffix(DUPLICATE) not in expected
    ]
    if extra:
        problems.append(
            "unexpected non-empty header(s) in column(s) "
            f"{', '.join(sorted(extra))}: export appends new columns from J onward, so the "
            "workbook must contain only the nine source columns"
        )
    if problems:
        return [], problems
    return [mapping[normalise_header(h)] for h in HEADERS], []


def read_rows(rows: Iterable[Sequence[Any]], *, sheet_name: str) -> WorkbookData:
    """Validate and extract every data row from an iterable of sheet rows (row 1 first)."""
    all_rows = [tuple(r) for r in rows]
    found = find_header(all_rows)
    if found is None:
        raise WorkbookError(
            [
                f"sheet {sheet_name!r}: no header row with Year, State and Name found in the "
                f"first {HEADER_SCAN_ROWS} rows"
            ]
        )
    header_row, mapping = found
    indexes, problems = _check_header(mapping)
    if problems:
        raise WorkbookError(
            [f"sheet {sheet_name!r}, header row {header_row}: {p}" for p in problems]
        )
    letters = tuple(get_column_letter(i + 1) for i in indexes)
    known = set(indexes)

    data: list[SourceRow] = []
    problems = []
    pending_blank: list[int] = []
    for sheet_row, raw in enumerate(all_rows[header_row:], start=header_row + 1):
        width = max(len(raw), max(indexes) + 1)
        row = _padded(raw, width)
        cells = tuple(row[i] for i in indexes)
        stray = [
            get_column_letter(i + 1)
            for i, v in enumerate(row)
            if i not in known and not _is_blank(v)
        ]
        if stray:
            problems.append(
                f"row {sheet_row}: data in column(s) {', '.join(stray)} outside the nine "
                "source columns"
            )
        if all(_is_blank(c) for c in cells):
            pending_blank.append(sheet_row)
            continue
        if pending_blank:
            problems.append(
                f"blank row(s) {', '.join(map(str, pending_blank))} inside the data: skipping "
                "them would break the row_id to sheet-row alignment"
            )
            pending_blank = []
        row_id = len(data) + 1
        try:
            digest = row_sha256(cells)
        except CellError as exc:
            problems.append(f"row {sheet_row}: {exc}")
            data.append(SourceRow(row_id, sheet_row, cells, "", 0, ()))
            continue
        row_problems = _row_problems(cells)
        if row_problems:
            problems.extend(f"row {sheet_row}: {p}" for p in row_problems)
            data.append(SourceRow(row_id, sheet_row, cells, digest, 0, ()))
            continue
        year = parse_year(cells[0])
        values = (str(year), *(plain_text(c) for c in cells[1:]))
        data.append(SourceRow(row_id, sheet_row, cells, digest, year, values))
    # Anything left in pending_blank is trailing blank rows: ignored.
    if problems:
        raise WorkbookError(problems)
    if not data:
        raise WorkbookError([f"sheet {sheet_name!r}: no data rows below header row {header_row}"])
    return WorkbookData(sheet_name, header_row, letters, data)


def _row_problems(cells: tuple[object, ...]) -> list[str]:
    problems: list[str] = []
    try:
        parse_year(cells[0])
    except ValueError as exc:
        problems.append(str(exc))
    for position, label in ((1, "State"), (2, "Name")):
        value = cells[position]
        if value is None or (isinstance(value, str) and not value.strip()):
            problems.append(f"{label} is empty")
    for header, cell in zip(HEADERS, cells, strict=True):
        try:
            canonical_cell(cell)
        except CellError as exc:
            problems.append(f"{header}: {exc}")
    return problems


def _sheet_rows(worksheet: Any) -> Iterator[tuple[object, ...]]:
    reset = getattr(worksheet, "reset_dimensions", None)
    if callable(reset):
        reset()  # trust the cells, not the (sometimes wrong) <dimension> tag
    for row in worksheet.iter_rows(min_row=1, values_only=True):
        yield tuple(row)


def _formula_problems(
    path: Path, sheet_name: str, header_row: int, letters: Sequence[str]
) -> list[str]:
    """Rows whose nine source cells hold a formula.

    A formula's cached value is what ingest hashes, but openpyxl discards cached values when it
    saves, so such a row could never survive the F1 round trip. Refuse it rather than store a value
    (or NULL) that export cannot reproduce.
    """
    wanted = {column_index_from_string(letter) for letter in letters}
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=False)
    problems: list[str] = []
    try:
        for row in workbook[sheet_name].iter_rows(min_row=header_row + 1):
            for cell in row:
                column = getattr(cell, "column", None)
                if column in wanted and getattr(cell, "data_type", None) == "f":
                    problems.append(
                        f"row {cell.row}: column {get_column_letter(column)} holds a formula; "
                        "formulas cannot survive the F1 round trip (saving drops the cached value)"
                    )
    finally:
        workbook.close()
    return problems


def read_workbook(path: Path) -> WorkbookData:
    """Open ``path`` read-only, find the header and return every validated data row."""
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        candidates: list[tuple[str, list[tuple[object, ...]]]] = []
        for name in workbook.sheetnames:
            sheet = workbook[name]
            head = []
            for number, row in enumerate(_sheet_rows(sheet), start=1):
                head.append(row)
                if number >= HEADER_SCAN_ROWS:
                    break
            if find_header(head) is not None:
                candidates.append((name, head))
        if not candidates:
            raise WorkbookError(
                [
                    f"no sheet has a header row with Year, State and Name in its first "
                    f"{HEADER_SCAN_ROWS} rows (sheets: {', '.join(workbook.sheetnames)})"
                ]
            )
        if len(candidates) > 1:
            raise WorkbookError(
                [
                    "more than one sheet has a header row "
                    f"({', '.join(n for n, _ in candidates)}); expected exactly one"
                ]
            )
        name, head = candidates[0]
        rows = list(_sheet_rows(workbook[name]))
    finally:
        workbook.close()
    # Formulas first: a formula with no cached value would otherwise surface as "Name is empty".
    found = find_header(head)
    if found is not None:
        indexes, header_problems = _check_header(found[1])
        if not header_problems:
            letters = [get_column_letter(i + 1) for i in indexes]
            formulas = _formula_problems(path, name, found[0], letters)
            if formulas:
                raise WorkbookError(formulas)
    return read_rows(rows, sheet_name=name)

from __future__ import annotations

import datetime as dt
from pathlib import Path

import openpyxl
import pytest

from efp.ingest.reader import (
    HEADERS,
    WorkbookError,
    normalise_header,
    parse_year,
    plain_text,
    read_workbook,
)
from tests.fixtures.make_workbook import ROWS_20, make_workbook


def test_reads_the_synthetic_workbook(tmp_path: Path) -> None:
    path = tmp_path / "w.xlsx"
    make_workbook(path)
    data = read_workbook(path)
    assert (data.sheet_name, data.header_row) == ("Cases", 4)
    assert data.column_letters == tuple("ABCDEFGHI")
    assert [r.row_id for r in data.rows] == list(range(1, 21))
    assert [r.sheet_row for r in data.rows] == list(range(5, 25))  # header_row + row_id
    assert data.rows[0].year == 2004
    assert data.rows[0].values[1:4] == ("OH", "Alex Example", "Criminal Conviction")


def test_the_header_is_found_by_content_not_position(tmp_path: Path) -> None:
    for header_row in (1, 4, 7, 20):
        path = tmp_path / f"h{header_row}.xlsx"
        make_workbook(path, header_row=header_row)
        data = read_workbook(path)
        assert data.header_row == header_row
        assert len(data.rows) == 20


def test_header_matching_ignores_case_and_spacing_but_hashes_do_not(tmp_path: Path) -> None:
    clean, messy = tmp_path / "clean.xlsx", tmp_path / "messy.xlsx"
    make_workbook(clean)
    wb = openpyxl.load_workbook(clean)
    ws = wb["Cases"]
    for column, name in enumerate(HEADERS, start=1):
        ws.cell(row=4, column=column, value=f"  {name.upper().replace(' ', '   ')} ")
    wb.save(messy)
    assert [r.sha256 for r in read_workbook(messy).rows] == [
        r.sha256 for r in read_workbook(clean).rows
    ]
    assert normalise_header("  Case   Type ") == "case type"


def test_column_order_in_the_sheet_does_not_change_the_hash(tmp_path: Path) -> None:
    straight, shuffled = tmp_path / "a.xlsx", tmp_path / "b.xlsx"
    make_workbook(straight)
    order = [8, 2, 0, 5, 1, 7, 3, 6, 4]  # a permutation of the nine columns
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Cases"
    for column, source in enumerate(order, start=1):
        ws.cell(row=4, column=column, value=HEADERS[source])
        for r, row in enumerate(ROWS_20, start=5):
            ws.cell(row=r, column=column, value=row[source])
    wb.save(shuffled)
    a, b = read_workbook(straight), read_workbook(shuffled)
    assert [r.sha256 for r in a.rows] == [r.sha256 for r in b.rows]
    assert b.column_letters != a.column_letters


def test_a_missing_column_fails_naming_it(tmp_path: Path) -> None:
    for header in ("Fraud Type", "Ideological Source", "Source"):
        path = tmp_path / "m.xlsx"
        make_workbook(path, drop_header=header)
        with pytest.raises(WorkbookError, match=f"missing expected column.*{header}"):
            read_workbook(path)


def test_an_extra_non_empty_header_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "x.xlsx"
    make_workbook(path, extra_header="Notes")
    with pytest.raises(WorkbookError, match=r"unexpected non-empty header.*\bJ\b"):
        read_workbook(path)


def test_a_repeated_header_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "d.xlsx"
    make_workbook(path, extra_header="Year")
    with pytest.raises(WorkbookError, match="appear twice"):
        read_workbook(path)


def test_no_header_anywhere_fails_loudly(tmp_path: Path) -> None:
    path = tmp_path / "n.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.append(["just", "a", "title"])
    ws.append([1, 2, 3])
    wb.save(path)
    with pytest.raises(WorkbookError, match="no sheet has a header row"):
        read_workbook(path)


def test_two_sheets_with_headers_are_ambiguous(tmp_path: Path) -> None:
    path = tmp_path / "two.xlsx"
    make_workbook(path)
    wb = openpyxl.load_workbook(path)
    second = wb.create_sheet("Copy")
    second.append(list(HEADERS))
    wb.save(path)
    with pytest.raises(WorkbookError, match="more than one sheet"):
        read_workbook(path)


def test_an_unrelated_extra_sheet_is_fine(tmp_path: Path) -> None:
    path = tmp_path / "notes.xlsx"
    make_workbook(path)
    wb = openpyxl.load_workbook(path)
    wb.create_sheet("Notes").append(["nothing", "to see"])
    wb.save(path)
    assert read_workbook(path).sheet_name == "Cases"


def test_trailing_blank_rows_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "t.xlsx"
    make_workbook(path, trailing_blank_rows=5)
    assert len(read_workbook(path).rows) == 20


def test_a_blank_row_inside_the_data_is_refused_with_its_sheet_row(tmp_path: Path) -> None:
    path = tmp_path / "b.xlsx"
    make_workbook(path, blank_row_after=3)
    with pytest.raises(WorkbookError, match=r"blank row\(s\) 8 inside the data"):
        read_workbook(path)


def test_every_problem_is_reported_together(tmp_path: Path) -> None:
    rows = [list(r) for r in ROWS_20[:6]]
    rows[0][0] = "abc"  # unparseable year
    rows[1][0] = None  # missing year
    rows[2][0] = 2016.5  # not whole
    rows[3][2] = ""  # empty name
    rows[4][1] = None  # empty state
    rows[5][0] = True  # a boolean is not a year
    path = tmp_path / "bad.xlsx"
    make_workbook(path, rows=[tuple(r) for r in rows])
    with pytest.raises(WorkbookError) as err:
        read_workbook(path)
    problems = " | ".join(err.value.problems)
    for needle in ("row 5", "row 6", "row 7", "row 8", "row 9", "row 10"):
        assert needle in problems
    assert len(err.value.problems) >= 6


def test_data_outside_the_nine_columns_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "stray.xlsx"
    make_workbook(path)
    wb = openpyxl.load_workbook(path)
    wb["Cases"]["K9"] = "stray note"
    wb.save(path)
    with pytest.raises(WorkbookError, match=r"row 9: data in column\(s\) K"):
        read_workbook(path)


def test_an_empty_data_section_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "empty.xlsx"
    make_workbook(path, rows=[])
    with pytest.raises(WorkbookError, match="no data rows"):
        read_workbook(path)


@pytest.mark.parametrize(
    ("cell", "year"),
    [(2016, 2016), (2016.0, 2016), ("2016", 2016), (" 2016 ", 2016), (1998, 1998), (2026, 2026)],
)
def test_year_parsing_accepts(cell: object, year: int) -> None:
    assert parse_year(cell) == year


@pytest.mark.parametrize(
    "cell", [None, True, 2016.5, "abc", "", "20 16", 99, 12345, dt.date(2016, 1, 1), [2016]]
)
def test_year_parsing_rejects(cell: object) -> None:
    with pytest.raises(ValueError):
        parse_year(cell)


def test_stored_values_are_verbatim(tmp_path: Path) -> None:
    path = tmp_path / "v.xlsx"
    make_workbook(path)
    rows = read_workbook(path).rows
    assert rows[17].values[5] == "Voted twice. "  # the trailing space survives
    assert rows[4].values[6] is None  # an empty Source stays NULL
    assert rows[10].year == 2016 and rows[10].cells[0] == "2016"  # text year: hash keeps the type
    assert rows[4].values[2] == "Jordan Q. Sample aka J. Placeholder"


def test_a_text_year_and_a_numeric_year_hash_differently(tmp_path: Path) -> None:
    numeric = [list(r) for r in ROWS_20[:3]]
    textual = [list(r) for r in ROWS_20[:3]]
    textual[0][0] = str(numeric[0][0])
    a, b = tmp_path / "a.xlsx", tmp_path / "b.xlsx"
    make_workbook(a, rows=[tuple(r) for r in numeric])
    make_workbook(b, rows=[tuple(r) for r in textual])
    ra, rb = read_workbook(a).rows, read_workbook(b).rows
    assert ra[0].year == rb[0].year
    assert ra[0].sha256 != rb[0].sha256
    assert ra[1].sha256 == rb[1].sha256


def test_plain_text_renderings() -> None:
    assert plain_text(None) is None
    assert plain_text("x") == "x"
    assert plain_text(5) == "5"
    assert plain_text(dt.date(2020, 1, 2)) == "2020-01-02"

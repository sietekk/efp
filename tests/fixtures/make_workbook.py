"""Synthetic Heritage-style workbooks for tests. Every value is invented; nothing is derived from
real rows.

Layout: a title block, the header on row 4 (movable), the nine source columns, data below. The 20
default rows cover what ingest must handle: blank cells, an ``aka`` name, one year outside the
2000-2025 window (1998), one year stored as text, a trailing space, a federal marker in the
details, and a data-validation dropdown on Ideological Motivation (which must survive a round
trip). ``make_workbook(n_rows=...)`` makes a larger seeded variant for sampling tests.
"""

from __future__ import annotations

import hashlib
import random
from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.worksheet.datavalidation import DataValidation

HEADERS = (
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

Row = tuple[Any, ...]

ROWS_20: list[Row] = [
    (
        2004,
        "OH",
        "Alex Example",
        "Criminal Conviction",
        "Duplicate Voting",
        "Voted in two precincts.",
        "https://example.test/oh/1",
        "liberal",
        "Synthetic note A",
    ),
    (
        2006,
        "TX",
        "Blair Sample",
        "Criminal Conviction",
        "Ineligible Voting",
        "Noncitizen registered. Prosecuted by the U.S. Attorney's Office.",
        "https://example.test/usao/2",
        None,
        None,
    ),
    (
        2009,
        "FL",
        "Casey Placeholder",
        "Diversion Program",
        "False Registration",
        "Listed a false address.",
        "https://example.test/fl/3",
        "independent",
        None,
    ),
    (
        2010,
        "GA",
        "Devon Invented",
        "Criminal Conviction",
        "Fraudulent Use Of Absentee Ballots",
        "Filled out ballots for relatives.",
        "https://example.test/ga/4",
        "conservative",
        "Synthetic note D",
    ),
    (
        2011,
        "NC",
        "Jordan Q. Sample aka J. Placeholder",
        "Criminal Conviction",
        "Duplicate Voting",
        "Alias used on one registration.",
        None,
        None,
        None,
    ),
    (
        2012,
        "PA",
        "Kai Fictional",
        "Civil Penalty",
        "Ineligible Voting",
        "Felon voted while on probation.",
        "https://example.test/pa/6",
        "unknown",
        None,
    ),
    (
        2013,
        "WI",
        "Lee Madeup",
        "Criminal Conviction",
        "Vote Buying",
        "Offered rides for votes.",
        "https://example.test/wi/7",
        None,
        None,
    ),
    (
        2014,
        "AZ",
        "Morgan Notreal",
        "Official Finding",
        "Illegal Assistance",
        "Assisted voters improperly.",
        "https://example.test/az/8",
        "liberal",
        None,
    ),
    (
        2015,
        "CA",
        "Noel Pretend",
        "Criminal Conviction",
        "Fraud/Misc",
        "Misc case; DOJ press release.",
        "https://example.test/usao-example/9",
        None,
        None,
    ),
    (
        2016,
        "MI",
        "Oakley Imaginary",
        "Criminal Conviction",
        "Duplicate Voting",
        "Voted in two states.",
        "https://example.test/mi/10",
        "conservative",
        None,
    ),
    (
        "2016",
        "NY",
        "Parker Fabricated",
        "Criminal Conviction",
        "Ineligible Voting",
        "Year stored as text on purpose.",
        "https://example.test/ny/11",
        None,
        None,
    ),
    (
        2017,
        "TX",
        "Quinn Synthetic",
        "Criminal Conviction",
        "Ineligible Voting",
        "Noncitizen voting; U.S. District Court sentencing.",
        "https://example.test/tx/12",
        "independent",
        None,
    ),
    (
        2018,
        "FL",
        "Reese Fakename",
        "Diversion Program",
        "Duplicate Voting",
        "Voted in FL and NY.",
        "https://example.test/fl/13",
        None,
        None,
    ),
    (
        2019,
        "OH",
        "Sage Testcase",
        "Criminal Conviction",
        "False Registration",
        "Falsified registration forms.",
        None,
        "liberal",
        "Synthetic note N",
    ),
    (
        2020,
        "PA",
        "Taylor Demo",
        "Criminal Conviction",
        "Fraudulent Use Of Absentee Ballots",
        "Mailed ballots for others.",
        "https://example.test/pa/15",
        "conservative",
        None,
    ),
    (
        2021,
        "GA",
        "Uma Invented",
        "Criminal Conviction",
        "Vote Buying",
        "Paid voters.",
        "https://example.test/ga/16",
        None,
        None,
    ),
    (
        2022,
        "AZ",
        "Vic Sample",
        "Civil Penalty",
        "Illegal Assistance",
        None,
        "https://example.test/az/17",
        "unknown",
        None,
    ),
    (
        2023,
        "WI",
        "Wren Example",
        "Criminal Conviction",
        "Duplicate Voting",
        "Voted twice. ",
        "https://example.test/wi/18",
        None,
        None,
    ),
    (
        2025,
        "NC",
        "Xan Placeholder",
        "Criminal Conviction",
        "Ineligible Voting",
        "Felon voted.",
        "https://example.test/nc/19",
        "independent",
        None,
    ),
    (
        1998,
        "OH",
        "Yael Outside",
        "Criminal Conviction",
        "Duplicate Voting",
        "Outside the window.",
        "https://example.test/oh/20",
        None,
        None,
    ),
]

_STATES = ("OH", "TX", "FL", "GA", "NC", "PA", "WI", "AZ", "CA", "MI", "NY", "TN")
_FRAUD = (
    "Duplicate Voting",
    "Ineligible Voting",
    "False Registration",
    "Fraudulent Use Of Absentee Ballots",
    "Vote Buying",
    "Illegal Assistance",
    "Fraud/Misc",
    "Impersonation",
)
_CASE = ("Criminal Conviction", "Diversion Program", "Civil Penalty", "Official Finding")


def bulk_rows(n: int, seed: int) -> list[Row]:
    """``n`` invented rows, reproducible from ``seed``."""
    rng = random.Random(seed)  # noqa: S311  (test data, not crypto)
    rows: list[Row] = []
    for i in range(1, n + 1):
        federal = rng.random() < 0.3
        details = f"Synthetic case {i}." + (" Prosecuted by the U.S. Attorney." if federal else "")
        source = f"https://example.test/case/{i}" if rng.random() < 0.9 else None
        rows.append(
            (
                rng.randint(2000, 2025),
                rng.choice(_STATES),
                f"Person{i:04d} Example",
                rng.choices(_CASE, weights=(6, 1, 1, 1))[0],
                rng.choices(_FRAUD, weights=(5, 4, 3, 3, 2, 2, 1, 1))[0],  # a skewed mix
                details,
                source,
                rng.choice(("liberal", "conservative", "independent", "unknown", None)),
                None,
            )
        )
    return rows


def make_workbook(
    path: Path,
    *,
    rows: list[Row] | None = None,
    header_row: int = 4,
    n_rows: int | None = None,
    seed: int = 1,
    trailing_blank_rows: int = 0,
    blank_row_after: int | None = None,
    extra_header: str | None = None,
    drop_header: str | None = None,
    sheet_name: str = "Cases",
) -> list[Row]:
    """Write a workbook to ``path`` and return the data rows written (blank rows excluded)."""
    data = rows if rows is not None else (bulk_rows(n_rows, seed) if n_rows else list(ROWS_20))
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = sheet_name
    for r in range(1, header_row):  # the title block
        ws.cell(
            row=r,
            column=1,
            value="Synthetic Election Fraud Export (invented data)"
            if r == 1
            else ("Generated for tests; no real subjects." if r == 2 else None),
        )
    headers = [h for h in HEADERS if h != drop_header]
    for c, name in enumerate(headers, start=1):
        ws.cell(row=header_row, column=c, value=name)
    if extra_header:
        ws.cell(row=header_row, column=len(headers) + 1, value=extra_header)
    sheet_row = header_row + 1
    for index, row in enumerate(data, start=1):
        cells = [v for h, v in zip(HEADERS, row, strict=True) if h != drop_header]
        for c, value in enumerate(cells, start=1):
            ws.cell(row=sheet_row, column=c, value=value)
        sheet_row += 1
        if blank_row_after == index:
            sheet_row += 1  # leave one blank row inside the data
    for _ in range(trailing_blank_rows):
        ws.cell(row=sheet_row, column=1, value=None)
        sheet_row += 1
    if drop_header != "Ideological Motivation":
        motivation_col = headers.index("Ideological Motivation") + 1
        dropdown = DataValidation(
            type="list", formula1='"liberal,conservative,independent,unknown"', allow_blank=True
        )
        ws.add_data_validation(dropdown)
        letter = openpyxl.utils.get_column_letter(motivation_col)
        dropdown.add(f"{letter}{header_row + 1}:{letter}{header_row + len(data) + 5}")
    wb.save(path)
    return data


def write_manifest(workbook: Path) -> str:
    """Write ``MANIFEST.txt`` beside the workbook in ``shasum -a 256`` format; return the hash."""
    digest = hashlib.sha256(workbook.read_bytes()).hexdigest()
    (workbook.parent / "MANIFEST.txt").write_text(f"{digest}  {workbook.name}\n", encoding="utf-8")
    return digest

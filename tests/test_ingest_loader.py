from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import openpyxl
import pytest

from efp import db as database
from efp.config import Config, load_config
from efp.ingest import loader
from efp.ingest.loader import IngestError, IngestRefused, parse_manifest, sha256_file
from efp.scope import in_window, population_sql
from tests.fixtures.make_workbook import ROWS_20, make_workbook, write_manifest


def _count(config: Config, table: str) -> int:
    conn = database.connect(config, migrate=False, create=False)
    try:
        return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


# ---------------------------------------------------------------- manifest


def test_manifest_matches(workbook: Path) -> None:
    assert loader.check_manifest(workbook) == sha256_file(workbook)


def test_manifest_mismatch_aborts_and_prints_both_hashes(workbook: Path) -> None:
    manifest = workbook.parent / "MANIFEST.txt"
    wrong = "0" * 64
    manifest.write_text(f"{wrong}  {workbook.name}\n", encoding="utf-8")
    with pytest.raises(IngestError) as err:
        loader.check_manifest(workbook)
    assert wrong in str(err.value) and sha256_file(workbook) in str(err.value)


def test_a_workbook_edited_after_the_manifest_is_caught(workbook: Path) -> None:
    with workbook.open("ab") as handle:
        handle.write(b"x")
    with pytest.raises(IngestError, match=r"does not match MANIFEST\.txt"):
        loader.check_manifest(workbook)


def test_a_missing_manifest_aborts_with_the_command_to_make_one(workbook: Path) -> None:
    (workbook.parent / "MANIFEST.txt").unlink()
    with pytest.raises(IngestError, match=r"MANIFEST\.txt not found.*shasum -a 256"):
        loader.check_manifest(workbook)


def test_a_manifest_without_an_entry_aborts(workbook: Path) -> None:
    (workbook.parent / "MANIFEST.txt").write_text(f"{'a' * 64}  other.xlsx\n", encoding="utf-8")
    with pytest.raises(IngestError, match="no entry for"):
        loader.check_manifest(workbook)


def test_a_missing_workbook_aborts(tmp_path: Path) -> None:
    with pytest.raises(IngestError, match="workbook not found"):
        loader.check_manifest(tmp_path / "nope.xlsx")


def test_manifest_parsing_accepts_shasum_formats() -> None:
    a, b = "a" * 64, "B" * 64
    entries = parse_manifest(f"{a}  one.xlsx\n\n{b} *two.xlsx\n{a}  /some/dir/three.xlsx\n")
    assert entries == {"one.xlsx": a, "two.xlsx": b.lower(), "three.xlsx": a}


def test_manifest_parsing_rejects_junk_and_conflicts() -> None:
    with pytest.raises(IngestError, match="line 1"):
        parse_manifest("not a hash line\n")
    with pytest.raises(IngestError, match="twice with different hashes"):
        parse_manifest(f"{'a' * 64}  x.xlsx\n{'b' * 64}  x.xlsx\n")


# ---------------------------------------------------------------- prepare / summary


def test_prepare_reports_counts_without_writing(
    workbook: Path, config: Config, data_dir: Path
) -> None:
    prepared = loader.prepare(config, workbook)
    summary = prepared.summary
    assert (summary.total_rows, summary.in_window, summary.source_nonempty) == (20, 19, 18)
    assert summary.by_band_and_case_type[("before 2000", "Criminal Conviction")] == 1
    assert summary.by_band_and_case_type[("2000-09", "Criminal Conviction")] == 2
    assert sum(summary.by_band_and_case_type.values()) == 20
    assert not data_dir.exists()


def test_prepare_rejects_a_stage_the_schema_does_not_allow(
    write_config: Callable[..., Path], workbook: Path, data_dir: Path
) -> None:
    config = load_config(
        write_config({'stages = ["resolve_links", "court"]': 'stages = ["resolve_links", "bogus"]'})
    )
    with pytest.raises(IngestError, match="bogus"):
        loader.prepare(config, workbook)
    assert not data_dir.exists()


# ---------------------------------------------------------------- ingest


def test_ingest_loads_every_row_and_seeds_every_stage(workbook: Path, config: Config) -> None:
    result = loader.ingest(config, loader.prepare(config, workbook))
    assert result.status == "ingested"
    conn = database.connect(config, migrate=False, create=False)
    try:
        assert conn.execute("SELECT count(*) FROM rows").fetchone()[0] == 20
        assert [r[0] for r in conn.execute("SELECT row_id FROM rows ORDER BY row_id")] == list(
            range(1, 21)
        )
        assert conn.execute("SELECT count(*) FROM case_stage").fetchone()[0] == 40
        assert conn.execute("SELECT DISTINCT status FROM case_stage").fetchall() == [("pending",)]
        assert {r[0] for r in conn.execute("SELECT DISTINCT stage FROM case_stage")} == {
            "resolve_links",
            "court",
        }
        record = conn.execute(
            "SELECT sha256, filename, sheet_name, header_row, data_rows, year_from, year_to FROM source_workbook"
        ).fetchone()
        assert record == (sha256_file(workbook), workbook.name, "Cases", 4, 20, 2000, 2025)
    finally:
        conn.close()


def test_stored_values_match_the_source(workbook: Path, config: Config) -> None:
    prepared = loader.prepare(config, workbook)
    loader.ingest(config, prepared)
    conn = database.connect(config, migrate=False, create=False)
    try:
        rows = conn.execute(
            "SELECT row_id, year, state, name, case_type, fraud_type, details, source_raw, "
            "ideological_motivation, ideological_source, row_sha256 FROM rows ORDER BY row_id"
        ).fetchall()
    finally:
        conn.close()
    for stored, source, row in zip(rows, ROWS_20, prepared.data.rows, strict=True):
        assert stored[0] == row.row_id
        assert stored[1] == int(source[0])
        assert stored[2:10] == tuple(
            source[1:]
        )  # None stays NULL; text verbatim (trailing space too)
        assert stored[10] == row.sha256


@pytest.mark.parametrize(
    ("years", "flags"),
    [((1999, 2000, 2025, 2026), (0, 1, 1, 0)), (("2000", 2025.0, 1900, 9999), (1, 1, 0, 0))],
)
def test_in_scope_is_exactly_the_year_window(
    years: tuple[object, ...], flags: tuple[int, ...], source_dir: Path, config: Config
) -> None:
    rows = [
        (y, "OH", f"Person {i}", "Criminal Conviction", "Duplicate Voting", None, None, None, None)
        for i, y in enumerate(years)
    ]
    path = source_dir / "years.xlsx"
    make_workbook(path, rows=rows)
    write_manifest(path)
    loader.ingest(config, loader.prepare(config, path))
    conn = database.connect(config, migrate=False, create=False)
    try:
        assert (
            tuple(r[0] for r in conn.execute("SELECT in_scope FROM rows ORDER BY row_id")) == flags
        )
    finally:
        conn.close()


def test_the_window_comes_from_config_and_is_recorded(
    write_config: Callable[..., Path], workbook: Path
) -> None:
    config = load_config(
        write_config({"year_from = 2000": "year_from = 2010", "year_to = 2025": "year_to = 2020"})
    )
    loader.ingest(config, loader.prepare(config, workbook))
    conn = database.connect(config, migrate=False, create=False)
    try:
        expected = sum(2010 <= int(r[0]) <= 2020 for r in ROWS_20)
        assert (
            conn.execute("SELECT count(*) FROM rows WHERE in_scope = 1").fetchone()[0] == expected
        )
        assert conn.execute("SELECT year_from, year_to FROM source_workbook").fetchone() == (
            2010,
            2020,
        )
    finally:
        conn.close()


def test_in_window_helper(config: Config) -> None:
    assert [in_window(y, config) for y in (1999, 2000, 2025, 2026)] == [False, True, True, False]


def test_re_ingesting_the_same_workbook_is_a_no_op(workbook: Path, config: Config) -> None:
    loader.ingest(config, loader.prepare(config, workbook))
    before = (
        _count(config, "rows"),
        _count(config, "case_stage"),
        _count(config, "source_workbook"),
    )
    again = loader.ingest(config, loader.prepare(config, workbook))
    assert again.status == "noop" and again.notes == []
    assert before == (
        _count(config, "rows"),
        _count(config, "case_stage"),
        _count(config, "source_workbook"),
    )


def test_a_modified_workbook_is_refused_and_nothing_changes(
    workbook: Path, config: Config, source_dir: Path
) -> None:
    loader.ingest(config, loader.prepare(config, workbook))
    changed = source_dir / "changed.xlsx"
    rows = [list(r) for r in ROWS_20]
    rows[2][2] = "Casey Placeholder (edited)"
    make_workbook(changed, rows=[tuple(r) for r in rows])
    write_manifest(changed)
    with pytest.raises(IngestRefused, match=r"row hash\(es\) differ, first row_id\(s\): \[3\]"):
        loader.ingest(config, loader.prepare(config, changed))
    conn = database.connect(config, migrate=False, create=False)
    try:
        assert conn.execute("SELECT name FROM rows WHERE row_id = 3").fetchone() == (
            "Casey Placeholder",
        )
    finally:
        conn.close()


def test_a_workbook_with_more_rows_is_refused(
    workbook: Path, config: Config, source_dir: Path
) -> None:
    loader.ingest(config, loader.prepare(config, workbook))
    bigger = source_dir / "bigger.xlsx"
    make_workbook(
        bigger,
        rows=[
            *ROWS_20,
            (
                2020,
                "OH",
                "Zed Extra",
                "Criminal Conviction",
                "Duplicate Voting",
                None,
                None,
                None,
                None,
            ),
        ],
    )
    write_manifest(bigger)
    with pytest.raises(IngestRefused, match="row count"):
        loader.ingest(config, loader.prepare(config, bigger))


def test_a_different_year_window_is_refused(
    write_config: Callable[..., Path], workbook: Path, config: Config
) -> None:
    loader.ingest(config, loader.prepare(config, workbook))
    other = load_config(write_config({"year_from = 2000": "year_from = 2005"}))
    with pytest.raises(IngestRefused, match="year_from"):
        loader.ingest(other, loader.prepare(other, workbook))


def test_a_workbook_hash_change_with_identical_rows_is_still_refused(
    workbook: Path, config: Config, source_dir: Path
) -> None:
    """Same cells, different file (an extra sheet): the recorded workbook hash must match."""
    loader.ingest(config, loader.prepare(config, workbook))
    other = source_dir / "other.xlsx"
    make_workbook(other)
    wb = openpyxl.load_workbook(other)
    wb.create_sheet("Notes")
    wb.save(other)
    write_manifest(other)
    with pytest.raises(IngestRefused, match="workbook hash"):
        loader.ingest(config, loader.prepare(config, other))


def test_ingest_is_atomic(workbook: Path, config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = loader.prepare(config, workbook)
    monkeypatch.setattr(loader, "INSERT_WORKBOOK", "INSERT INTO nowhere VALUES (1)")
    with pytest.raises(sqlite3.Error):
        loader.ingest(config, prepared)
    for table in ("rows", "case_stage", "source_workbook"):
        assert _count(config, table) == 0, table
    monkeypatch.undo()
    assert loader.ingest(config, prepared).status == "ingested"  # the retry succeeds


def test_a_bad_workbook_writes_nothing_at_all(
    source_dir: Path, config: Config, data_dir: Path
) -> None:
    path = source_dir / "bad.xlsx"
    make_workbook(path, rows=[(None, "OH", "A Person", None, None, None, None, None, None)])
    write_manifest(path)
    with pytest.raises(IngestError, match="Year is missing"):
        loader.prepare(config, path)
    assert not data_dir.exists()


def test_check_only_writes_nothing(
    workbook: Path, config: Config, data_dir: Path, tmp_path: Path
) -> None:
    result = loader.check_only(config, workbook)
    assert result.status == "checked" and result.sample_file == "would write"
    assert not data_dir.exists()
    assert not (tmp_path / "sample_rows.json").exists()


def test_the_immutable_tables_refuse_changes_after_ingest(workbook: Path, config: Config) -> None:
    loader.ingest(config, loader.prepare(config, workbook))
    conn = database.connect(config, migrate=False, create=False)
    try:
        for sql in (
            "UPDATE rows SET name = 'x' WHERE row_id = 1",
            "DELETE FROM rows",
            "UPDATE source_workbook SET sha256 = 'x'",
            "DELETE FROM source_workbook",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(sql)
    finally:
        conn.close()


# ---------------------------------------------------------------- population (decision 2)


def test_population_sql(workbook: Path, config: Config) -> None:
    loader.ingest(config, loader.prepare(config, workbook))
    conn = database.connect(config, migrate=False, create=False)
    try:
        everyone = conn.execute(
            f"SELECT count(*) FROM rows r WHERE {population_sql('all')}"
        ).fetchone()[0]
        convictions = conn.execute(
            f"SELECT count(*) FROM rows r WHERE {population_sql('convictions')}"
        ).fetchone()[0]
    finally:
        conn.close()
    assert everyone == 19  # in scope only; the 1998 row is stored but excluded
    expected = sum(
        1
        for r in ROWS_20
        if 2000 <= int(r[0]) <= 2025 and str(r[3]).startswith("Criminal Conviction")
    )
    assert convictions == expected
    assert 0 < convictions < everyone  # the population rule really narrows the set
    with pytest.raises(ValueError, match="unknown population"):
        population_sql("everything")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="identifier"):
        population_sql("all", alias="r; DROP TABLE rows")


def test_a_no_op_run_notes_when_runner_stages_changed_but_changes_nothing(
    write_config: Callable[..., Path], workbook: Path
) -> None:
    first = load_config(write_config())
    loader.ingest(first, loader.prepare(first, workbook))
    wider = load_config(
        write_config(
            {'stages = ["resolve_links", "court"]': 'stages = ["resolve_links", "court", "news"]'}
        )
    )
    result = loader.ingest(wider, loader.prepare(wider, workbook))
    assert result.status == "noop"
    assert any("case_stage has 40" in n and "implies 60" in n for n in result.notes)
    assert _count(wider, "case_stage") == 40  # ingest never alters an existing database

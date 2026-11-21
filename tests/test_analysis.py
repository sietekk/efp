from __future__ import annotations

import sqlite3
from pathlib import Path

import duckdb
import pytest

from efp.analysis import AnalysisError, open_readonly
from efp.config import Config
from tests.dbhelpers import insert_row


def _duckdb_has_sqlite() -> bool:
    probe = duckdb.connect(":memory:")
    try:
        probe.execute("LOAD sqlite")
    except duckdb.Error:
        return False
    finally:
        probe.close()
    return True


needs_extension = pytest.mark.skipif(
    not _duckdb_has_sqlite(),
    reason="DuckDB sqlite extension is not installed (never auto-installed)",
)


def test_missing_database_is_an_error(config: Config, data_dir: Path) -> None:
    with pytest.raises(AnalysisError, match="not found"):
        open_readonly(config)
    assert not data_dir.exists()


@needs_extension
def test_read_only_attach_reads_the_pipeline_database(
    conn: sqlite3.Connection, config: Config
) -> None:
    insert_row(conn)
    conn.commit()
    handle = open_readonly(config)
    try:
        assert handle.execute("SELECT name FROM pipeline.rows").fetchall() == [
            ("Synthetic Person",)
        ]
    finally:
        handle.close()


@needs_extension
def test_the_handle_cannot_write(conn: sqlite3.Connection, config: Config) -> None:
    insert_row(conn)
    conn.commit()
    handle = open_readonly(config)
    try:
        with pytest.raises(duckdb.Error):
            handle.execute("DELETE FROM pipeline.rows")
        with pytest.raises(duckdb.Error):
            handle.execute("INSERT INTO pipeline.case_stage (row_id, stage) VALUES (1, 'court')")
    finally:
        handle.close()
    assert conn.execute("SELECT count(*) FROM rows").fetchone() == (1,)
    assert conn.execute("SELECT count(*) FROM case_stage").fetchone() == (0,)


def test_alias_must_be_a_plain_identifier(conn: sqlite3.Connection, config: Config) -> None:
    with pytest.raises(AnalysisError, match="identifier"):
        open_readonly(config, alias="x; DROP TABLE rows")


def test_a_missing_extension_gives_instructions_not_a_download(
    conn: sqlite3.Connection, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_connect = duckdb.connect

    class NoExtension:
        def __init__(self) -> None:
            self._inner = real_connect(":memory:")

        def execute(self, sql: str, *args: object) -> object:
            if sql.strip().upper() == "LOAD SQLITE":
                raise duckdb.Error("extension not found")
            return self._inner.execute(sql, *args)

        def close(self) -> None:
            self._inner.close()

    monkeypatch.setattr(duckdb, "connect", lambda *_a, **_k: NoExtension())
    with pytest.raises(AnalysisError, match="INSTALL sqlite"):
        open_readonly(config)

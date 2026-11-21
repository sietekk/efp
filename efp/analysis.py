"""Read-only analysis access: DuckDB attaches the SQLite file; notebooks never touch the write path.

``open_readonly`` runs ``LOAD sqlite`` and ``ATTACH ... (TYPE sqlite, READ_ONLY)``. It never runs
``INSTALL``: that is a network download outside the capture layer. If the extension is not
available the error says how to install it once, by hand.
"""

from __future__ import annotations

import duckdb

from efp.config import Config


class AnalysisError(Exception):
    """The analysis connection could not be opened."""


def open_readonly(config: Config, *, alias: str = "pipeline") -> duckdb.DuckDBPyConnection:
    """An in-memory DuckDB connection with the pipeline database attached read-only."""
    db_path = config.paths.database
    if not db_path.is_file():
        raise AnalysisError(f"database not found: {db_path}")
    if not alias.isidentifier():
        raise AnalysisError("alias must be a plain identifier")
    conn = duckdb.connect(":memory:")
    try:
        conn.execute("LOAD sqlite")
    except duckdb.Error as exc:
        conn.close()
        raise AnalysisError(
            "DuckDB's sqlite extension is not installed. Install it once, by hand: "
            "python -c \"import duckdb; duckdb.sql('INSTALL sqlite')\" "
            f"(this build never installs it for you). Detail: {exc}"
        ) from exc
    try:
        path_sql = str(db_path).replace("'", "''")
        conn.execute(f"ATTACH '{path_sql}' AS {alias} (TYPE sqlite, READ_ONLY)")
    except duckdb.Error as exc:
        conn.close()
        raise AnalysisError(f"could not attach {db_path} read-only: {exc}") from exc
    return conn

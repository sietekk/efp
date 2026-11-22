-- 0004_source_workbook: which workbook the rows came from. One immutable row, written by ingest.
--
-- Idempotence (a second `efp ingest` of the same file is a no-op) and export (build 19) both need
-- to know the workbook's hash, sheet and header row, and the year window that fixed rows.in_scope.
-- audit_log also records them for each invocation, but it is append-only and cannot be amended, so
-- the durable record lives here. Sheet row of a row_id = header_row + row_id (docs/row_hash.md).

CREATE TABLE source_workbook (
  workbook_id INTEGER PRIMARY KEY CHECK (workbook_id = 1),  -- exactly one workbook
  sha256      TEXT    NOT NULL,                             -- of the file, matches MANIFEST.txt
  filename    TEXT    NOT NULL,                             -- base name only; no directory
  sheet_name  TEXT    NOT NULL,
  header_row  INTEGER NOT NULL,                             -- 1-based sheet row of the header
  data_rows   INTEGER NOT NULL,                             -- rows loaded (all of them, in or out of scope)
  year_from   INTEGER NOT NULL,                             -- the window that set rows.in_scope
  year_to     INTEGER NOT NULL,
  ingested_at TEXT    NOT NULL
);

-- Same protection as rows: no UPDATE, no DELETE, no REPLACE, from any connection.
CREATE TRIGGER source_workbook_no_update BEFORE UPDATE ON source_workbook
BEGIN SELECT RAISE(ABORT, 'source_workbook is append-only: UPDATE rejected'); END;

CREATE TRIGGER source_workbook_no_delete BEFORE DELETE ON source_workbook
BEGIN SELECT RAISE(ABORT, 'source_workbook is append-only: DELETE rejected'); END;

CREATE TRIGGER source_workbook_no_replace BEFORE INSERT ON source_workbook
WHEN EXISTS (SELECT 1 FROM source_workbook WHERE workbook_id = NEW.workbook_id)
BEGIN SELECT RAISE(ABORT, 'source_workbook is append-only: REPLACE rejected'); END;

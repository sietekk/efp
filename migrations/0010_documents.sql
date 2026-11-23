-- 0010_documents: page-level text, quality and provenance for court PDFs (build 10).
--
-- documents (0002) holds one row per PDF per run. This adds the columns the stage needs:
--   text_capture_id    the canonical text, stored as its own capture (tool = 'documents')
--   layout_capture_id  the word map {page, bbox, char_start, char_end, method}, also a capture
--   canonical_version  the canonical-text rule that produced text_sha256 (docs/documents.md)
--   methods_json       every module tried per page, with the reason one was skipped
--   current            0 once a later run of the stage has replaced this row (never deleted)
-- A re-run inserts a new documents row and flips the old one to current = 0; nothing else about
-- a documents row may change, and none may be deleted.

ALTER TABLE documents ADD COLUMN text_capture_id   INTEGER REFERENCES captures(capture_id);
ALTER TABLE documents ADD COLUMN layout_capture_id INTEGER REFERENCES captures(capture_id);
ALTER TABLE documents ADD COLUMN canonical_version INTEGER;
ALTER TABLE documents ADD COLUMN methods_json      TEXT;
ALTER TABLE documents ADD COLUMN current           INTEGER NOT NULL DEFAULT 1 CHECK (current IN (0, 1));

CREATE TRIGGER documents_no_delete BEFORE DELETE ON documents
BEGIN SELECT RAISE(ABORT, 'documents is append-only: DELETE rejected'); END;

CREATE TRIGGER documents_current_only BEFORE UPDATE ON documents
WHEN NOT (
      OLD.current = 1 AND NEW.current = 0
  AND NEW.document_id       IS OLD.document_id
  AND NEW.row_id            IS OLD.row_id
  AND NEW.record_id         IS OLD.record_id
  AND NEW.capture_id        IS OLD.capture_id
  AND NEW.pre_ocr_sha256    IS OLD.pre_ocr_sha256
  AND NEW.post_ocr_sha256   IS OLD.post_ocr_sha256
  AND NEW.ocr_applied       IS OLD.ocr_applied
  AND NEW.ocr_engine        IS OLD.ocr_engine
  AND NEW.docling_path      IS OLD.docling_path
  AND NEW.page_count        IS OLD.page_count
  AND NEW.text_sha256       IS OLD.text_sha256
  AND NEW.quality_min       IS OLD.quality_min
  AND NEW.quality_json      IS OLD.quality_json
  AND NEW.extracted_at      IS OLD.extracted_at
  AND NEW.text_capture_id   IS OLD.text_capture_id
  AND NEW.layout_capture_id IS OLD.layout_capture_id
  AND NEW.canonical_version IS OLD.canonical_version
  AND NEW.methods_json      IS OLD.methods_json
)
BEGIN SELECT RAISE(ABORT, 'documents is append-only: only current 1 -> 0 may change'); END;

-- One row per page of a documents row. A page below the quality threshold, or one a model
-- transcribed, is flagged: its text is kept (in the canonical text, at char_start..char_end) but
-- it is not extraction-eligible (document_pages_extractable).
CREATE TABLE document_pages (
  document_id  INTEGER NOT NULL REFERENCES documents(document_id),
  page_no      INTEGER NOT NULL CHECK (page_no >= 1),          -- 1-based
  method       TEXT NOT NULL CHECK (method IN
               ('native','docling','tesseract','paddleocr','model','blank','failed')),
  quality      REAL CHECK (quality IS NULL OR (quality >= 0 AND quality <= 1)),
  flagged      INTEGER NOT NULL CHECK (flagged IN (0, 1)),
  flag_reasons TEXT,                                           -- JSON list
  char_start   INTEGER NOT NULL,                               -- span in the canonical text
  char_end     INTEGER NOT NULL,
  PRIMARY KEY (document_id, page_no),
  -- a page a model transcribed, or one with no text, can never be unflagged
  CHECK (NOT (method IN ('model', 'failed') AND flagged = 0))
);

CREATE TRIGGER document_pages_no_update BEFORE UPDATE ON document_pages
BEGIN SELECT RAISE(ABORT, 'document_pages is append-only: UPDATE rejected'); END;
CREATE TRIGGER document_pages_no_delete BEFORE DELETE ON document_pages
BEGIN SELECT RAISE(ABORT, 'document_pages is append-only: DELETE rejected'); END;

-- A page below the threshold its document was read with can never be stored unflagged (the
-- threshold is recorded in documents.quality_json), whatever wrote the row.
CREATE TRIGGER document_pages_below_threshold_flagged BEFORE INSERT ON document_pages
WHEN NEW.flagged = 0 AND NEW.quality IS NOT NULL AND NEW.quality < COALESCE(
  (SELECT json_extract(quality_json, '$.threshold') FROM documents
    WHERE document_id = NEW.document_id), 0)
BEGIN SELECT RAISE(ABORT, 'document_pages: a page below the quality threshold must be flagged'); END;

-- Every page of the latest run of each document, flags included: for reading and for counts.
CREATE VIEW document_pages_current AS
  SELECT p.*, d.row_id, d.record_id, d.capture_id, d.text_capture_id, d.layout_capture_id
    FROM document_pages p JOIN documents d ON d.document_id = p.document_id
   WHERE d.current = 1;

-- What extraction (build 16) may read: unflagged pages with text of the latest run. Flagged,
-- failed and blank pages are not here; they are in document_pages_current with their flag.
-- It also holds each page to the threshold its document was read with; a reader that has since
-- raised documents.ocr_quality_threshold must apply the new value as well (docs/documents.md).
CREATE VIEW document_pages_extractable AS
  SELECT p.* FROM document_pages_current p JOIN documents d ON d.document_id = p.document_id
   WHERE p.flagged = 0 AND p.method NOT IN ('blank', 'failed') AND p.quality IS NOT NULL
     AND p.quality >= COALESCE(json_extract(d.quality_json, '$.threshold'), 1.0);

-- Every model call, for the budget cap and the audit (build 10 transcription, build 16 extraction).
-- temperature is NULL where the API rejects it for the model; temperature_note says so.
CREATE TABLE llm_calls (
  call_id          INTEGER PRIMARY KEY,
  row_id           INTEGER REFERENCES rows(row_id),
  stage            TEXT NOT NULL,
  purpose          TEXT NOT NULL,
  capture_id       INTEGER REFERENCES captures(capture_id),    -- request and response, as stored
  model_id         TEXT NOT NULL,
  prompt_sha256    TEXT,
  temperature      REAL,
  temperature_note TEXT,
  thinking         TEXT,
  input_tokens     INTEGER,
  output_tokens    INTEGER,
  cost_usd         REAL NOT NULL CHECK (cost_usd >= 0),
  request_id       TEXT,
  ok               INTEGER NOT NULL CHECK (ok IN (0, 1)),
  error            TEXT,
  called_at        TEXT NOT NULL
);

CREATE TRIGGER llm_calls_no_update BEFORE UPDATE ON llm_calls
BEGIN SELECT RAISE(ABORT, 'llm_calls is append-only: UPDATE rejected'); END;
CREATE TRIGGER llm_calls_no_delete BEFORE DELETE ON llm_calls
BEGIN SELECT RAISE(ABORT, 'llm_calls is append-only: DELETE rejected'); END;

CREATE INDEX idx_document_pages_doc  ON document_pages(document_id);
CREATE INDEX idx_documents_current   ON documents(row_id, current);
CREATE INDEX idx_llm_calls_called_at ON llm_calls(called_at);
CREATE INDEX idx_documents_text      ON documents(text_capture_id);
CREATE INDEX idx_documents_layout    ON documents(layout_capture_id);
CREATE INDEX idx_llm_calls_row       ON llm_calls(row_id);
CREATE INDEX idx_llm_calls_capture   ON llm_calls(capture_id);
CREATE UNIQUE INDEX idx_documents_one_current ON documents(row_id, capture_id) WHERE current = 1;

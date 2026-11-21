-- 0002_supporting: the supporting tables ("same conventions" in docs/spec.md section 3).
-- The spec gives these as prose, so the columns follow build/02-schema.md. Every row_id,
-- capture_id and record_id is a declared foreign key so PRAGMA foreign_key_check covers it.
-- ref_* tables are NOT created here (build 11's loaders create them). Reserved names:
-- ref_dime, ref_fec_candidates, ref_ftm, ref_local, ref_congress, ref_state_leg,
-- ref_surname_freq, ref_given_freq, ref_population.

-- Each raw URL from the Source cell, its expansion, the method used, and the resulting capture.
CREATE TABLE links (
  link_id          INTEGER PRIMARY KEY,
  row_id           INTEGER NOT NULL REFERENCES rows(row_id),
  url_raw          TEXT NOT NULL,                 -- as it appears in the Source cell
  url_expanded     TEXT,
  expansion_method TEXT CHECK (expansion_method IN
                   ('live','wayback_short','wayback_target','unresolved')),
  status           TEXT NOT NULL CHECK (status IN ('live','archived','dead','unresolved')),
  capture_id       INTEGER REFERENCES captures(capture_id),
  resolved_at      TEXT
);

-- Located records, one row per document. strategy names the court sub-strategy that found it
-- (spec section 4) so coverage is reported per strategy.
CREATE TABLE court_records (
  record_id      INTEGER PRIMARY KEY,
  row_id         INTEGER NOT NULL REFERENCES rows(row_id),
  source_kind    TEXT NOT NULL CHECK (source_kind IN
                 ('doj_release','state_ag_release','da_release','federal_docket',
                  'state_docket','pin_report','other')),
  jurisdiction   TEXT,
  court          TEXT,
  case_number    TEXT,
  document_kind  TEXT,
  strategy       TEXT,
  capture_id     INTEGER REFERENCES captures(capture_id),
  found_at       TEXT NOT NULL
);

-- Court PDFs. The pre-OCR hash is the evidentiary one; both are stored (spec section 7).
CREATE TABLE documents (
  document_id     INTEGER PRIMARY KEY,
  row_id          INTEGER NOT NULL REFERENCES rows(row_id),
  record_id       INTEGER REFERENCES court_records(record_id),
  capture_id      INTEGER REFERENCES captures(capture_id),
  pre_ocr_sha256  TEXT,
  post_ocr_sha256 TEXT,
  ocr_applied     INTEGER NOT NULL DEFAULT 0 CHECK (ocr_applied IN (0, 1)),
  ocr_engine      TEXT,
  docling_path    TEXT,
  page_count      INTEGER,
  text_sha256     TEXT,
  quality_min     REAL,
  quality_json    TEXT,
  extracted_at    TEXT
);

-- News items.
CREATE TABLE articles (
  article_id        INTEGER PRIMARY KEY,
  row_id            INTEGER NOT NULL REFERENCES rows(row_id),
  capture_id        INTEGER REFERENCES captures(capture_id),
  outlet            TEXT,
  published_date    TEXT,
  date_method       TEXT,                         -- how the date was found (htmldate, metadata, ...)
  extractor         TEXT,
  extractor_version TEXT,
  source_kind       TEXT NOT NULL CHECK (source_kind IN ('automated','manual')),
  query_json        TEXT,
  retrieved_at      TEXT
);

-- Handles come only from captured records; the capture that surfaced the handle is recorded.
CREATE TABLE social_accounts (
  account_id        INTEGER PRIMARY KEY,
  row_id            INTEGER NOT NULL REFERENCES rows(row_id),
  platform          TEXT NOT NULL,
  handle            TEXT NOT NULL,
  attribution_basis TEXT,
  allowlist_version TEXT,
  allowlist_sha256  TEXT,
  capture_id        INTEGER REFERENCES captures(capture_id),
  found_at          TEXT
);

-- Gold set: frozen and hashed.
CREATE TABLE gold_set (
  gold_id     INTEGER PRIMARY KEY,
  row_id      INTEGER NOT NULL REFERENCES rows(row_id),
  field       TEXT NOT NULL,
  coder_a     TEXT,
  coder_b     TEXT,
  consensus   TEXT,
  span        TEXT,
  split       TEXT NOT NULL CHECK (split IN ('dev','heldout')),
  set_version TEXT NOT NULL,
  set_sha256  TEXT NOT NULL
);

-- Coverage matrix, one row per (state, kind).
CREATE TABLE state_adapters (
  state          TEXT NOT NULL,
  kind           TEXT NOT NULL CHECK (kind IN ('ag','docket')),
  portal         TEXT,
  adapter_module TEXT,
  mode           TEXT NOT NULL CHECK (mode IN ('automated','assisted','manual','none')),
  reason         TEXT,
  last_verified  TEXT,
  PRIMARY KEY (state, kind)
);

-- Every CLI invocation. Append-only by trigger (0003). finished_at and exit_code are NULL until
-- the invocation ends; the trigger allows exactly that one completion write.
CREATE TABLE audit_log (
  audit_id        INTEGER PRIMARY KEY,
  invoked_at      TEXT NOT NULL,
  finished_at     TEXT,
  command         TEXT NOT NULL,
  args_json       TEXT NOT NULL,                  -- arguments with secret-looking values redacted
  git_sha         TEXT,
  lockfile_sha256 TEXT,
  config_sha256   TEXT,
  exit_code       INTEGER
);

CREATE INDEX idx_links_row               ON links(row_id);
CREATE INDEX idx_links_capture           ON links(capture_id);
CREATE INDEX idx_court_records_row       ON court_records(row_id);
CREATE INDEX idx_court_records_capture   ON court_records(capture_id);
CREATE INDEX idx_documents_row           ON documents(row_id);
CREATE INDEX idx_documents_record        ON documents(record_id);
CREATE INDEX idx_documents_capture       ON documents(capture_id);
CREATE INDEX idx_articles_row            ON articles(row_id);
CREATE INDEX idx_articles_capture        ON articles(capture_id);
CREATE INDEX idx_social_accounts_row     ON social_accounts(row_id);
CREATE INDEX idx_social_accounts_capture ON social_accounts(capture_id);
CREATE INDEX idx_gold_set_row            ON gold_set(row_id);

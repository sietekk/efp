-- 0001_core: the core tables, as written in docs/spec.md section 3.
-- Column names, types, CHECK lists and comments are the spec's. The only additions are the
-- CREATE INDEX statements at the end (the spec has none). Migrations are append-only: never
-- edit this file once applied; add 0004_*.sql instead.

-- Immutable source. Written once by ingest; a trigger rejects UPDATE and DELETE.
CREATE TABLE rows (
  row_id            INTEGER PRIMARY KEY,          -- 1-based position in the source sheet
  year              INTEGER NOT NULL,
  state             TEXT    NOT NULL,
  name              TEXT    NOT NULL,
  case_type         TEXT,
  fraud_type        TEXT,
  details           TEXT,
  source_raw        TEXT,                         -- the Source cell verbatim
  ideological_motivation TEXT,                    -- Treistman's column, as received
  ideological_source     TEXT,
  in_scope          INTEGER NOT NULL,             -- 1 if 2000 <= year <= 2025
  row_sha256        TEXT    NOT NULL,             -- hash of the nine source cells
  ingested_at       TEXT    NOT NULL
);

-- Work queue. One record per (row, stage).
CREATE TABLE case_stage (
  row_id       INTEGER NOT NULL REFERENCES rows(row_id),
  stage        TEXT    NOT NULL CHECK (stage IN
               ('resolve_links','court','identity','news','documents','social','extract','code')),
  status       TEXT    NOT NULL DEFAULT 'pending' CHECK (status IN
               ('pending','running','done','failed','needs_review','skipped')),
  attempts     INTEGER NOT NULL DEFAULT 0,
  gate_reason  TEXT,                              -- why skipped, if skipped
  last_error   TEXT,
  input_hash   TEXT,                              -- hash of inputs; unchanged hash => idempotent no-op
  tool_version TEXT,
  started_at   TEXT, finished_at TEXT,
  PRIMARY KEY (row_id, stage)
);

-- Every retrieval, forever. Never updated.
CREATE TABLE captures (
  capture_id       INTEGER PRIMARY KEY,
  row_id           INTEGER REFERENCES rows(row_id),
  stage            TEXT NOT NULL,
  url_requested    TEXT NOT NULL,
  url_final        TEXT,                          -- after redirects
  retrieved_at     TEXT NOT NULL,                 -- UTC ISO-8601
  http_status      INTEGER,
  content_type     TEXT,
  content_sha256   TEXT NOT NULL,
  body_path        TEXT NOT NULL,                 -- store/<aa>/<sha256>
  headers_json     TEXT,
  warc_path        TEXT, wacz_path TEXT,          -- when captured via Auto Archiver / Browsertrix
  tsa_token_path   TEXT,                          -- RFC 3161 token, when timestamped
  wayback_url      TEXT,                          -- SPN result, when submitted
  tool             TEXT NOT NULL, tool_version TEXT NOT NULL,
  site_data_sha256 TEXT,                          -- WMN / Maigret data hash for social captures
  manual_source    TEXT                           -- 'newsbank' | 'newspapers_com' | 'westlaw' | NULL
);

-- Facts about the person, each pointing at the capture that supports it.
CREATE TABLE identifiers (
  identifier_id  INTEGER PRIMARY KEY,
  row_id         INTEGER NOT NULL REFERENCES rows(row_id),
  kind           TEXT NOT NULL CHECK (kind IN
                 ('middle_name','dob','age','city','case_number','office','employer',
                  'party_on_ballot','handle','email','alias')),
  value          TEXT NOT NULL,
  capture_id     INTEGER NOT NULL REFERENCES captures(capture_id),
  span           TEXT                             -- verbatim text the value came from
);

-- Identity resolution outputs.
CREATE TABLE identity_candidates (
  candidate_id     INTEGER PRIMARY KEY,
  row_id           INTEGER NOT NULL REFERENCES rows(row_id),
  source           TEXT NOT NULL,                 -- 'court' | 'dime' | 'fec' | 'ftm' | 'voter_file' | 'harvard_local'
  fields_json      TEXT NOT NULL,
  prior_namesakes  REAL,                          -- N_namesakes from Census x SSA
  match_weight     REAL,                          -- Splink log2 Bayes factor
  posterior        REAL,                          -- combined with the population prior
  waterfall_json   TEXT                           -- per-comparison evidence, for the audit
);

-- Human and model decisions. Never updated; a reversal is a new row.
CREATE TABLE decisions (
  decision_id   INTEGER PRIMARY KEY,
  row_id        INTEGER NOT NULL REFERENCES rows(row_id),
  kind          TEXT NOT NULL CHECK (kind IN ('identity','code','review','override')),
  outcome       TEXT NOT NULL,                    -- e.g. 'confirmed', 'unresolved', 'motive=financial'
  rationale     TEXT,
  decided_by    TEXT NOT NULL,                    -- 'splink:4.0.17+model:<sha>' or reviewer initials
  decided_at    TEXT NOT NULL,
  evidence_ids  TEXT                              -- JSON list of capture_id / extraction_id
);

-- Typed fields pulled from text, each with its verbatim span.
CREATE TABLE extractions (
  extraction_id    INTEGER PRIMARY KEY,
  row_id           INTEGER NOT NULL REFERENCES rows(row_id),
  capture_id       INTEGER NOT NULL REFERENCES captures(capture_id),
  field            TEXT NOT NULL,                 -- 'stated_motive','party','office_sought','org','role'
  value            TEXT,
  verbatim_span    TEXT,
  char_start       INTEGER, char_end INTEGER,
  alignment_status TEXT CHECK (alignment_status IN
                   ('match_exact','match_greater','match_lesser','match_fuzzy','unaligned')),
  model_id         TEXT, prompt_sha256 TEXT, temperature REAL,
  extracted_at     TEXT NOT NULL
);

-- Final coded values, one per (row, column).
CREATE TABLE codes (
  row_id           INTEGER NOT NULL REFERENCES rows(row_id),
  column_name      TEXT    NOT NULL,
  value            TEXT,
  evidence_tier    TEXT CHECK (evidence_tier IN
                   ('stated_by_subject','court_document','news_reporting','circumstantial','none')),
  extraction_id    INTEGER REFERENCES extractions(extraction_id),
  decision_id      INTEGER REFERENCES decisions(decision_id),
  codebook_version TEXT NOT NULL,
  PRIMARY KEY (row_id, column_name)
);

-- Indexes (not in the spec): every foreign key, plus the access paths the runner and
-- selftest use. case_stage(row_id) and codes(row_id) are covered by their primary keys.
CREATE INDEX idx_case_stage_stage_status   ON case_stage(stage, status);
CREATE INDEX idx_captures_content_sha256   ON captures(content_sha256);
CREATE INDEX idx_captures_row_stage        ON captures(row_id, stage);
CREATE INDEX idx_identifiers_row_kind      ON identifiers(row_id, kind);
CREATE INDEX idx_identifiers_capture       ON identifiers(capture_id);
CREATE INDEX idx_identity_candidates_row   ON identity_candidates(row_id);
CREATE INDEX idx_decisions_row             ON decisions(row_id);
CREATE INDEX idx_extractions_row           ON extractions(row_id);
CREATE INDEX idx_extractions_capture       ON extractions(capture_id);
CREATE INDEX idx_codes_column_name         ON codes(column_name);
CREATE INDEX idx_codes_extraction          ON codes(extraction_id);
CREATE INDEX idx_codes_decision            ON codes(decision_id);

-- 0006_capture_support: what the capture layer (build 05) needs beyond the spec's core tables.
--
-- 1. captures stays append-only, with exactly two exceptions. wayback_url (the Save Page Now
--    result) and tsa_token_path (the RFC 3161 token for the weekly manifest) cannot exist when the
--    row is inserted, so each may go from NULL to a value once. Every other column is immutable,
--    and a value already set cannot be changed (same pattern as audit_log's completion write).
-- 2. spn_queue: the Save Page Now work queue. A mutable work table like case_stage.
-- 3. manifests / manifest_tokens: which captures each weekly manifest covers and its tokens.

DROP TRIGGER captures_no_update;

CREATE TRIGGER captures_no_update BEFORE UPDATE ON captures
WHEN NOT (
      OLD.capture_id       IS NEW.capture_id
  AND OLD.row_id           IS NEW.row_id
  AND OLD.stage            IS NEW.stage
  AND OLD.url_requested    IS NEW.url_requested
  AND OLD.url_final        IS NEW.url_final
  AND OLD.retrieved_at     IS NEW.retrieved_at
  AND OLD.http_status      IS NEW.http_status
  AND OLD.content_type     IS NEW.content_type
  AND OLD.content_sha256   IS NEW.content_sha256
  AND OLD.body_path        IS NEW.body_path
  AND OLD.headers_json     IS NEW.headers_json
  AND OLD.warc_path        IS NEW.warc_path
  AND OLD.wacz_path        IS NEW.wacz_path
  AND OLD.tool             IS NEW.tool
  AND OLD.tool_version     IS NEW.tool_version
  AND OLD.site_data_sha256 IS NEW.site_data_sha256
  AND OLD.manual_source    IS NEW.manual_source
  AND (OLD.wayback_url    IS NEW.wayback_url    OR (OLD.wayback_url    IS NULL AND NEW.wayback_url    IS NOT NULL))
  AND (OLD.tsa_token_path IS NEW.tsa_token_path OR (OLD.tsa_token_path IS NULL AND NEW.tsa_token_path IS NOT NULL))
)
BEGIN SELECT RAISE(ABORT, 'captures is append-only: only wayback_url and tsa_token_path may be set, once, from NULL'); END;

CREATE TABLE spn_queue (
  queue_id        INTEGER PRIMARY KEY,
  capture_id      INTEGER NOT NULL REFERENCES captures(capture_id),
  url             TEXT    NOT NULL,
  status          TEXT    NOT NULL DEFAULT 'pending' CHECK (status IN
                  ('pending','submitted','done','deferred','failed')),
  attempts        INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,                          -- UTC ISO-8601; NULL = as soon as the budget allows
  job_id          TEXT,                          -- SPN2 job id while a submission is in flight
  wayback_url     TEXT,
  last_error      TEXT,
  enqueued_at     TEXT    NOT NULL,
  finished_at     TEXT,
  UNIQUE (capture_id)
);
CREATE INDEX idx_spn_queue_status ON spn_queue(status, next_attempt_at);

CREATE TABLE manifests (
  manifest_id      INTEGER PRIMARY KEY,
  path             TEXT    NOT NULL UNIQUE,      -- relative to paths.manifests
  sha256           TEXT    NOT NULL,             -- hash of the manifest file itself
  iso_week         TEXT,                         -- e.g. 2026-W47
  first_capture_id INTEGER,
  last_capture_id  INTEGER,
  entry_count      INTEGER NOT NULL,
  created_at       TEXT    NOT NULL
);

CREATE TABLE manifest_tokens (
  token_id    INTEGER PRIMARY KEY,
  manifest_id INTEGER NOT NULL REFERENCES manifests(manifest_id),
  tsa_url     TEXT    NOT NULL,
  path        TEXT    NOT NULL,                  -- relative to paths.manifests
  token_sha256 TEXT   NOT NULL,
  gen_time    TEXT,                              -- the TSA's own timestamp, UTC ISO-8601
  created_at  TEXT    NOT NULL,
  UNIQUE (manifest_id, tsa_url)
);

CREATE TABLE manifest_captures (
  manifest_id INTEGER NOT NULL REFERENCES manifests(manifest_id),
  capture_id  INTEGER NOT NULL UNIQUE REFERENCES captures(capture_id),  -- one manifest per capture
  PRIMARY KEY (manifest_id, capture_id)
);
CREATE INDEX idx_manifest_captures_capture ON manifest_captures(capture_id);

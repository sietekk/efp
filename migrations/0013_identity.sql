-- 0013_identity: identity resolution (build 12).
--
-- decisions gain the stage they belong to, a batch (the pre-registered run is 'main'; a sensitivity
-- re-run at a stricter threshold is 'sensitivity' and never read by a gate) and the candidate a
-- label or verdict is about. identity_candidates gain the settings they were scored with, when, and
-- a current flag: a rescore appends new rows and flips the old ones, so a score is never rewritten
-- in place and the audit can still read what a decision was made from.
--
-- identity_label_queue and identity_audit_sample are the two operator samples (the 150 pairs that
-- fit m and u, the 50 confirmed rows of the F3 audit). They are append-only; the operator's labels
-- and verdicts are decisions of kind 'review', not columns here.
--
-- ref_place_county is the Census place-to-county relationship, for the city comparison's
-- "same county" level (Census 2020 place-to-county relationship file, public domain).

ALTER TABLE decisions ADD COLUMN stage TEXT;
ALTER TABLE decisions ADD COLUMN batch TEXT NOT NULL DEFAULT 'main';
ALTER TABLE decisions ADD COLUMN candidate_id INTEGER;
CREATE INDEX idx_decisions_row_kind_batch ON decisions(row_id, kind, batch);

ALTER TABLE identity_candidates ADD COLUMN settings_sha TEXT;
ALTER TABLE identity_candidates ADD COLUMN scored_at TEXT;
ALTER TABLE identity_candidates ADD COLUMN current INTEGER NOT NULL DEFAULT 1;
ALTER TABLE identity_candidates ADD COLUMN alias_index INTEGER;
CREATE INDEX idx_identity_candidates_current ON identity_candidates(row_id, current);

CREATE TABLE identity_label_queue (
  queue_id     INTEGER PRIMARY KEY,
  candidate_id INTEGER NOT NULL REFERENCES identity_candidates(candidate_id),
  row_id       INTEGER NOT NULL REFERENCES rows(row_id),
  stratum      TEXT    NOT NULL,
  position     INTEGER NOT NULL,
  seed         INTEGER NOT NULL,
  built_at     TEXT    NOT NULL,
  UNIQUE (candidate_id)
);
CREATE TRIGGER identity_label_queue_no_update BEFORE UPDATE ON identity_label_queue
BEGIN SELECT RAISE(ABORT, 'identity_label_queue is append-only: UPDATE rejected'); END;
CREATE TRIGGER identity_label_queue_no_delete BEFORE DELETE ON identity_label_queue
BEGIN SELECT RAISE(ABORT, 'identity_label_queue is append-only: DELETE rejected'); END;

CREATE TABLE identity_audit_sample (
  audit_id    INTEGER PRIMARY KEY,
  row_id      INTEGER NOT NULL REFERENCES rows(row_id),
  decision_id INTEGER NOT NULL REFERENCES decisions(decision_id),
  stratum     TEXT    NOT NULL,                  -- 'common' | 'rare'
  band        INTEGER NOT NULL,                  -- 1 if the row's posterior was ever in the review band
  weight      REAL    NOT NULL,                  -- inverse inclusion probability of the draw
  draw        INTEGER NOT NULL,                  -- which draw (1 = the first sample of 50)
  seed        INTEGER NOT NULL,
  drawn_at    TEXT    NOT NULL,
  UNIQUE (row_id, draw)
);
CREATE TRIGGER identity_audit_sample_no_update BEFORE UPDATE ON identity_audit_sample
BEGIN SELECT RAISE(ABORT, 'identity_audit_sample is append-only: UPDATE rejected'); END;
CREATE TRIGGER identity_audit_sample_no_delete BEFORE DELETE ON identity_audit_sample
BEGIN SELECT RAISE(ABORT, 'identity_audit_sample is append-only: DELETE rejected'); END;

CREATE TABLE ref_place_county (
  load_id    INTEGER NOT NULL REFERENCES ref_sources(load_id),
  state      TEXT NOT NULL,                      -- USPS code
  place_key  TEXT NOT NULL,                      -- upper-case A-Z, "city"/"town"/... suffix dropped
  county_key TEXT NOT NULL,                      -- upper-case A-Z, "county"/"parish"/... suffix dropped
  area_land  INTEGER NOT NULL DEFAULT 0          -- land area of the place inside the county
);
CREATE INDEX idx_ref_place_county ON ref_place_county(load_id, state, place_key);
CREATE VIEW ref_place_county_current AS
  SELECT t.* FROM ref_place_county t JOIN ref_sources s ON s.load_id = t.load_id WHERE s.current = 1;

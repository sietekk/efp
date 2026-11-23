-- 0011_reference: the reference datasets (build 11). Spec sections 5 and 8.
--
-- ref_sources has one row per loaded (or refused) file: where it came from, when, its SHA-256, its
-- license and the attribution sentence, and why it was gated or skipped. Every data table row carries
-- the load_id of the file it came from. A reload of a byte-identical file is a no-op; a different file
-- is a new load and the old one is set current = 0 (its rows stay; the *_current views hide them).
-- Reference files are never committed; the raw files live under paths.bulk.

CREATE TABLE ref_sources (
  load_id      INTEGER PRIMARY KEY,
  dataset      TEXT NOT NULL,                       -- census_surnames | ssa_names | ... | wmn
  part         TEXT NOT NULL DEFAULT '',            -- one dataset, several files: ssa national / state, fec cycle, congress current / historical
  url          TEXT,
  version      TEXT,                                -- vintage as published (e.g. 2010, 2023, a cycle)
  retrieved_at TEXT,
  file_sha256  TEXT,
  file_size    INTEGER,
  capture_id   INTEGER REFERENCES captures(capture_id),   -- the download (or the manual import) as a capture
  license      TEXT,
  attribution  TEXT,
  row_count    INTEGER,
  notes_json   TEXT,                                -- the surname floor and its rule, the reason for a gate, the columns a file had
  status       TEXT NOT NULL CHECK (status IN ('loaded', 'gated', 'skipped', 'failed')),
  current      INTEGER NOT NULL DEFAULT 1 CHECK (current IN (0, 1)),
  loaded_at    TEXT NOT NULL
);
CREATE INDEX idx_ref_sources_dataset ON ref_sources(dataset, part, current);
CREATE INDEX idx_ref_sources_capture ON ref_sources(capture_id);
CREATE UNIQUE INDEX idx_ref_sources_loaded_file ON ref_sources(dataset, part, file_sha256) WHERE status = 'loaded';

-- Population prior inputs (spec section 8).
CREATE TABLE ref_surname_freq (
  load_id  INTEGER NOT NULL REFERENCES ref_sources(load_id),
  surname  TEXT NOT NULL,                           -- as published (upper case)
  rank     INTEGER,
  count    INTEGER NOT NULL,
  prop100k REAL NOT NULL                            -- proportion per 100,000 residents
);
CREATE INDEX idx_ref_surname_load ON ref_surname_freq(load_id, surname);

CREATE TABLE ref_given_freq (
  load_id INTEGER NOT NULL REFERENCES ref_sources(load_id),
  name    TEXT NOT NULL,
  sex     TEXT NOT NULL CHECK (sex IN ('F', 'M')),
  year    INTEGER NOT NULL,                         -- year of birth
  state   TEXT,                                     -- NULL for the national file
  count   INTEGER NOT NULL
);
CREATE INDEX idx_ref_given_load ON ref_given_freq(load_id, name, sex, year);
CREATE INDEX idx_ref_given_state ON ref_given_freq(state);

CREATE TABLE ref_population (
  load_id  INTEGER NOT NULL REFERENCES ref_sources(load_id),
  state    TEXT NOT NULL,                           -- USPS code
  age_band TEXT NOT NULL,                           -- 0-4, 5-9, ..., 80-84, 85+
  year     INTEGER NOT NULL,                        -- the estimate year
  count    INTEGER NOT NULL
);
CREATE INDEX idx_ref_population_load ON ref_population(load_id, state, year);

-- Candidate universes. Names are parsed at load (surname_key is upper-case A-Z only, for blocking).
-- Party is an identity signal for a public figure and is never evidence of motive (build 18).
CREATE TABLE ref_dime (
  load_id    INTEGER NOT NULL REFERENCES ref_sources(load_id),
  bonica_rid TEXT NOT NULL,
  cycle      INTEGER,
  name_full  TEXT,
  surname    TEXT, given TEXT, middle TEXT, surname_key TEXT,
  state      TEXT, district TEXT, office TEXT, party TEXT,
  cfscore    REAL
);
CREATE INDEX idx_ref_dime_load ON ref_dime(load_id, surname_key);

CREATE TABLE ref_fec_candidates (
  load_id       INTEGER NOT NULL REFERENCES ref_sources(load_id),
  cand_id       TEXT NOT NULL,
  cycle         INTEGER,                            -- the file's two-year cycle
  name_full     TEXT,
  surname       TEXT, given TEXT, middle TEXT, surname_key TEXT,
  party         TEXT,
  election_year INTEGER,
  state         TEXT,                               -- office state
  office        TEXT,                               -- H, S, P
  district      TEXT,
  ici           TEXT,                               -- incumbent / challenger / open seat
  status        TEXT
);
CREATE INDEX idx_ref_fec_load ON ref_fec_candidates(load_id, surname_key);

CREATE TABLE ref_ftm (
  load_id     INTEGER NOT NULL REFERENCES ref_sources(load_id),
  ftm_id      TEXT NOT NULL,
  name_full   TEXT,
  surname     TEXT, given TEXT, middle TEXT, surname_key TEXT,
  state       TEXT, office TEXT, party TEXT,
  year        INTEGER,
  won         INTEGER
);
CREATE INDEX idx_ref_ftm_load ON ref_ftm(load_id, surname_key);

CREATE TABLE ref_local (
  load_id       INTEGER NOT NULL REFERENCES ref_sources(load_id),
  record_id     TEXT NOT NULL,
  name_full     TEXT,
  surname       TEXT, given TEXT, middle TEXT, surname_key TEXT,
  state         TEXT, place TEXT, office TEXT,
  party         TEXT,
  party_imputed INTEGER NOT NULL DEFAULT 0 CHECK (party_imputed IN (0, 1)),   -- 1 where the race is nonpartisan: never assert it
  year          INTEGER
);
CREATE INDEX idx_ref_local_load ON ref_local(load_id, surname_key);

CREATE TABLE ref_congress (
  load_id    INTEGER NOT NULL REFERENCES ref_sources(load_id),
  bioguide   TEXT NOT NULL,
  fec_ids    TEXT,                                  -- JSON list
  opensecrets TEXT, icpsr TEXT, govtrack TEXT,
  name_full  TEXT,
  surname    TEXT, given TEXT, middle TEXT, surname_key TEXT,
  birthday   TEXT,
  state      TEXT,                                  -- last state served
  party      TEXT,                                  -- last party
  first_year INTEGER, last_year INTEGER,
  terms_json TEXT
);
CREATE INDEX idx_ref_congress_load ON ref_congress(load_id, surname_key);

CREATE TABLE ref_state_leg (
  load_id      INTEGER NOT NULL REFERENCES ref_sources(load_id),
  openstates_id TEXT NOT NULL,
  name_full    TEXT,
  surname      TEXT, given TEXT, middle TEXT, surname_key TEXT,
  state        TEXT,
  chamber      TEXT,
  district     TEXT,
  party        TEXT,
  first_year   INTEGER, last_year INTEGER,
  retired      INTEGER NOT NULL DEFAULT 0 CHECK (retired IN (0, 1))
);
CREATE INDEX idx_ref_state_leg_load ON ref_state_leg(load_id, surname_key);

-- Only the rows of each dataset's current load.
CREATE VIEW ref_surname_freq_current AS
  SELECT t.* FROM ref_surname_freq t JOIN ref_sources s ON s.load_id = t.load_id WHERE s.current = 1;
CREATE VIEW ref_given_freq_current AS
  SELECT t.* FROM ref_given_freq t JOIN ref_sources s ON s.load_id = t.load_id WHERE s.current = 1;
CREATE VIEW ref_population_current AS
  SELECT t.* FROM ref_population t JOIN ref_sources s ON s.load_id = t.load_id WHERE s.current = 1;
CREATE VIEW ref_dime_current AS
  SELECT t.* FROM ref_dime t JOIN ref_sources s ON s.load_id = t.load_id WHERE s.current = 1;
CREATE VIEW ref_fec_candidates_current AS
  SELECT t.* FROM ref_fec_candidates t JOIN ref_sources s ON s.load_id = t.load_id WHERE s.current = 1;
CREATE VIEW ref_ftm_current AS
  SELECT t.* FROM ref_ftm t JOIN ref_sources s ON s.load_id = t.load_id WHERE s.current = 1;
CREATE VIEW ref_local_current AS
  SELECT t.* FROM ref_local t JOIN ref_sources s ON s.load_id = t.load_id WHERE s.current = 1;
CREATE VIEW ref_congress_current AS
  SELECT t.* FROM ref_congress t JOIN ref_sources s ON s.load_id = t.load_id WHERE s.current = 1;
CREATE VIEW ref_state_leg_current AS
  SELECT t.* FROM ref_state_leg t JOIN ref_sources s ON s.load_id = t.load_id WHERE s.current = 1;

-- The candidate universe build 12 matches against, in one shape and indexed for blocking on the
-- surname. It is rebuilt by `efp reference index` from whichever current loads exist, so the identity
-- build works over any subset. source_id is the record's own identifier in its source (a FEC
-- candidate ID, a bioguide ID, a DIME bonica_rid, ...). Nicknames are build 12's table.
CREATE TABLE ref_candidates_all (
  source      TEXT NOT NULL CHECK (source IN
              ('dime', 'fec', 'ftm', 'harvard_local', 'congress', 'state_leg')),
  source_id   TEXT NOT NULL,
  load_id     INTEGER NOT NULL REFERENCES ref_sources(load_id),
  surname     TEXT, given TEXT, middle TEXT,
  surname_key TEXT,
  state       TEXT,
  office      TEXT,
  party       TEXT,
  party_imputed INTEGER NOT NULL DEFAULT 0,
  years       TEXT,                                 -- e.g. 2004-2018
  first_year  INTEGER,
  last_year   INTEGER
);
CREATE INDEX idx_ref_candidates_surname ON ref_candidates_all(surname_key, state);
CREATE INDEX idx_ref_candidates_source ON ref_candidates_all(source, source_id);
CREATE INDEX idx_ref_candidates_load ON ref_candidates_all(load_id);

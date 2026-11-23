-- 0012_reference_loader_version: a parser fix must be able to reload a byte-identical file, and the
-- candidate index must know which loads it was built from (build 11 review).
--
-- ref_sources.loader_version is the version of the loader that read the file. The "this file is
-- already loaded" uniqueness now includes it, so the same file read by an improved loader is a new
-- load (the old one stays, current = 0). ref_index_state records, per rebuild of ref_candidates_all,
-- the fingerprint of the loads it was built from; a later load, import or reload that changes
-- which loads are current makes the index stale even when the row counts happen to match.

ALTER TABLE ref_sources ADD COLUMN loader_version INTEGER NOT NULL DEFAULT 1;

DROP INDEX idx_ref_sources_loaded_file;
CREATE UNIQUE INDEX idx_ref_sources_loaded_file
  ON ref_sources(dataset, part, file_sha256, loader_version) WHERE status = 'loaded';

CREATE TABLE ref_index_state (
  built_at    TEXT NOT NULL,
  fingerprint TEXT NOT NULL,                        -- the current candidate load_ids, sorted
  total       INTEGER NOT NULL
);
CREATE INDEX idx_ref_index_state_built ON ref_index_state(built_at);

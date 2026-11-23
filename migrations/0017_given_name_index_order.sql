-- 0017_given_name_index_order: 0016's index led with the name, so an initial (a range of names with
-- state IS NULL) walked every state's rows for the letter. Leading with state makes state IS NULL an
-- equality ahead of the name range, and still serves the exact-name lookups (state, name, year).
-- Maintenance 03 review.

DROP INDEX idx_ref_given_name;
CREATE INDEX idx_ref_given_state_name ON ref_given_freq(state, name, year);

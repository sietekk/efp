-- 0016_given_name_index: the population prior looks a given name up by name, state and birth year
-- (efp/identity/prior.py). With the SSA files loaded (8.9 million rows, 6.7 million of them by
-- state) the existing indexes lead with load_id or state, so a national lookup scanned every
-- national row (about 150 ms per candidate). Maintenance 03 measured it.

CREATE INDEX idx_ref_given_name ON ref_given_freq(name, state, year);

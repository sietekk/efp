-- A located document whose match is unconfirmed (several candidates, initial only, state not
-- corroborated) is marked weak, with the evidence for the match, so readers of court_records
-- never mistake a candidate for the primary record.
ALTER TABLE court_records ADD COLUMN weak INTEGER NOT NULL DEFAULT 0;
ALTER TABLE court_records ADD COLUMN basis_json TEXT;

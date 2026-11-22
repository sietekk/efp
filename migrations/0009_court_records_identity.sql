-- A located record is identified by the document it is about, not by the search that found it.
--   docket_id: the CourtListener docket, so the same docket found again (even with a changed
--              search response, or a weak flag that has flipped) is the same record.
--   current:   0 when a later run of the strategy that found it no longer finds it (a new bulk
--              quarter dropped the candidate, the row's state changed). Readers use current = 1;
--              the old row is kept, not deleted.
ALTER TABLE court_records ADD COLUMN docket_id INTEGER;
ALTER TABLE court_records ADD COLUMN current INTEGER NOT NULL DEFAULT 1;

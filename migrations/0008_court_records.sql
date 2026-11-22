-- The court stage's record table, as readers should see it.
--
-- court_records keeps every record a run ever found. Columns added here:
--   weak            1 when the match is an unconfirmed candidate (several candidates, an initial
--                   only, a state or date nothing corroborates)
--   basis_json      the evidence for the match, and the record's `identity` for non-docket records
--   docket_id       the CourtListener docket: a docket is identified by it, not by a search response
--   current         0 when a later run of the strategy that found the record no longer finds it
--                   (the row is kept, never deleted)
--   text_capture_id the capture of the extracted text the record's identifiers were read from
ALTER TABLE court_records ADD COLUMN weak INTEGER NOT NULL DEFAULT 0;
ALTER TABLE court_records ADD COLUMN basis_json TEXT;
ALTER TABLE court_records ADD COLUMN docket_id INTEGER;
ALTER TABLE court_records ADD COLUMN current INTEGER NOT NULL DEFAULT 1;
ALTER TABLE court_records ADD COLUMN text_capture_id INTEGER;

-- Later builds read these views, never the table, or they would treat superseded rows and
-- candidates as findings.
--   court_records_current    records the latest run still finds (candidates included)
--   court_records_confirmed  those that are not candidates
-- A filing is read from its docket's RECAP listing, so it is only as current and as confirmed as
-- its parent docket record (same row, same docket id): filings are re-captured only for a docket
-- that is not weak, and a docket that turns weak or is no longer found leaves them untouched.
CREATE VIEW court_records_current AS
  SELECT cr.* FROM court_records cr
   WHERE cr.current = 1
     AND (cr.document_kind IS NOT 'filing'
          OR EXISTS (SELECT 1 FROM court_records d
                      WHERE d.row_id = cr.row_id AND d.docket_id = cr.docket_id
                        AND d.document_kind = 'docket' AND d.current = 1));

CREATE VIEW court_records_confirmed AS
  SELECT cr.* FROM court_records cr
   WHERE cr.current = 1 AND cr.weak = 0
     AND (cr.document_kind IS NOT 'filing'
          OR EXISTS (SELECT 1 FROM court_records d
                      WHERE d.row_id = cr.row_id AND d.docket_id = cr.docket_id
                        AND d.document_kind = 'docket' AND d.current = 1 AND d.weak = 0));

-- Identifiers must not outlive the record that justified them: those read from a record that is
-- still current; person-level kinds (age, city, dob, office, employer, party) only from one that
-- is also not weak, a case number from any current record.
CREATE VIEW court_identifiers_current AS
  SELECT i.*
    FROM identifiers i
   WHERE EXISTS (
     SELECT 1 FROM court_records cr
      WHERE cr.row_id = i.row_id
        AND cr.text_capture_id = i.capture_id
        AND cr.current = 1
        AND (cr.weak = 0 OR i.kind = 'case_number')
   );

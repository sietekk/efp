-- Person-level identifiers must not outlive the record that justified them.
--
-- A court record that was confirmed and is later ambiguous (weak) or no longer found
-- (current = 0) leaves the identifiers already read from its text in `identifiers`. The record now
-- names the text capture it was read from, and a view joins the two:
--   court_identifiers_current  identifiers read from a record that is still current; person-level
--                              kinds (age, city, dob, office, employer, party, ...) only from one
--                              that is also not weak; a case number from any current record.
-- Later builds read this view, not `identifiers`, for what the court stage found.
ALTER TABLE court_records ADD COLUMN text_capture_id INTEGER;

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

-- A filing is read from its docket's RECAP listing: it is only as current and as confirmed as
-- that docket. Filings are re-captured only for a docket that is not weak, so when a docket turns
-- weak (a second candidate) or is no longer found, its earlier filing rows are not touched by the
-- run, and a view that looked only at the filing's own flags would keep calling them confirmed.
-- The two views therefore tie a filing to its parent docket record (same row, same docket id).
DROP VIEW court_records_current;
DROP VIEW court_records_confirmed;

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

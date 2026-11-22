-- What a later build should read from court_records.
--
-- court_records keeps every record a run ever found (a superseded one is current = 0, never
-- deleted), and marks an unconfirmed candidate weak = 1. Reading the table directly would treat
-- both as findings. These views are the safe way in:
--   court_records_current    the records the latest run still finds (candidates included)
--   court_records_confirmed  those that are not candidates (current = 1 and weak = 0)
CREATE VIEW court_records_current AS
  SELECT * FROM court_records WHERE current = 1;

CREATE VIEW court_records_confirmed AS
  SELECT * FROM court_records WHERE current = 1 AND weak = 0;

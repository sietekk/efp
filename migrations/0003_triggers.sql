-- 0003_triggers: append-only enforcement.
--
--   rows, captures, decisions  reject every UPDATE and DELETE, and every INSERT whose key already
--                              exists (which is what REPLACE / INSERT OR REPLACE do after
--                              deleting the old row). A reversal of a decision is a new row.
--   audit_log                  the same, except that one UPDATE per row is allowed: the
--                              completion write that fills finished_at and exit_code from NULL
--                              with every other column unchanged.
--
-- The *_no_replace triggers matter because SQLite fires DELETE triggers for a REPLACE only when
-- PRAGMA recursive_triggers is ON, and a plain sqlite3 shell or any non-efp connection has it OFF.
-- These triggers do not depend on that setting. efp.db.connect() also turns recursive_triggers ON
-- as a second layer, and `efp selftest --db` probes both.
--
-- What triggers cannot stop is schema change (DROP TRIGGER, ALTER TABLE) from a raw connection.
-- See migrations/README.md; tests/test_db_guard.py keeps it out of efp/ and of later migrations.
--
-- codes is deliberately NOT append-only: it is derived state (rebuilt by the code stage when a
-- codebook version changes, `efp run code --reset`), and the history of any human-touched value
-- lives in decisions, which is append-only. case_stage is the one mutable work table.

CREATE TRIGGER rows_no_update BEFORE UPDATE ON rows
BEGIN SELECT RAISE(ABORT, 'rows is append-only: UPDATE rejected'); END;

CREATE TRIGGER rows_no_delete BEFORE DELETE ON rows
BEGIN SELECT RAISE(ABORT, 'rows is append-only: DELETE rejected'); END;

CREATE TRIGGER captures_no_update BEFORE UPDATE ON captures
BEGIN SELECT RAISE(ABORT, 'captures is append-only: UPDATE rejected'); END;

CREATE TRIGGER captures_no_delete BEFORE DELETE ON captures
BEGIN SELECT RAISE(ABORT, 'captures is append-only: DELETE rejected'); END;

CREATE TRIGGER decisions_no_update BEFORE UPDATE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions is append-only: UPDATE rejected'); END;

CREATE TRIGGER decisions_no_delete BEFORE DELETE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions is append-only: DELETE rejected'); END;

CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only: DELETE rejected'); END;

CREATE TRIGGER audit_log_complete_once BEFORE UPDATE ON audit_log
WHEN NOT (
      OLD.finished_at IS NULL AND OLD.exit_code IS NULL
  AND NEW.finished_at IS NOT NULL AND NEW.exit_code IS NOT NULL
  AND NEW.audit_id        IS OLD.audit_id
  AND NEW.invoked_at      IS OLD.invoked_at
  AND NEW.command         IS OLD.command
  AND NEW.args_json       IS OLD.args_json
  AND NEW.git_sha         IS OLD.git_sha
  AND NEW.lockfile_sha256 IS OLD.lockfile_sha256
  AND NEW.config_sha256   IS OLD.config_sha256
)
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only: only a one-time completion update is allowed'); END;

-- Refuse an INSERT that would replace an existing row. A fresh key (or NULL, which SQLite
-- assigns) passes.
CREATE TRIGGER rows_no_replace BEFORE INSERT ON rows
WHEN EXISTS (SELECT 1 FROM rows WHERE row_id = NEW.row_id)
BEGIN SELECT RAISE(ABORT, 'rows is append-only: REPLACE rejected'); END;

CREATE TRIGGER captures_no_replace BEFORE INSERT ON captures
WHEN EXISTS (SELECT 1 FROM captures WHERE capture_id = NEW.capture_id)
BEGIN SELECT RAISE(ABORT, 'captures is append-only: REPLACE rejected'); END;

CREATE TRIGGER decisions_no_replace BEFORE INSERT ON decisions
WHEN EXISTS (SELECT 1 FROM decisions WHERE decision_id = NEW.decision_id)
BEGIN SELECT RAISE(ABORT, 'decisions is append-only: REPLACE rejected'); END;

CREATE TRIGGER audit_log_no_replace BEFORE INSERT ON audit_log
WHEN EXISTS (SELECT 1 FROM audit_log WHERE audit_id = NEW.audit_id)
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only: REPLACE rejected'); END;

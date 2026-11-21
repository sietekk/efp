-- 0003_triggers: append-only enforcement.
--
--   rows, captures, decisions  reject every UPDATE and DELETE. (A reversal of a decision is a
--                              new decisions row.)
--   audit_log                  rejects every DELETE and every UPDATE except the one completion
--                              write: filling finished_at and exit_code, once, from NULL, with
--                              every other column unchanged.
--
-- INSERT OR REPLACE deletes the conflicting row, which only fires DELETE triggers when
-- PRAGMA recursive_triggers is ON. efp.db.connect() always turns it on, and selftest --db
-- checks that a REPLACE is refused.
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

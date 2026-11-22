-- 0005_case_stage_summary: where the runner keeps each (row, stage) outcome summary.
-- The module chain records every module's outcome ({name, status, reason, ms}) here, and the
-- first-pass report aggregates it. case_stage is the one mutable work table, so a plain column
-- is enough; the append-only record of what was fetched stays in captures.
ALTER TABLE case_stage ADD COLUMN summary_json TEXT;

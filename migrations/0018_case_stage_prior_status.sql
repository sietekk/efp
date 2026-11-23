-- 0018_case_stage_prior_status: what a row's stage was before the runner claimed it.
--
-- A row claimed by `--recheck` or `--force` goes from done or needs_review to running. If the process
-- dies, recovery used to put every running row back as pending, which dropped a review-band row out
-- of `efp review` until its stage was run again. The claim now records the status it replaced, and
-- recovery restores a settled one (done, needs_review); anything else is pending as before.
-- case_stage is the one mutable work table.

ALTER TABLE case_stage ADD COLUMN prior_status TEXT;

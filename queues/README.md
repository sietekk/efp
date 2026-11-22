# queues/

The pipeline's work lists for the operator are written under the data directory
(`~/efp-data/queues/`), not here: they name row ids and case numbers, and row-level data stays
with the data. This folder is kept for the layout in `CLAUDE.md`; any `*.csv` that ends up here is
git-ignored.

- `pacer.csv` (build 07): federal dockets found without their filings in RECAP, or whose filings
  were not checked, with what is missing. Columns: `row_id, court, case_number, docket_id, missing`.
  Location is `court.pacer_queue` in `config.toml` (relative paths are under `paths.data_dir`).

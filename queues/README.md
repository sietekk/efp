# queues/

The pipeline's work lists for the operator are written under the data directory
(`~/efp-data/queues/`), not here: they name row ids and case numbers, and row-level data stays
with the data. This folder is kept for the layout in `CLAUDE.md`; any `*.csv` that ends up here is
git-ignored.

- `pacer.csv` (build 07): federal dockets found without their filings in RECAP, or whose filings
  were not checked, with what is missing. Columns: `row_id, court, case_number, docket_id, missing`.
  Location is `court.pacer_queue` in `config.toml` (relative paths are under `paths.data_dir`).
- `portal_accounts.csv` (build 09): docket portals that are manual or want something the pipeline
  will not give (an account, a fee, a person at a browser, terms not yet cleared). Columns:
  `state, portal, url, wants, detail, rows_affected`. Written by `efp court docket probe`.
- `assisted_dockets.csv` (build 09): rows whose state's docket portal is `assisted`, with the query
  to run in a browser and the import command to run afterwards. Columns: `row_id, state, year,
  portal_url, name, filed_from, filed_to, after`. Written by `efp review court --assisted`.

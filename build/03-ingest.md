# Build 03: Ingest and the round-trip guarantee

Spec: section 4 (ingest component), section 1 (F1, F10), section 11 (integration test on a synthetic workbook). Task list: D1, C1. Needs: 02. Branch: `build/03-ingest`. Report: `reports/03-ingest.md`.

Read first: `CLAUDE.md`, `build/README.md`, `docs/spec.md` sections 1, 3 (`rows`) and 4 (ingest and export contracts; ingest must produce what export will verify), `docs/plan.md` "Jeff's open decisions" (decision 5: the 50-row sample is frozen in the skeleton), `reports/02-schema.md`.

Stay in plan mode until I approve. Propose the column mapping, the hash canonicalization, the header-detection rule, the sample stratification and the synthetic workbook generator. Wait.

## What to build

1. **Workbook reader.** `efp ingest <workbook.xlsx>` opens the file with openpyxl in read-only mode. The Heritage export does not start with the header row (the first rows are a title block; in the April 2026 export the header is the fourth row). Detect the header by content (the row containing `Year`, `State`, `Name`), never by position, and fail loudly if the nine expected columns are not all present. The nine source columns, in order, map to `rows`: Year → `year`, State → `state`, Name → `name`, Case Type → `case_type`, Fraud Type → `fraud_type`, Details → `details`, Source → `source_raw`, Ideological Motivation → `ideological_motivation`, Ideological Source → `ideological_source`. `row_id` is the 1-based position among data rows in sheet order; it never changes.
2. **Manifest check first.** Before reading, compute the workbook's SHA-256 and compare it with `MANIFEST.txt` in `paths.source` (format: `shasum -a 256` output). Mismatch or missing manifest aborts with the two hashes printed. Record the workbook hash, sheet name and header row index in `audit_log.args_json`.
3. **Row hash.** `row_sha256` = SHA-256 over the nine cell values as the *exact strings openpyxl returns*, with `None` as the empty string, joined by the unit separator `\x1f`. Numbers are not reformatted (a `Year` cell read as `2016` and one read as `"2016"` hash differently, and that is correct: export must reproduce the original cell). Store the canonicalization rule in `docs/row_hash.md`; export (build 19) verifies against it.
4. **Scope flag.** `in_scope = 1` means exactly `2000 <= year <= 2025`, as the schema comment says, and nothing else. The population choice (decision 2: `scope.population = "all"` or `"convictions"`, where a conviction is a `case_type` beginning with "Criminal Conviction") is applied at query time by the runner and by export, never baked into `rows`, because `rows` is immutable and the decision may change after ingest. Record the population rule in force in `audit_log.args_json` on every run.
5. **Idempotence.** Ingest refuses to run twice against a populated `rows` table unless the workbook hash matches and every `row_sha256` matches, in which case it is a no-op that says so. One (row, stage) record per stage in `case_stage` is created at ingest with status `pending`, for every stage in `runner.stages` from `config.toml`.
6. **The frozen sample.** Draw the 50-row stratified sample (strata: year band 2000–09 / 2010–16 / 2017–25; federal vs state, where federal means `source_raw` or `details` names a U.S. Attorney, DOJ or a federal court, else state; fraud type collapsed to the top five categories plus other) with `scope.sample_seed`, and write `tests/fixtures/sample_rows.json` holding only `row_id` and `row_sha256` per row, plus the seed and the strata counts. No names in the fixture. `efp run <stage> --sample` (build 04) uses this file.
7. **Round trip, early.** `efp export --dry-run` (a minimal version here; build 19 completes export) writes the nine source columns back into a copy of the workbook and asserts byte-identical cell values against `row_sha256` for every row. This is the F1 test and it runs in CI against the synthetic workbook.
8. **Synthetic workbook generator** in `tests/fixtures/make_workbook.py`: 20 invented rows with the Heritage layout (title block, header on row 4, the nine columns, a few blank cells, one `aka` name, one year outside the window), used by the integration test. Never derived from real rows.

## Tests

- Header detection on the synthetic workbook and on a variant with the header on a different row.
- Hash canonicalization: a changed whitespace character changes the hash; `None` and `""` hash the same; numeric versus string cells differ.
- Manifest mismatch aborts; re-ingest of the identical workbook is a no-op; re-ingest of a modified workbook is refused.
- Sample is reproducible from the seed and covers every stratum that has rows.
- F1 round trip on the synthetic workbook.

## Done when

- `uv run efp ingest ~/efp-data/source/<workbook>.xlsx` loads 1,481 rows, prints the count by year band and case type, and `efp status` shows every stage `pending`.
- `uv run efp export --dry-run` passes F1 on the real workbook and the report states the measured counts: rows in the year window, rows with a non-empty Source cell (expected 1,322), and the strata sizes of the frozen sample.

## Guardrails that bite here

The nine source columns are never written. `rows` is written once. The real workbook never enters the repository or the test suite; `.gitignore` already excludes `*.xlsx` outside `tests/fixtures/`.

Finish with `reports/03-ingest.md` and a pull request. Do not merge.

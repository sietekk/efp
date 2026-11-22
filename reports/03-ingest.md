# Build 03 — Ingest and the round-trip guarantee

Branch: `build/03-ingest` · PR: this PR · Date: 2026-11-21 · Model: claude-opus-5-5 (plan), claude-sonnet-5-5 (build) · Prompt: `build/03-ingest.md` · Spec sections: 4 (ingest), 1 (F1, F10), 3 (`rows`), 11

**Read first.** The real workbook is ingested: `~/efp-data/pipeline.db` exists (mode 600), holds all **1,620** rows (**1,481** in the 2000–2025 window), and `efp export --dry-run` passes F1 on all 1,620. You approved that run; I printed only counts, never names or row text. Three findings from the real file need your decision before later builds: the dropdown that openpyxl silently drops (decision 2), the `Criminal Conviction?` rows (decision 3), and the multi-URL Source cells (decision 5).

## What this build set out to do

Load the Heritage workbook into `rows` once, immutably; prove the nine source columns survive a round trip (F1/F10); freeze the 50-row sample. Header found by content, row hash defined once and documented, in-scope flag from the year window only, idempotent re-run, a minimal `efp export --dry-run`, and a synthetic workbook generator.

Changes from the approved plan: after the real `--check` run, ingest and the dry-run report URL-bearing Source cells, duplicate rows and dropdown survival (decisions 2, 5, 6); the independent review added formula refusal, race-safe ingest and a tighter federal classifier, which meant redrawing the sample once (decision 11).

## What was built

- **`efp/rowhash.py` + `docs/row_hash.md`:** the one hash implementation (ingest, export, `selftest --db`). Cells are canonicalised by type, so `2016` and `"2016"` differ while `None` and `""` match; `\x1e`/`\x1f` are reserved so the encoding is injective; whitespace is never normalised.
- **`efp/ingest/reader.py`:** read-only open; header found by content in the first 50 rows; all nine headers required; any extra non-empty header, repeated header, blank row inside the data, data outside the nine columns, bad Year/State/Name, unsupported cell type, or **formula cell** is refused. Every problem is collected and reported together; nothing is written.
- **`efp/ingest/loader.py`:** manifest check first (both hashes printed on a mismatch); one `BEGIN IMMEDIATE` transaction writes `rows`, `case_stage` and `source_workbook`; whether `rows` is empty is decided inside that transaction; a repeat run is a no-op only if the workbook hash, year window and every row hash match, otherwise it is refused.
- **`efp/ingest/sample.py`:** the frozen sample (`tests/fixtures/sample_rows.json`): strata year band × federal/state × fraud type (top five + other), one row per non-empty stratum plus proportional, selection by `SHA-256(seed|row_id|row_sha256)` (deterministic and independent of the Python version), never overwritten.
- **`efp/ingest/report.py`:** counts only.
- **`migrations/0004_source_workbook.sql`:** one immutable row (workbook hash, sheet, header row, row count, year window), same triggers as `rows`.
- **`efp/export/roundtrip.py`, `efp export --dry-run`:** F1 (see decision 4).
- **`efp status`** (minimal), **`efp ingest [--check]`**, audit `args_json` now an object `{argv, population, ...}`, `selftest --db` recomputes every row hash from the workbook (13 checks).
- **Shared:** `efp/scope.py` (`population_sql`, applied at query time), config key `scope.sample_file`, `openpyxl>=3.1` (core) and `types-openpyxl` (dev). `tests/fixtures/make_workbook.py` (20 invented rows, a larger seeded variant, an `x14`-dropdown variant).
- **Results:** `uv run ruff check .`, `ruff format --check .`, `mypy efp` pass; `uv run pytest -m "not live"`: **447 passed**, 96% coverage; `pre-commit run --all-files` passes (the scanner skips `sample_rows.json`, whose content is SHA-256 row hashes by design).

## What was run

| Run | Rows attempted | Rows resolved | Wall clock | Requests (per domain) | API spend |
| --- | --- | --- | --- | --- | --- |
| Real workbook, `efp ingest --check` | 1,620 | 1,620 read, 0 written | seconds (not timed) | 0 | $0 |
| Real workbook, `efp ingest` | 1,620 | 1,620 loaded; 1,481 `in_scope` | about a second (a synthetic 1,481-row run took 0.4 s) | 0 | $0 |
| Real workbook, `efp export --dry-run` | 1,620 | 1,620 verified (F1 PASS) | about a second | 0 | $0 |
| 50-row sample | 1,481 eligible | 50 drawn | n/a | 0 | $0 |

## Results by source

Measured on the real workbook (sha256 `85936794…c525b484`, sheet `heritage-voter-fraud-map-search`, header on sheet row 4, a second sheet `Sheet1` holds the dropdown's four choices and is ignored):

| Measure | All rows | In the 2000–2025 window |
| --- | --- | --- |
| Rows | 1,620 | **1,481** |
| Source cell non-empty | 1,619 | 1,480 |
| **Source cell contains an http(s) URL** | 1,398 | **1,322** (159 without) |
| Source cells carrying a hyperlink object | 0 | 0 |
| Rows identical to another row in all nine columns | 127 | not measured separately |

Rows by year band and case type (all loaded rows):

| Case type | before 2000 | 2000–09 | 2010–16 | 2017–25 | total |
| --- | --- | --- | --- | --- | --- |
| Civil Penalty | 7 | 17 | 24 | 2 | 50 |
| Criminal Conviction | 128 | 308 | 536 | 408 | 1,380 |
| Criminal Conviction? | 0 | 2 | 0 | 0 | 2 |
| Diversion Program | 0 | 31 | 55 | 49 | 135 |
| Diversion Program? | 0 | 3 | 0 | 0 | 3 |
| Judicial Finding | 4 | 3 | 5 | 13 | 25 |
| Official Finding | 0 | 3 | 7 | 15 | 25 |

In the window, the plain case types match `research-plan.md` exactly (1,252 convictions, 135 diversion, 43 civil, 25 official, 21 judicial); the five `?` rows are the 229 − 224 gap noticed in the docs.

Frozen sample (seed 20261027, `sample_sha256` `025a50c2…`): 50 rows in 23 non-empty strata (of 36 possible). Sampled by band and jurisdiction: 2000–09 federal 2, state 12; 2010–16 federal 0 (none classified), state 18; 2017–25 federal 3, state 15. Top fraud types: fraudulent use of absentee ballots, false registrations, ineligible voting [felon voter], duplicate voting, buying votes. Strata (population → sampled): 2000–09 federal absentee 9→1, federal other 1→1, state buying 54→2, duplicate 23→1, false registrations 81→3, absentee 65→2, ineligible 19→1, other 115→3; 2010–16 state buying 51→2, duplicate 56→2, false registrations 96→3, absentee 118→3, ineligible 144→4, other 162→4; 2017–25 federal false registrations 1→1, absentee 1→1, other 5→1, state buying 7→1, duplicate 64→2, false registrations 44→2, absentee 66→2, ineligible 40→2, other 259→6.

## Failures by reason

| Reason | Rows | Example row_id | Action taken |
| --- | --- | --- | --- |
| None in the real runs | 0 | n/a | n/a |
| One deliberate refusal: after the classifier fix, `efp ingest --check` refused because the committed sample differed from a fresh draw | n/a | n/a | the sample was redrawn on purpose (decision 11) |

## Gated sources encountered (not worked around)

None.

## Numbers for the first-pass report

- Link survival denominator: **1,322** in-window rows have a source URL; **159** have none. (**725** in-window rows contain more than one URL, up to 8, measured with a one-off script, not part of the committed tooling.)
- **158** in-window non-empty Source cells contain no URL (short notes of 12–101 characters).
- Population: 1,481 rows; 1,252 plain convictions (1,254 with the rule in decision 3).

## Spot-check guide for the operator

```bash
uv run efp ingest ~/efp-data/source/heritage-voter-fraud-map-search-20260406150111.xlsx --check   # counts only; writes nothing
uv run efp status            # every stage pending for 1,481 rows
uv run efp export --dry-run  # F1, plus the dropdown warning
uv run efp selftest --db     # 13 checks, including a recomputation of every row hash
```

If `uv run efp` fails with `No module named 'efp'`, see decision 13. Open `tests/fixtures/sample_rows.json` and confirm it holds only `row_id`/`row_sha256`. Look at the 23 strata above and ask whether the federal/state split looks right (decision 4).

## Decisions needed from the operator

1. **The real database now exists.** `~/efp-data/pipeline.db` (mode 600, 2.4 MB) and a committed `tests/fixtures/sample_rows.json`. To start over: delete the database, delete the sample file, ingest again.
2. **The Ideological Motivation dropdown will not survive openpyxl.** It is a list validation on `H5:H1624` whose choices are `Sheet1!$A$1:$A$5`, stored as an Excel `x14` extension; openpyxl discards those on save, so a plain load/save of the workbook loses it (`export --dry-run` now says so). F1 itself is unaffected (cell values are intact), but build 19 must preserve the dropdown. Options: (a) edit the sheet XML directly; (b) save with openpyxl and re-inject the original `<extLst>` block into the saved sheet, verified by the same validation count; (c) recreate an ordinary list validation with the same range (works in modern Excel; not byte-original). Default: nothing in this build; build 19 should take (b) and verify it. The lookup sheet `Sheet1` must be kept.
3. **"Begins with Criminal Conviction" counts two uncertain rows.** The prompt's rule gives 1,254 in-window convictions; `research-plan.md` says 1,252, because `Criminal Conviction?` (2 rows) is a different label. `Diversion Program?` (3 rows) is similar. Default: the prompt's rule, case-sensitive (`GLOB`), in `efp/scope.py`. Switching to an exact match is a one-line change and nothing is stored, so it re-queries instantly.
4. **The federal/state classifier is a literal, conservative rule.** Federal means the Source or Details names a U.S. Attorney, the U.S. Department of Justice, a U.S./federal court, `justice.gov` or `usao`. Bare "Department of Justice"/"DOJ" does not count (Wisconsin, California and New Hampshire call their state agencies that; the reviewer found this). It marks **17** of 1,481 rows federal, all from the Details text (the Source cells are news links, none from `justice.gov`). Before the fix it marked 18; a wider rule (federal judge/jury/prosecutor/charges/indictment, FBI, U.S. Marshals) would add about 21 more, and about 49 rows that merely mention "federal" would still be unclassified. Default: literal (it claims less). It only shapes the sample's strata; redrawing is "delete `sample_rows.json`, run `efp ingest`", but only before anyone codes the 50 rows.
5. **Source cells are plain text, often several URLs.** No hyperlink objects exist, so the URL is the text; 725 in-window rows hold more than one (up to 8), and 158 hold only a note. Build 06 must split on URLs; ingest keeps the cell verbatim.
6. **127 rows are exact duplicates** of another row in all nine columns. `row_sha256` is therefore not unique and is not a key; `row_id` is. Nothing is merged (research plan rule 1). Worth a thought for identity resolution and for counting incidences.
7. **Both ideological columns are empty in every row** (Ideological Motivation and Ideological Source), so the "as received" columns carry nothing and the dropdown has no values yet.
8. **Ingest loads every data row.** The docs disagree (1,620 vs 1,481); the file has 1,620 rows, 1,481 in the window, so `rows` holds all 1,620 with `in_scope` marking the window, and status and the runner use the in-window population.
9. **Design choices from the plan, all kept:** a `source_workbook` table (migration 0004) instead of audit-only provenance; `audit_log.args_json` is an object `{argv, population, ...}` (build 02's audit tests were updated); a minimal `efp status` now; the dry-run round-trips through openpyxl's writer rather than writing DB values over cells (the DB's typed `year` cannot reproduce a text cell); strict, collect-everything failures; the year window comes from config and is stored with the workbook; `openpyxl` is now a core dependency; the sample is never overwritten and carries a `sample_sha256`; `scope.sample_file` is a new documented config key.
10. **Doc discrepancies, not changed by me:** `research-plan.md` 224 vs 229 (explained by decision 3); `build/04-runner.md` lists `news` before `documents`, `config.toml` has `documents` before `news`.
11. **The sample was drawn twice.** The first draw (`sample_sha256` `e8fcf0eb…`) used a classifier that counted state Department of Justice offices as federal. Nothing had used it. I deleted the file and re-ran the no-op ingest, which is the only way the frozen-sample guard allows a redraw; the new hash is `025a50c2…`.
12. **The secret scanner skips `tests/fixtures/sample_rows.json`** (50 SHA-256 row hashes read as "high-entropy hex"). One named file, with the reason in `.pre-commit-config.yaml`.
13. **Environment, unchanged from build 02:** the checkout is in an iCloud-synced folder; macOS keeps hiding the `.pth` files in `.venv`, which breaks `uv run efp` (tests still pass). `chflags nohidden .venv/lib/python3.12/site-packages/*.pth`, or keep the venv outside Documents (`UV_PROJECT_ENVIRONMENT`). The `README 2.md`-style files are iCloud conflict copies, left alone. **Resolved after build 03:** the repository moved to `~/dev/efp`, outside iCloud, and the virtual environment no longer sits in a synced folder; see the README "Developing" section.
14. **Audit gap on the very first ingest.** The database does not exist until ingest creates it, so that one row is written after the load; every later ingest writes its row before changing the database, so a crash leaves a trace. `selftest --db` reports a skipped check (workbook not found) as `ok` with "skipped" in its detail.
15. **CI.** `.circleci/config.yml` runs on the PR; GitHub Actions was blocked by the billing lock in build 01.

## Done-when check

1. *"`uv run efp ingest ~/efp-data/source/<workbook>.xlsx` loads 1,481 rows, prints the count by year band and case type, and `efp status` shows every stage `pending`."* **Holds, with one clarification.** The file has 1,620 rows; all are loaded and 1,481 are `in_scope` (decision 8). The year-band × case-type table is printed (above). `efp status` shows all eight stages with 1,481 `pending` and nothing else.
2. *"`uv run efp export --dry-run` passes F1 on the real workbook and the report states the measured counts: rows in the year window, rows with a non-empty Source cell (expected 1,322), and the strata sizes of the frozen sample."* **Holds.** F1 PASS, 1,620 rows verified against `rows.row_sha256`. Rows in the window: 1,481. The expected 1,322 is the number of in-window rows whose Source **contains a URL**; rows with a *non-empty* Source cell number 1,480 in the window (1,619 overall), so the expectation was about links, not non-empty cells. Strata sizes are listed above.

Independent review: `done-when-reviewer` (which was told not to touch the real data) returned "not ready to report" with 7 findings. Fixed: the federal classifier counting state agencies (and missing a few federal-court phrasings), formula cells being accepted (they can never survive F1), a race between concurrent ingests, raw tracebacks for a non-xlsx file or a failing sample write, the case-insensitive conviction match, the audit row being written last, the Unicode-digit year message, `status --stage` accepting nonsense, BOM manifests, and drift in `docs/row_hash.md`. Left as is: a skipped check reported as ok (decision 14) and the heuristic nature of the classifier (decision 4). I did not re-run the reviewer after the fixes; they are covered by 24 new tests (447 total) and by re-running `--check`, `ingest`, `status`, `export --dry-run` and `selftest --db` on the real workbook afterwards.

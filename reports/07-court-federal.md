# Build 07 — Federal court record: DOJ, CourtListener, RECAP

Branch: `build/07-court-federal` (off `main` at e581ccb; build 06 is merged, nothing stacked) · PR: see the pull request for this branch · Date: 2026-11-21 · Model: claude-sonnet-5-5 · Prompt: `build/07-court-federal.md` · Spec sections: 4 (court), 5 (DOJ, CourtListener, RECAP, PACER), 1 (F3)

## What this build set out to do

Locate the primary record for each row: a DOJ press release, a Public Integrity Section report entry, or a federal docket (CourtListener bulk index first, REST second, RECAP-archived filings captured as PDFs), extract identifiers with verbatim spans, and say per row and per strategy what was attempted. Nothing is fetched from PACER.

Changes the approved proposal made to the prompt:

- **Three-way classification.** Build 03 found only 17 in-window rows with federal cues, so "federal, otherwise state" would send nearly every row down a path that does not exist until builds 08 and 09. A row is `federal` (positive federal signals), `state` (positive state signals only) or `unknown`. `unknown` rows get the federal strategies too, because those are local and cheap. The classification and its basis are in `summary_json`.
- **Header-driven bulk loader.** The CourtListener bulk file names and columns are not documented in this repository and the build environment has no network. The loader finds each column by name (with aliases) and stops, listing the columns it actually saw, if one is missing.
- **PIN report URLs are not supplied.** No project document lists them and none were invented; `court.pin_report_urls` is empty until you set it.

## What was built

- `efp/court/` — `__init__.py` (the stage: classify, run the chain, persist, queue, summarise), `classify.py`, `names.py` (in-house name parser and matcher; the installed `nameparser` 3.x has a different API), `identifiers.py`, `located.py`, `doj.py`, `pin.py`, `bulk.py`, `courtlistener.py`, `recap.py`, `modules.py` (registers all seven modules; `court.state_ag` and `court.state_docket` report "not built until build 08/09").
- `efp/capture.py` — `Capture.download` (streams to disk, resumable, hashed; the `captures` row's body is a JSON manifest, not the file; redirects are refused) and `Capture.wait_limit` (fail fast on a daily quota).
- CLI: `efp court bulk fetch [--yes]`, `load`, `status`; `efp selftest` reports the bulk index (dataset date, row counts), the API token, the PIN URLs and the RECAP gate; `efp selftest --live` adds one DOJ query and one CourtListener search; `efp status` gains the court-status distribution with federal, state and unknown apart.
- Config: a new `[court]` section (documented in `config.example.toml`). Docs: `docs/sources.md` (every source, its budget, and which details are assumed), RUNBOOK, `queues/README.md`.
- Tests: `test_court_text.py`, `test_court_doj.py`, `test_court_bulk.py`, `test_court_stage.py`, `test_court_cli.py`, `test_court_review.py` and `courthelpers.py` (a few hundred invented bulk rows with planted targets and decoys). Existing tests that used `court` as the "no component yet" stage now use `identity`.
- Results: `uv run pytest -m "not live"`: 810 passed, 1 deselected. `ruff check`, `ruff format --check`, `mypy efp`: clean. Coverage 93% overall (`doj.py` 95%, `bulk.py` 94%, `identifiers.py` 98%).

How the chain behaves: seven modules in the order `config.toml` lists, policy `first_hit`. A module stops the chain only when it locates a document **with a case number**; a release without one is recorded and the chain goes on. Every module's outcome, hit, miss or skipped-with-reason, is in `summary_json.modules`; `court_records.strategy` is the module that found each document; a row nothing resolves is `done` with `court_status = not_found`.

## What was run

| Run | Rows attempted | Rows resolved | Wall clock | Requests (per domain) | API spend |
| --- | --- | --- | --- | --- | --- |
| 50-row sample | 0 | 0 | n/a | none | $0 |
| All rows | 0 | 0 | n/a | none | $0 |

Nothing was fetched from the real network and `~/efp-data` was not changed (this session has neither). **The bulk index is not loaded, the court-status distribution is not measured, and `queues/pacer.csv` does not exist yet.** Commands to produce them:

```bash
uv run efp court bulk fetch              # lists the newest files and sizes; downloads nothing
uv run efp court bulk fetch --yes        # downloads (refuses above court.bulk_max_gb = 150)
uv run efp court bulk load               # builds ~/efp-data/bulk/courtlistener.duckdb
uv run efp selftest                      # shows the index's dataset date and row counts
uv run efp run court --sample            # then the full run, detached
uv run efp status                        # court status, federal / state / unknown apart
```

## Results by source

No live requests. `efp modules --stage court` lists the seven modules with their availability and reasons.

## Failures by reason

None in the build's own runs. Independent review (`done-when-reviewer`) found 18 issues; all but two are fixed and tested (the two are decisions 6 and 7):

| Finding | Severity | Fix |
| --- | --- | --- |
| The CourtListener fast-fail wait (1 s) was shorter than the 1.25 s gap the 0.8/s budget needs, so the RECAP documents step failed almost every time and was recorded as "daily quota spent" | High | Wait is 5 s: only a spent *daily* budget fails fast. Tested under the real limiter |
| The DOJ title search read only the newest 50 releases and never asked for aliases | High | Pages back to the row's year (up to 6 pages per name) and searches each alias surname |
| One defendant's age, city and case number were recorded for another in a multi-defendant release | High | The identifier window stops at the next person's "Name, NN,"; with several case numbers only those in the subject's own sentences are taken, and the record is `weak` |
| "John told May" matched "John May"; "J. Smith" matched "Jane Smith" without a flag | Medium | Middle words are case-sensitive; initial-only matches are flagged |
| "West Virginia" corroborated Virginia; a Washington, D.C. dateline corroborated Washington | Medium | Fixed with tests |
| Every hit from the index was non-weak even when a name matched several dockets | Medium | `weak` when there is more than one hit, a score under 3, an initial-only match, or an unknown state |
| Dockets past the first were queued as "not in RECAP" without being checked | Medium | Queued as "documents not checked: only the best candidate per row is looked up" |
| Why a record matched was not visible to the identity build | Medium | `summary_json.located` now carries the basis, capture id and URL |
| A malformed docket id aborted a multi-hour load; a failed load left a WAL file | Medium | Bad rows are skipped and counted; a failed load removes its partial files |
| A finished download was fetched again every run; a redirect left the host unchecked | Medium | Skipped when the file is complete and recorded; redirects are refused |
| `court.courtlistener_api` was available without the index, spending the daily budget on rows the index could answer | Medium | Unavailable until the index is loaded ("bulk before API") |
| `request_docket`, a callable PACER purchase POST carrying the password in its body, existed though unused | Low | Deleted; it will be written when the written clarification exists |
| `efp court bulk load` could mix files from different quarters | Low | Uses one complete dataset date |
| The queue sat in the repository tree | Low | A relative `court.pacer_queue` resolves under `paths.data_dir` (decision 5) |
| A DOJ client per row repeated the title-honoured probe | Low | Cached on the capture client |
| LIKE wildcards in a surname were not escaped | Low | Escaped |
| The docstring claimed nothing was held in memory | Low | Corrected (only the kept ids are in memory) |

One bug in build 05 surfaced and was fixed here: a daily cap of exactly 1 produced an invalid rate list in the limiter (regression test added).

## What the court stage does, after twenty review rounds

This PR went through twenty rounds of review findings (each fixed with tests). The commits record the order; this section is the resulting behaviour, which is what to review and to build on. Nothing here is measured against the real network or data: it is tested on synthetic fixtures.

**Statuses.** `court_status` has eight values. `primary_record` needs a docket that is not weak; `candidate_only` is a weak docket, or only weak or report-only records; `press_release_only` needs a record that is not weak. Three values mean the search did not finish, are never negatives, and win over `press_release_only`: `deferred_error` (a module raised, or the API or DOJ listing failed), `deferred_quota` (the CourtListener daily budget was spent or a 429 came back, a cap of 0 included) and `deferred_coverage` (the DOJ harvest did not reach the row's years). `gated_portal` is set by build 09; `not_found` means every applicable strategy completed and found nothing. The plan's four are a subset.

**Deferral and `--recheck`.** A deferred row finishes `done` with an input hash that carries a suffix (`:quota`, `:error`, `:recap`, `:coverage`), so it never matches the recomputed hash and `efp run court --recheck` revisits exactly those rows. The base hash holds the row's inputs, the modules that can run, the bulk quarter, the PIN URLs and, only for rows the DOJ module can apply to and only while the feed's newest date is inside their release window, that date.

**Weak records.** One rule (`docket_is_weak`) decides it for the bulk and API paths: unknown row state, unknown court state, several matches (counted before `top_candidates` truncates), an initial-only match, an unknown filing date, or a bulk score under 3. A weak result is recorded but is a *miss* for its module, so the chain goes on. A weak record gives no person-level identifier (only its case number) and is never looked up in RECAP or queued for PACER.

**Records and readers.** Migration 0008 adds `weak`, `basis_json`, `docket_id`, `current` and `text_capture_id` to `court_records` and three views. A docket is identified by its CourtListener docket id and a non-docket record by a stable `identity` (a DOJ release's uuid, else its text or capture hash); a record found again is updated in place and one a strategy ran for and no longer finds is `current = 0`, never deleted. **Builds 08 and later read `court_records_current`, `court_records_confirmed` and `court_identifiers_current`, never the tables:** a filing is only as current and confirmed as its docket, and an identifier only as current as the record it was read from.

**Capture layer.** `Capture.download` honours `robots.txt`, retries 429/5xx, resumes only under a strong ETag or a `Last-Modified` date (`If-Range`), restarts from zero otherwise or after a 416 that is not "the part is the whole file", resumes a connection broken mid-stream a bounded number of times, and refuses to follow a redirect. `Retry-After` is honoured only when another attempt follows. `wait_limit` and the 429 memory are per-run and typed (`mark_throttled`, `once_per_run`, `clear_throttles`, called at the start of each run). A `rate_limits.daily_caps` value of 0 means the domain is switched off: no request is made and rows needing it are deferred.

**DOJ.** The title search is authoritative only if one of its releases survives the name match, and falls back to the date-sorted harvest when it is empty, capped or unmatched. A row fetches at most `court.doj_harvest_pages_per_row` harvest pages; `efp court doj harvest` walks the whole range once (run it before `efp run court`). The harvest refreshes the feed's fresh end once per run, and a harvest that stops short of the row's window defers the row. PIN reports are read once per reader (name and version); a failed fetch is tried once per run.

**Bulk loader.** It refuses a courts file with no federal court, counts short rows, fails cleanly on a truncated or malformed file, and refuses to load a file whose size and SHA-256 differ from its download manifest (or that has none), so every bulk-derived record names a real manifest capture.

## Gated sources encountered (not worked around)

| Source | What it wants | Rows affected | Where logged |
| --- | --- | --- | --- |
| PACER / RECAP Fetch | An account, per-page fees, and a written answer on the fee exemption versus RECAP's public uploads | every federal docket whose filings are not already in RECAP | `efp selftest`, `court.recap` availability reason, `queues/pacer.csv` |
| CourtListener EDU tier | A membership application by email | none: the 125/day default is used | not used |
| PIN reports | The operator's list of report URLs | pre-2013 federal rows have no fallback until they are set | `efp selftest`, `court.pin_reports` availability reason |

## Numbers for the first-pass report

Two of the plan's nine numbers draw on this stage. Primary-record rate: rows with a docket or filing, press release only, gated portal, none, federal and state separately (`efp status`). Identifier yield: rows with a date of birth or age, a city, a case number (`identifiers`). **Neither is measured yet.**

## Spot-check guide for the operator

- `uv run efp run court --sample`, then `SELECT r.row_id, cr.source_kind, cr.case_number, cr.court, cr.strategy FROM court_records cr JOIN rows r USING (row_id)` and open a few captures.
- Start with records flagged `weak` in `case_stage.summary_json` (`$.located[*].weak`, with `basis`): they are the ones most likely to be a different person with the same name.
- Check a row with a common surname: the index returns up to five dockets and all are weak by design.
- Read a few identifiers against their captured text: each `span` is a verbatim substring.

## Decisions needed from the operator

1. **Three-way classification** (approved). Unknown rows get the local federal strategies; only positively-federal rows may use the REST budget and the RECAP documents step.
2. **Assumed details, in `docs/sources.md`:** the S3 bucket address, bulk file names and CSV columns, the DOJ `parameters[title]` parameter and the CourtListener v4 search parameters. The first real run is the check; the loader and the DOJ client fail loudly or abstain rather than guess. If the loader stops on a column, send its message back: the alias table in `efp/court/bulk.py` is a one-line fix.
3. **PIN report URLs** (approved as empty): supply them in `court.pin_report_urls` and install `pdfplumber` or `pypdf` (the `documents` extra).
4. **Bulk size guard.** `court.bulk_max_gb = 150` is my guess at a ceiling for "tens of gigabytes"; `efp court bulk fetch` prints the real sizes first and downloads nothing without `--yes`.
5. **PACER queue location.** The prompt names `queues/pacer.csv`, but it lists row ids with case numbers and CLAUDE.md keeps row-level data in `~/efp-data`. The default is `court.pacer_queue = "queues/pacer.csv"` resolved under `paths.data_dir`, i.e. `~/efp-data/queues/pacer.csv`. Set an absolute path to put it elsewhere.
6. **The DOJ RSS fallback (`field_component`) is not built.** The prompt calls it a coverage check, not the primary path. When the server ignores the title parameter, the client harvests the date-sorted feed into a local index instead. Say so if you want the per-office feeds as well.
7. **Loader speed.** The loader streams CSVs row by row in Python and inserts in batches; at tens of gigabytes that is hours, not minutes. DuckDB's own `read_csv` would be faster but needs decompressing the `.bz2` files first (DuckDB does not read bz2) and a confirmed column list. Worth switching if the first real load is too slow.
8. **RECAP Fetch** is never enabled and there is no purchase function. Its availability needs `court.recap_enabled = true` and PACER credentials; even then `run()` only records a note.
9. **Weak flags.** A record is `weak` when the state is not corroborated, only an initial and the surname matched, the document names several case numbers, the index returned several dockets, or the state is unknown. Weak records are still recorded, now with `weak` and `basis_json` columns on `court_records` (migration 0008); the identity build decides. A row with only weak dockets has `court_status = candidate_only`, not `primary_record`, and a weak-only bulk or API result is a `miss` so the chain goes on to the modules that can confirm it.
10. **Name matching** is an in-house parser, not `nameparser`. It handles given/middle/surname/suffix, "Last, First", particles, nicknames and aka; it has no knowledge of cultural name orders beyond that.
11. **Identifier extraction is windowed to the subject's sentence** and abstains rather than guess when a release names several defendants and the facts cannot be attributed. This lowers yield on multi-defendant releases by design.
12. **Downloads** are recorded as manifests, not copies; the raw files stay in `~/efp-data/bulk/` (off backup) and a download refuses to follow a redirect.
13. **Two local indexes** live under `paths.bulk` (`doj_releases.sqlite`, `pin_reports.sqlite`): derived, rebuildable, every page behind them is a capture.
14. **Bulk downloads honour `robots.txt`.** (An earlier version treated the S3 bucket as a published dataset and skipped the check; `CLAUDE.md` says every network call honours it, so the check is made and a disallow is recorded and refused.)
15. **API quota.** A row the CourtListener API could not be asked about (daily budget spent) finishes `done` with `court_status = deferred_quota` and an input hash that never matches, so `efp run court --recheck` revisits exactly those rows once the budget resets.
16. **The row's `state` is free text.** `efp.court.doj.normalize_state` accepts a postal abbreviation or a full state name and treats anything else (blank, "N/A", "Unknown", "Multiple") as unknown. An unknown state applies no state filter and makes every docket match weak; before, a junk value was used as a filter and matched nothing. Ingest does not normalize the column; I could not read the real database to see which values occur, so this is a guard, not a fix for a known value.

## Done-when check

- *"The bulk index is loaded and `efp selftest` reports its row counts and the quarter it came from."* The code path and its report are tested (`test_selftest_reports_the_bulk_index_and_the_court_sources`, `test_fetch_with_yes_downloads_each_file_and_load_builds_the_index`). **The real index is not loaded;** commands above.
- *"`uv run efp run court --sample` shows per-strategy results on the 50 rows; the full run (detached) gives every in-scope row a logged attempt for every applicable federal strategy, and `efp status` reports the court-status distribution, federal and state separately."* Holds on synthetic data end to end (`test_the_runner_drives_the_court_component_and_a_second_run_is_a_no_op`, `test_status_reports_court_status_with_federal_and_state_apart`); every module's outcome is recorded per row. **Not run on the real data.**
- *"`queues/pacer.csv` lists every federal docket whose documents are not in RECAP."* The queue is written and de-duplicated (`test_a_docket_with_no_documents_in_recap_goes_to_the_pacer_queue_once`, `test_weak_candidates_are_neither_looked_up_in_recap_nor_queued_for_purchase`). Dockets whose documents were not checked (a non-federal classification, no token, a spent daily budget, or beyond the best candidate per row) are listed with that reason rather than as "missing". **The real queue does not exist until the run.**

Review checkpoint (yours): the court-status distribution and the weak records, once the run has produced them.

## Operator decisions

Filled in by the operator at merge time. Pre-filled only where a later report explicitly records the decision (quoted, with its source); every other item is blank.

1. Decision: ______ · Date: ______
2. Decision: ______ · Date: ______
3. Decision: ______ · Date: ______
4. Decision: ______ · Date: ______
5. Decision: ______ · Date: ______
6. Decision: ______ · Date: ______
7. Decision: ______ · Date: ______
8. Decision: ______ · Date: ______
9. Decision: ______ · Date: ______
10. Decision: ______ · Date: ______
11. Decision: ______ · Date: ______
12. Decision: ______ · Date: ______
13. Decision: ______ · Date: ______
14. Decision: ______ · Date: ______
15. Decision: ______ · Date: ______
16. Decision: ______ · Date: ______

## Carried forward

- operator; 10, 12: the CourtListener bulk-file details (S3 bucket address, file names, CSV columns), the DOJ `parameters[title]` parameter and the CourtListener v4 search parameters are assumed (listed in `docs/sources.md`); the first real run is the check, and the loader stops listing the columns it saw if one is missing (decision 2).
- operator; 10, 12: `court.pin_report_urls` is empty (no project document lists the Public Integrity Section report URLs) and reading them needs `pdfplumber` or `pypdf` (the `documents` extra); until both are set, pre-2013 federal rows have no fallback (decision 3).
- 09, 12, 19: builds 08 and later read `court_records_current`, `court_records_confirmed` and `court_identifiers_current`, never the tables; a filing is only as current and confirmed as its docket, and an identifier only as current as the record it was read from ("Records and readers").
- 09: `court_status` has eight values; `gated_portal` is reserved for build 09, and `candidate_only`, `deferred_error`, `deferred_quota` and `deferred_coverage` mean the search did not finish or only candidates were found, never a negative.
- 12: a record is `weak` when the state is not corroborated, only an initial and the surname matched, the document names several case numbers, the index returned several dockets, or the state is unknown; `summary_json.located` carries the basis, and the identity build decides what a weak record is worth. A weak record gives no person-level identifier, only its case number (decision 9).
- 12, 19: the row's `state` column is free text; court code uses `efp.court.doj.normalize_state`, ingest does not normalize it, and the real values were not inspected (decision 16).
- 10: filings already in RECAP are captured as PDFs (court records with `document_kind = 'filing'`) for the documents stage; nothing is fetched from PACER, and `court.recap_enabled` stays false with no purchase function (decision 8).
- operator; 09: `queues/pacer.csv` resolves under `~/efp-data/queues/` and lists federal dockets whose filings are not in RECAP; a PACER purchase function is written only after the written answer on the fee exemption versus RECAP's public uploads exists (decisions 5, 8).
- 08-16: `rate_limits.daily_caps` of 0 switches a domain off (no request is made and rows needing it are deferred), and a deferred row is revisited by `--recheck` through the suffix on its `input_hash` (`:quota`, `:error`, `:recap`, `:coverage`).
- 19, 21: the primary-record rate and the identifier yield (two of the nine numbers) are not yet measured; they come from `efp run court` on the real data and `efp status`.
- 20: the raw bulk files stay in `~/efp-data/bulk/` (excluded from backup) and each download is recorded as a manifest capture, which `efp court bulk load` verifies by size and SHA-256 (decision 12).

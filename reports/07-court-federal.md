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

## Follow-up fixes after PR review

Three issues were raised on the PR and are fixed with tests:

| Issue | Fix |
| --- | --- |
| A bulk download wedged forever on a complete or oversized `.part` (the retry asked for a range past the end and got 416) | A `.part` already the expected size is finalized without a request; one longer than the file is discarded; a 416 for a shorter stale part discards it and restarts from zero in the same call; a 206 whose `Content-Range` does not start at the offset is discarded; a wrong-sized finish removes the part so it cannot wedge the next try |
| A weak or ambiguous docket match produced `court_status = primary_record`, and a weak bulk hit ended the `first_hit` chain so the API and state modules never ran | `court_status` is `primary_record` only for a docket that is not weak; a row with only weak dockets is `candidate_only`. Every module now returns *hit* only for a non-weak result with a case number, so a weak match is recorded but the chain goes on (DOJ, bulk and API; PIN entries are always weak, so that module never stops it). `court_records` gained `weak` and `basis_json` (migration 0008), so a reader of the table sees the candidate and the evidence |
| A row whose API lookup hit the daily quota finished `done` / `not_found` and escaped `--recheck`; the quota was detected by substring; malformed JSON failed the row | The quota is a typed `CaptureBudgetError` (also raised for HTTP 429). Such a row finishes with `court_status = deferred_quota` and an input hash that never matches, so `efp run court --recheck` revisits exactly those rows. A non-JSON CourtListener answer is a noted miss, not a failed row |

A second review round added three more, also fixed with tests:

| Issue | Fix |
| --- | --- |
| A module that raised (`run_chain` turns an exception into an error outcome and continues) left the row `done` / `not_found` with an unchanged hash, so a failure looked like a negative | The row's `summary_json.errors` lists the failures. With nothing located the status is `deferred_error`; with something located the finding stands. Either way the input hash gains `:error`, so `--recheck` retries it |
| A row without a year became `year = 0` and built invalid date bounds, which the bulk query rejects and the API would have spent a quota call on, with the failure swallowed | The DOJ, bulk and API modules are not applicable to a row with no year (with the reason), and the search functions refuse a non-positive year. (The schema makes `rows.year` NOT NULL, so this is a guard, not a known case) |
| The RECAP-documents step spent API quota on bulk hits, set no quota flag when the budget ran out, and queued the unchecked docket for PACER with a quota message as the reason | Only a docket that is not weak is looked up (a candidate that may be someone else's is neither looked up nor queued). A budget error sets the same `api_quota_exhausted` flag and `:quota` hash suffix as the docket search; any "could not look it up for now" outcome (budget, token, API error) adds `:recap` to the hash and is **not** queued, because its documents are unknown, not absent |

A third review round, also fixed with tests:

| Issue | Fix |
| --- | --- |
| `court_records` de-duplication ignored `weak` and `docket_id`, so a docket that turned weak on a later run kept its old "primary" row while `summary_json` said `candidate_only`; a changed API response inserted a duplicate | A docket is identified by its CourtListener docket id (not the search response's hash) and is **updated** when found again: `weak`, `basis_json` and its capture follow the latest run. A record a strategy that ran this time no longer finds is marked `current = 0` (never deleted; RECAP filings are kept, since their absence may only mean the archive was not asked). Migration 0009 adds `docket_id` and `current`; readers should use `current = 1` |
| `Capture.download` skipped `robots.txt` and the 429/5xx retry | It now checks `robots.txt` (a disallow is recorded and refused) and retries 429/5xx with a pause, as `fetch` does; a spent domain budget is a `CaptureBudgetError`. This **replaces decision 14** above: bulk downloads do honor `robots.txt` |
| `wait_limit` set one shared value on the adapter | It is thread-local, so a parallel fetch keeps its own budget wait |
| `court bulk fetch` chose the newest file of each kind independently, so mismatched dates could be downloaded and then rejected by `load` | `latest_set` returns the newest single date that has all four kinds (empty when none does), and the command says which kinds exist on which dates |

A fourth review round, also fixed with tests:

| Issue | Fix |
| --- | --- |
| The CourtListener API module (and the DOJ listing) turned an outage, a 5xx or a bad answer into a clean *miss*, so the row ended `not_found` with a hash `--recheck` never revisits | A non-budget `CaptureError` is now `Outcome.error`: the row is `deferred_error` (or keeps its finding) with `:error` on its hash. A spent budget stays a quota deferral |
| `_supersede` counted a quota-spent or failed API run as "evaluated" and retired earlier dockets | An errored module is never evaluated, and a spent budget removes the API and RECAP strategies from the evaluated set, so earlier records stay `current = 1` |

A fifth review round:

| Issue | Fix |
| --- | --- |
| The API module called a docket weak only when the row's state was unknown, so several dockets for a common name, an initial-only match or a court of unknown state were stored as confirmed (and the RECAP step then took filings from the first) | One shared rule, `efp.court.located.docket_is_weak`, is used by both the bulk and the API module: weak when the row's state is unknown, the court's state is unknown, several dockets matched, only an initial and the surname matched, or (bulk) the score is below 3. `search_party` still lets a docket of unknown court state through, but it is now a candidate |

A sixth review round:

| Issue | Fix |
| --- | --- |
| The bulk search had no lower filing-date bound when `date_terminated` was NULL, so a 2003 docket matched a 2020 row (the API path already used `year - 8`) | Both paths use one constant, `LOOKBACK_YEARS` (`efp.court.located`); bulk adds `date_filed >= year - 8`. A docket with no filing date is kept but weak (`docket_is_weak(date_known=...)`, applied in the API path too) |
| A daily cap of 0 was read as "1 a day" | The config rejects a cap below 1; the limiter raises on one and uses the replacement rate only for exactly 1 |
| A connection broken mid-stream in `Capture.download` escaped as a raw exception | It is caught: the download resumes from the `.part` file with a Range request, up to `rate_limits.retry_attempts` times with backoff, then ends as a `CaptureError` ("run the download again to resume") with the part kept |

A seventh review round:

| Issue | Fix |
| --- | --- |
| The DOJ title-probe miss counter was cumulative and could switch off a title search that had been shown to work | Misses are consecutive: a success resets the count, and a server that has honored the parameter once is never switched off |
| A capped DOJ harvest that stopped before the row's window gave a clean `not_found` for older rows | The row gets `court_status = deferred_coverage`, a `doj_coverage_gap` flag with the oldest harvested date, a note naming `court.doj_harvest_max_pages`, and `:coverage` on its hash, so `--recheck` revisits it after the cap is raised |

`court_status` therefore has eight values: `primary_record`, `candidate_only`, `press_release_only`, `gated_portal` (set by build 09), `deferred_quota`, `deferred_error`, `deferred_coverage`, `not_found`. The plan's four are a subset. `candidate_only` is "found a candidate, not confirmed", and the three `deferred_*` values are "could not finish" (the API budget was spent, a module failed, or the DOJ harvest did not reach the row's years): without them those rows would be counted as findings or as negatives.

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
14. **(Superseded by the third review round: downloads now check `robots.txt`.)** Bulk downloads did not check `robots.txt`. `Capture.download` (and the bucket listing, which is `kind="api"`) fetches the CourtListener S3 bulk files from the address the listing names, under the domain's rate budget and logged, but without a `robots.txt` check, as for any documented data endpoint. The default I took: treat the bucket as a published dataset, not a scrape target. Say so if you want the check added.
15. **API quota.** A row the CourtListener API could not be asked about (daily budget spent) finishes `done` with `court_status = deferred_quota` and an input hash that never matches, so `efp run court --recheck` revisits exactly those rows once the budget resets.
16. **The row's `state` is free text.** `efp.court.doj.normalize_state` accepts a postal abbreviation or a full state name and treats anything else (blank, "N/A", "Unknown", "Multiple") as unknown. An unknown state applies no state filter and makes every docket match weak; before, a junk value was used as a filter and matched nothing. Ingest does not normalize the column; I could not read the real database to see which values occur, so this is a guard, not a fix for a known value.

## Done-when check

- *"The bulk index is loaded and `efp selftest` reports its row counts and the quarter it came from."* The code path and its report are tested (`test_selftest_reports_the_bulk_index_and_the_court_sources`, `test_fetch_with_yes_downloads_each_file_and_load_builds_the_index`). **The real index is not loaded;** commands above.
- *"`uv run efp run court --sample` shows per-strategy results on the 50 rows; the full run (detached) gives every in-scope row a logged attempt for every applicable federal strategy, and `efp status` reports the court-status distribution, federal and state separately."* Holds on synthetic data end to end (`test_the_runner_drives_the_court_component_and_a_second_run_is_a_no_op`, `test_status_reports_court_status_with_federal_and_state_apart`); every module's outcome is recorded per row. **Not run on the real data.**
- *"`queues/pacer.csv` lists every federal docket whose documents are not in RECAP."* The queue is written and de-duplicated (`test_a_docket_with_no_documents_in_recap_goes_to_the_pacer_queue_once`, `test_dockets_beyond_the_first_are_queued_as_not_checked_not_as_missing_from_recap`). Dockets whose documents were not checked (a non-federal classification, no token, a spent daily budget, or beyond the best candidate per row) are listed with that reason rather than as "missing". **The real queue does not exist until the run.**

Review checkpoint (yours): the court-status distribution and the weak records, once the run has produced them.

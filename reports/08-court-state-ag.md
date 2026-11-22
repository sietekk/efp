# Build 08 — State AG and DA press-release harvester

Branch: `build/08-court-state-ag` (stacked on `maint/02-remove-robots`, PR #10, which is based on `main` after build 07 merged as #9) · PR: see the pull request for this branch · Date: 2026-11-22 · Model: claude-sonnet-5-5 · Prompt: `build/08-court-state-ag.md` · Spec sections: 6 (state AG and DA harvester), 4 (court sub-strategy 2), 11 (contract tests)

## What this build set out to do

Replace the `court.state_ag` stub of the court chain with a harvester of state attorney-general (and, later, district-attorney) press releases: a framework with a registry and the spec's `AGScraper` protocol, one swappable module per state for the ten states with the most rows (MN, TX, FL, NC, CA, WI, OH, KY, IN, IL), a coverage matrix, and cassette contract tests with a live smoke per module.

Changes the approved proposal and the operator's mid-build instructions made:

- **Stacked on the maintenance branch.** The operator retired `robots.txt` honoring and the politeness rules project-wide while keeping rate limiting (PR #10, `reports/m02-remove-robots.md`). This build is written for that: it never consults `robots.txt`, and a site that refuses a request is a `manual` state with the reason.
- **`broken` is a real mode.** Migration `0009_state_adapters_broken.sql` rebuilds `state_adapters` so `mode` also allows `broken` (decision taken at planning).
- **Site details were probed, not assumed**, through the capture layer on 2026-11-22; cassettes were recorded the same way.
- **The index is persistent, not only per run.** Each state's release index is a local sqlite file (`paths.bulk/ag_<state>.sqlite`) filled by a resumable harvest, with the lessons of build 07's DOJ harvest built in from the start: a per-row page budget, a freshness walk once per run, `deferred_coverage` instead of a clean negative, and an out-of-reach state for an archive that ends before the row's window.

## What was built

- `efp/court/adapters/ag/`: `__init__.py` (the protocol and the frozen `Release` dataclass verbatim, the registry), `base.py` (`StateAdapter`, `AgIndex`: fetching through the capture layer, Wayback fallback for a gone index, harvest, freshness, reach), `module.py` (the `court.state_ag` module), `sync.py` (`state_adapters`), `record.py` (cassette recorder), the state modules `tx.py ca.py oh.py il.py` and `manual.py` (the six manual states with their reasons), `states.py` (imports them all).
- `efp/coverage.py` and `docs/coverage_matrix.md`; `efp status --coverage [--write]`.
- CLI: `efp court ag verify | harvest | record <state>`.
- `efp selftest --live` runs one first-page smoke per automated adapter (`efp/livecheck.py`); a failure is recorded as `broken` (or `manual` if the site refuses) in `state_adapters` and shows in the matrix.
- Config: `court.ag_harvest_pages_per_row`, `court.ag_harvest_max_pages`, `court.ag_max_candidates` (documented in `config.example.toml`), and `rate_limits."oag.ca.gov" = 0.08` (80% of California's published Crawl-delay of 10 seconds).
- Court stage: the `court.state_ag` hash freshness is scoped like the DOJ one (only rows the module can apply to, only while the index's newest date is inside the row's window); `ag_coverage_gap` makes a row `deferred_coverage` and keeps the module out of the "evaluated" set so earlier records stay current.
- Tests: `tests/test_court_ag.py` (47 tests: every cassette, the registry and protocol, harvest, per-row budget, freshness, reach, the Wayback fallback, failed listings, a 403 recorded as manual, the matching rule, the module end to end, hash scoping, candidate ordering, the DA record kind, weak rules, the matrix, `broken`), a live test in `tests/test_live.py`, four cassettes in `tests/cassettes/ag/` (page tokens redacted).
- `uv run pytest -m "not live"`: 936 passed. `ruff check`, `ruff format --check`, `mypy efp`: clean.

How a row flows: `applies` skips a federal row, a state with no adapter, a `manual` or `broken` one (with its reason, so the chain moves on), and a state whose archive ends before the row's window. `run` asks the adapter for releases in the window with election wording in the title or snippet (listings carry no body), fetches up to `court.ag_max_candidates` of them, and a release is a hit when the subject is named in its title or opening (surname plus a given name or matching initial, or an alias), it carries an election charge, and its date is within `court.doj_year_slack` years of the row. Hits are `court_records` (`state_ag_release`, or `da_release` for a DA office) with identifiers extracted by the same code as the DOJ releases; the record is weak when only an initial matched, the text names several case numbers, or several releases matched.

## What was run

| Run | Rows attempted | Rows resolved | Wall clock | Requests (per domain) | API spend |
| --- | --- | --- | --- | --- | --- |
| Site probes (capture layer, throwaway database) | n/a | n/a | minutes | a handful per site: robots/index/pages for the ten states | $0 |
| `efp court ag verify` | n/a | 10 adapters recorded | seconds | one listing page per automated state | $0 |
| `efp selftest --live` (AG checks) | n/a | 4 of 4 ok | seconds | one listing page per automated state | $0 |
| `efp run court --sample` / full run | 0 | 0 | not run | none | $0 |

**The court stage was not run on the real rows**: this session has no `~/efp-data`, no dataset database and no frozen sample. The located-record rates by state and strategy are therefore not measured yet, and the coverage matrix's located columns are 0. To produce them:

```bash
uv run efp court ag verify                  # record the adapters, probe the automated ones
uv run efp court ag harvest                 # walk each automated state's archive into its index (resumable)
uv run efp run court --sample               # 50 rows first
nohup uv run efp run court > ~/efp-data/logs/court.log &   # then every row, detached
uv run efp status --coverage --write        # regenerates docs/coverage_matrix.md with every state
```

## Results by source

Live smoke, 2026-11-22 (`efp selftest --live`): TX 10 dated releases on the first page, CA 30, OH 10 (the current month), IL 1,183 (the whole archive on one page). DOJ and the Wayback CDX also answered in the same run.

| State | Mode | Why |
| --- | --- | --- |
| TX | automated | `texasattorneygeneral.gov/news/releases?page=N`, ten releases a page |
| CA | automated | `oag.ca.gov/media/news?page=N`, thirty a page; budget 0.08/s from the published Crawl-delay |
| OH | automated | by month, `/Media/News-Releases/<Month>-<YYYY>`; the cursor is the month |
| IL | automated | one page holds the whole archive, which reaches back only to 2024 (older rows are reported out of reach, not as negatives) |
| FL, NC | manual | HTTP 403 to automated requests |
| MN | manual | no archive or search index; releases are reachable only from curated pages |
| WI | manual | releases are PDF files only; reading them needs the optional PDF reader |
| KY, IN | manual | the lists are loaded by script; no server-rendered list, feed or archive |

## Failures by reason

None in the build's own runs (nothing ran on rows).

## Review findings (independent `done-when-reviewer`, all fixed)

| Finding | Fix |
| --- | --- |
| **High:** a listing that failed to load (429/5xx) was read as an empty page, so the archive was recorded as finished and later rows were skipped as out of reach or came out `not_found` | A listing that is not ok raises (only a genuine 404/410 is "no page"); nothing is saved as walked. OH's failed month is no longer skipped |
| The recorded mode of a state did not reach the input hash, so a row skipped while a state was `manual` or `broken` was never rechecked after `efp court ag verify` fixed it | The recorded mode is part of the hash for rows of a state that has an adapter |
| The candidate cap could push the subject's release out behind generic election news | Releases whose title or snippet holds the surname are read first; a truncation is flagged (`ag_candidates_truncated`) and noted |
| The freshness walk stopped after the per-row page budget and on an empty OH month, leaving a permanent gap | It walks until it reaches a known release (bounded by `ag_harvest_max_pages`) and an empty month does not end it |
| One 403 on a single release page marked the whole state `manual` | Only a 403 on a listing does; a refused release is that release's failure (and an error if every release failed) |
| The live smoke and `ag verify` could read cached pages, and OH was flagged `broken` on the first day of a month | `probe()` bypasses the cache and tries the next page when the first is empty |
| The election-charge test matched anywhere on the page | It must be in the title or the opening, the same stretch that names the subject |
| A 200 page that parsed to nothing (a layout change, a challenge page) marked the archive finished, and 12 empty OH months walked to the floor and did the same | An empty page is the end of an archive only when something is already indexed; an empty first page (or, for OH, a year of empty months) with nothing indexed raises and `done` is never set |
| After the first page, an empty parse (a 200 interstitial, a layout change on older pages) was stored as "archive exhausted" for TX and CA | TX and CA now need a positive end signal: a page with releases and a pager with no next link (`rel="next"` / `pager-next`), or a 404/410. A mid-walk page that parses to nothing, or a listing with no pager at all, raises; progress is not marked done and the walk resumes at that page |
| A 404 in the middle of a TX/CA walk was read as the end of the archive (an edge can answer one for any page) | Only the pager ends an archive. A mid-walk 404 raises and the walk resumes there; a 404 on the first page is left to the empty-archive rule |
| The first row of a state recorded a clean miss when the archive could not reach its window (the reach was checked before the archive had been walked) | `applies` walks the archive (bounded, shared by later rows) before asking, so the first row is skipped with "the IL archive reaches back only to 2024" like every later row; a row's page budget is spent once across `applies` and `run` |
| `efp court ag harvest` walked a state recorded `manual`/`broken`; `ag verify` reset a recorded mode to the declared one before probing, so a transient probe failure left a blocked state `automated` | `harvest` consults `state_adapters` like `applies` does and skips with the recorded reason; `verify` seeds only missing rows (a recorded mode is kept; manual-declared adapters refresh their code-owned facts), and only a successful probe changes an automated state's record |
| OH read any 404 as an empty month and moved on, leaving a hole an edge error could cause | A 404 month is asked once more, past the cache, before it is accepted as empty |
| `applies` swallowed a walk failure silently and the freshness walk, already marked done, was not retried | The failure is noted on the row, and the once-per-run mark is made only after the freshness walk finishes (`Capture.ran_this_run` / `mark_ran`), so a failed walk is retried on the next row |
| One 403 persisted a state as `manual`, and any `CaptureError` in a smoke test or `ag verify` persisted `broken`, switching a state off for the whole pipeline | A listing refusal makes the row `deferred_error` and records `manual` only after 3 refusals in one run; throttles, 5xx and dropped connections (`is_transient`) are retried and never recorded; the live smoke reports a refusal without recording it (`efp court ag verify` is where a refusal becomes `manual`); `broken` is recorded only when a page loaded and parsed to nothing |
| Smaller: a NULL `min(year)` in `ag harvest`; the matrix omitted some strategies and the states with no rows; a stale robots.txt docstring; page tokens in two cassettes | Fixed; tokens are redacted by the recorder and in the committed cassettes |

## Gated sources encountered (not worked around)

| Source | What it wants (account, fee, form, person) | Rows affected | Where logged |
| --- | --- | --- | --- |
| Florida AG releases | answers HTTP 403 to automated requests | 89 | `state_adapters` FL, `docs/coverage_matrix.md` |
| North Carolina DOJ releases | answers HTTP 403 to automated requests | 82 | `state_adapters` NC |
| Minnesota AG releases | no index exists to walk | 138 | `state_adapters` MN |
| Wisconsin DOJ releases | PDF files only (an optional reader is needed) | 68 | `state_adapters` WI |
| Kentucky AG releases | script-rendered list | 62 | `state_adapters` KY |
| Indiana AG releases | script-rendered list | 59 | `state_adapters` IN |

The six manual states carry 498 of the ten states' 798 rows (62%); those rows keep the rest of the chain (CourtListener bulk/API when classified federal or unknown, then the state-docket adapters of build 09) and the manual import path.

## Numbers for the first-pass report

Gated-source demand (one of the nine numbers): of the 798 rows in the ten first-pass states, 498 sit in a state whose AG releases cannot be read automatically (above). Located-record rates by state and strategy: **not yet measured**.

## Spot-check guide for the operator

- After the run: `uv run efp status --coverage` and look at `state_ag` located counts for TX, CA, OH, IL; a few `court_records` with `strategy = 'court.state_ag'` (read through `court_records_confirmed`) and open their captures.
- Rows in the four states with `court_status = deferred_coverage` mean the index has not reached their window: run `efp court ag harvest` and `--recheck`.
- IL rows before 2024 are skipped with "the IL archive reaches back only to 2024": that is the site's reach, not a negative.
- Listings carry only a title (and for TX/OH a summary), so a release whose title and summary lack election wording is never read; a subject named only in a body of such a release is missed. The record's `basis` says why a release matched.

## Decisions needed from the operator

1. **Six of ten states are manual.** Taken as the prompt allows. Decide whether to spend effort on any of them (a PDF reader for WI; an investigation of the script-loaded lists for KY and IN, which probably call a JSON endpoint that was not looked for; MN has no index at all). FL and NC refuse automated requests and stay manual.
2. **DA modules are not built.** The prompt ties them to the domain table (`reports/first-pass/domains.md`), which build 06's real run has not produced. `Release.office` already carries `DA:<county>` and the record kind `da_release` is wired; adding a DA site is one more small module once the table exists.
3. **IL reaches back only to 2024** on the page the adapter reads; older IL rows are reported out of reach. An older archive, if the site has one, would be a second cursor.
4. **Candidate selection uses title and summary only** (listings have no body), with election terms or the surname; the matching itself reads the full release text. Widen `ELECTION_TERMS` use if the first run shows misses.
5. **`docs/coverage_matrix.md` is provisional**: ten states, the prompt's row counts, no located counts. `efp status --coverage --write` regenerates it with every state (including those with no adapter, as `none`) from the real database.
6. **Cassettes are real recordings** of unrelated current releases (a settlement, a coalition filing, a company), trimmed to a few listing items; re-record with `efp court ag record <state>` when a site changes. A site redesign shows up first as a failing live smoke (state marked `broken`).
7. **Terms of use were not read.** The prompt and the plan keep the rule that a source whose terms forbid automation is not scraped. This build did not read the terms of the TX, CA, OH or IL sites; please confirm them before the full run. The probes made a handful of requests per site through the capture layer.
8. **The year window is the DOJ one.** `search(..., window=2)` is the protocol's default, but the module passes `court.doj_year_slack` (1), the same window the DOJ releases use; there is no separate `court.ag_*` window key. Add one if the two should differ.

## Done-when check

- *"`uv run efp run court --sample` and the full run (detached) record an attempt for every in-scope row in the ten states; `efp status` shows located-record rates by state and strategy."* **Does not hold here: not run** (no database or sample in this session). The module, hash scoping, deferral states and `efp status --coverage` are built and tested on synthetic rows (`tests/test_court_ag.py`); the commands are above.
- *"`docs/coverage_matrix.md` is generated and lists the ten states' modes with reasons for every `manual`."* **Holds, provisionally**: generated by the new renderer from the probed `state_adapters` data and the prompt's counts; every manual has its reason.
- *"All ten cassettes pass in CI; the live smoke results are in the report with the date."* **Holds for the four automated states**: four cassettes pass in CI (`test_each_state_cassette_parses_to_dated_releases_and_the_release_text_extracts`); the six manual states have no site to record and instead have their probed reasons asserted (`test_the_ten_first_pass_states_are_registered_with_a_mode_and_every_manual_has_a_reason`). Live smoke 2026-11-22: all four ok (above).

Review checkpoint (yours): the matrix and the manual list, then the located-record rates once the run has produced them.

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

## Carried forward

- 09: `state_adapters` has `kind = 'ag'` rows for the ten first-pass states: automated TX, CA, OH, IL; manual FL, NC (HTTP 403 to automated requests), MN (no archive or search index), WI (PDF files only), KY and IN (lists loaded by script); `mode = 'broken'` is now allowed (migration 0009) and is set by a failed live smoke. `kind = 'docket'` rows are build 09's.
- 09: `docs/coverage_matrix.md` exists and is provisional (the ten states, the prompt's row counts, no located counts); `efp status --coverage --write` regenerates it with every state from the real database, and build 09 extends it with the docket adapters and the gated list.
- 09, 14: the six manual AG states carry 498 of the ten states' 798 rows; their rows keep the rest of the court chain and the manual import path (decision 1). Each AG site's address is in `state_adapters.portal`.
- 08, 09: the DA and county-attorney modules are not built; they wait on `reports/first-pass/domains.md`, which the real `resolve_links` run produces. `Release.office` already carries `DA:<county>` and the record kind `da_release` is wired (decision 2).
- 12: state AG records are `court_records` with `source_kind` `state_ag_release` or `da_release` and `strategy = 'court.state_ag'`, read through the views; a record is weak when only an initial matched, the text names several case numbers, or several releases matched.
- 09: the harvester's walk-and-reach pattern is reusable: a persistent per-state index under `paths.bulk`, a resumable harvest with a per-row page budget, a freshness walk once per run, `deferred_coverage` when the archive has not reached the row's window, and an out-of-reach skip when the archive ends before it.
- operator; 09: the terms of use of the TX, CA, OH and IL sites were not read in this build; confirm them before the full run (decision 7). California's published Crawl-delay is 10 seconds, so `rate_limits."oag.ca.gov"` is 0.08.
- 19, 21: located-record rates by state and strategy are not yet measured; they come from `efp run court` on the real rows and `efp status --coverage`.

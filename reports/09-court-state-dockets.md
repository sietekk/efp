# Build 09 — State docket adapters and the portal probe

Branch: `build/09-court-state-dockets` (off `main`; builds 07 and 08 and the two maintenance PRs are merged, nothing stacked) · PR: see the pull request for this branch · Date: 2026-11-22 · Model: claude-sonnet-5-5 · Prompt: `build/09-court-state-dockets.md` · Spec sections: 6 (state docket adapters), 4 (court sub-strategy 4), 12 (portal redesigns; source terms), 7 (WACZ)

## What this build set out to do

Probe the public docket portals of the twenty largest states, record each as `automated`, `assisted` or `manual` with the reason, build the adapter framework and the automated adapters, present the assisted rows to the operator, capture JavaScript-only portals with Browsertrix only where terms allow, and complete the coverage matrix with the docket column and the court-status distribution.

Changes the approved plan made to the prompt (both operator-approved at planning):

- **Terms gate.** The prompt allows the probe one page load per portal (the entry point), so the pipeline cannot read a portal's terms. An adapter therefore searches a portal only for a state listed in a new `court.docket_terms_cleared` (empty by default) after the operator has read that portal's terms. **No portal is `automated` yet.**
- **Assisted rows do not block.** The prompt makes an assisted row `needs_review`; build 04 decision 5 says a predecessor in `needs_review` holds identity and later stages. An assisted row instead finishes `done` with `court_status = gated_portal` and the constructed query in `summary_json.assisted`; `efp review court --assisted` and `queues/assisted_dockets.csv` list it and importing the result later re-runs the row.
- **No search was run against any portal while building** (guardrail), so the Wisconsin adapter and its cassettes are synthetic and its request and answer shapes are assumed (listed in `docs/sources.md`).

## What was built

- `efp/court/adapters/docket/`: `__init__.py` (`DocketHit`, the `DocketAdapter` protocol, the registry), `base.py` (the automated adapter base: capture-layer fetching, JSON POST, refusals as `SiteBlocked`), `wi.py` (Wisconsin WCCA, behind the terms gate), `probe.py` (entry-page analysis, the mode rules, one-page probe, best portal per state, snapshot), `query.py` (the deterministic assisted query), `queue.py` (the operator's CSVs under the data directory), `module.py` (`court.state_docket`, replacing the stub), `states.py`.
- `efp/capture.py`: `Capture.browser(url, row_id, stage)` runs Browsertrix Crawler in Docker (one URL, WACZ), stores it content-addressed and inserts a `captures` row with `wacz_path`; if Docker is absent or not running it raises `CaptureError` and the row is deferred, never a negative. `_insert` takes `wacz_path`.
- `efp/court/__init__.py`: `court_status` has `gated_portal` (set only when nothing was found, nothing is deferred, and the portal is assisted or wants an account or a fee); `summary_json.assisted`; the input hash carries the docket portal's recorded mode and the cleared-list membership for rows the module can apply to only.
- `efp/coverage.py` and `docs/coverage_matrix.md`: the docket section (mode, what the portal wants, adapter present, verified date, court-status distribution by state) and a reasons list.
- CLI: `efp court docket probe [--state] [--snapshot]`, `efp review court --assisted [--state]` (only the `--assisted` path of `review` is built); `efp selftest --live` loads each cleared docket portal's entry point.
- `efp/court/adapters/ag/sync.py`: `write` and `recorded` take a `kind` (default `ag`), so both adapter kinds share one table.
- Reference data: `reference/docket_portals.csv` (the seed list: twenty states, 22 portals, unverified addresses) and `reference/docket_probe.csv` (the 2026-11-22 probe).
- Config (documented in `config.example.toml`): `court.docket_terms_cleared`, `court.docket_assisted_limit`, `court.docket_max_hits`, `court.docket_browser_image`.
- Docs: `docs/sources.md` (probe, assumptions), `queues/README.md`, `RUNBOOK.md`.
- Tests: `tests/test_court_docket.py` (50 tests: entry-page analysis, 13 mode-rule cases, transient and gate rules, best portal per state, the seed list, one GET and no POST, the Wisconsin adapter on synthetic cassettes, the module's skip reasons, assisted rows and their deterministic query, the cleared automated path, candidates and portal errors, repeated refusals, hash scoping, `Capture.browser` with Docker absent, failing and faked, `review --assisted`, the matrix); synthetic cassettes in `tests/cassettes/docket/`.
- Results: `uv run pytest -m "not live"`: 988 passed. `ruff check`, `ruff format --check`, `mypy efp`: clean.

## What was run

| Run | Rows attempted | Rows resolved | Wall clock | Requests (per domain) | API spend |
| --- | --- | --- | --- | --- | --- |
| `efp court docket probe` (three runs while the rules were tuned) | n/a | 19 of 20 states recorded | seconds each | one entry page per portal per run (22 portals); no search | $0 |
| `efp run court --sample` / full run | 0 | 0 | not run | none | $0 |

The court stage was **not run on real rows**: this session has no `~/efp-data`, database or frozen sample, so the court-status distribution by state, the rows blocked per portal and `queues/portal_accounts.csv` with real row counts are not produced. The probe ran against the real portals through the capture layer into a scratch database; its results are committed in `reference/docket_probe.csv` and the matrix. To produce the rest:

```bash
uv run efp court docket probe --snapshot   # records state_adapters and queues/portal_accounts.csv with row counts from the real database
uv run efp run court --sample              # then the full run, detached
uv run efp status --coverage --write       # regenerates docs/coverage_matrix.md with every state
uv run efp review court --assisted         # the rows waiting on your own searches
```

## Results by source

The probe of 2026-11-22: **0 automated, 19 states assisted, 0 manual, 1 not judged** (Mississippi's entry point failed transiently; it is probed again next run). One state's best portal is recorded; the others are in `reference/docket_probe.csv`.

| Wants | States (best portal) | Meaning |
| --- | --- | --- |
| `terms-not-cleared` | WI | an adapter exists (WCCA); automated once you have read its terms and listed `WI` |
| `blocked` | NC, NH | the entry point answers HTTP 403 to automated requests |
| `challenge` | OH (Cuyahoga), AZ (Maricopa) | a challenge page stands in front of the entry point |
| `account` | TX (Harris), KY, AL | the entry point shows a login and no search form (a heuristic: the operator checks) |
| `fee` | FL (Broward, one of two FL portals) | fee wording on the entry page |
| `js-only` | FL (Miami-Dade, recorded), CA (Los Angeles), VA | the search renders only in a browser |
| `no-adapter` | IN, IL, PA, MO | an online search exists; no adapter is built for the platform |
| `search-not-found` | MN, NJ, WV, IA | no name-search form is visible at the entry page (it may sit behind a menu) |

The seed address for Dallas County answered HTTP 404 (`unreachable`); Texas is recorded through Harris County. Corrected seed addresses are in `reference/docket_portals.csv` for the next probe.

## Failures by reason

None in the build's own runs.

## Review findings (independent `done-when-reviewer`, all fixed)

| Finding | Fix |
| --- | --- |
| **High:** the gated list kept one portal per state, so a state's other gated portals (Broward's fee wording, Dallas) were missing | `portal_accounts.csv` is built from every portal probed; a portal the pipeline cannot search today (anything not `automated`) is listed with what it wants |
| **High:** one county portal stood in for a whole county-by-county state (every TX row got Harris County's portal and `gated_portal`) | For a state with county-level portals only, a row gets a portal only if its own text names a probed county; otherwise it is noted "no docket portal probed for this row's county" and is not marked gated |
| **Medium:** a browser capture has no HTTP status, so `record.ok` dropped every hit from a script-only adapter | A `browsertrix` capture counts as a success; tested through a `needs_js` adapter |
| **Medium:** Browsertrix ran without the project User-Agent | `--userAgent` is the configured value; a timed-out crawl removes its container by name |
| **Medium:** a docket's provenance pointed at the detail page, though its fields come from the search answer | The search answer's capture is the record's source and parent of the evidence text; the detail page is kept in `basis.detail_capture` |
| Medium: an earlier court hit skips the docket module (the chain is `first_hit`, docket last) | By design (the chain stops at the first document with a case number); recorded as decision 10 |
| Re-probing one state (`--state`) overwrote `portal_accounts.csv` with only that state | With `--state` the file is merged: that state's rows are replaced and every other state's kept |
| `Capture.browser` ignored the capture store, used `:latest`, recorded efp's version, and read a WACZ of any size into memory | An earlier WACZ of the URL is reused (a new `captures` row says so; no Docker, no budget) unless `refresh`; the image must be pinned (a version or digest; `latest` is refused; the default is `1.13.2`); the resolved image id is recorded and `tool_version` is Browsertrix's tag; a WACZ over 250 MB is refused |
| A county-portal-only state's row that names no probed county ended `not_found` (a recorded negative) | It is a coverage gap: `deferred_coverage` with `:coverage` on its hash, so it is revisited when a county is added, never recorded as searched-and-absent |
| A common name fetched every matching docket (and its PDFs) per row, and an unpaged answer was treated as complete | `court.docket_max_hits` (default 10) caps the dockets recorded per row: past it the extra matches are not fetched, no detail pages or documents are fetched, the recorded dockets rest on the search answer, all are candidates, and the row says why. An answer that reports more than it returned (assumed keys `more`, `hasMore`, `totalCount`; no pagination is built) makes its hits candidates too |
| A transient failure of a state's statewide portal let a lower-ranked county portal overwrite the state's recorded mode | If any portal of a state was not judged, the probe keeps the recorded mode for that state and says so; it is probed again next time |
| The WCCA search sent an empty `firstName` exact match for a surname-only row (its meaning to WCCA is unknown: a silent miss or every case with the surname) | A row with no given name is never searched by an adapter: it takes the assisted path with the query built from what the row has and a note; the adapter itself also returns nothing without a given name |
| Low: the assisted query's `platform` held the probe's "wants"; record identity ignored the county; two different filing windows; shallow heuristics (any `$5` was a fee, a `javascript:` terms link); the cleared list was case-sensitive | The query uses the portal's platform; identity includes the court; both paths use `court.window_years_after`; the fee rule needs a per-search price, script and anchor links are skipped, the cleared list is upper-cased |
| Low: gated versus assisted counted differently | `gated_portal` is set for every assisted or account/fee portal, and the gated list now lists every non-automated portal, so they agree |
| Low: the queue files sit under the data directory, not `queues/` | Deliberate (they hold names; `pacer.csv` is the precedent); stated in `queues/README.md` |

## Gated sources encountered (not worked around)

Every portal above is a gated source in the sense of the plan: nothing was worked around, and a portal that refused, challenged or asked for a login or fee is listed with what it wants. With real row counts, `queues/portal_accounts.csv` (written by the probe) names for each the account, fee, challenge or terms step and the rows affected; the ten states counted in build 08 are:

| Portal (state) | What it wants | Rows in the state |
| --- | --- | --- |
| Harris County (TX) | login shown, no search form seen | 113 |
| NC Judicial Branch portal | answers 403 to automated requests | 82 |
| Cuyahoga County (OH) | challenge page | 63 |
| KY CourtNet | login shown, no search form seen | 62 |
| Miami-Dade and Broward (FL) | script-only search; fee wording at Broward | 89 |
| Los Angeles (CA) | script-only search | 70 |
| WCCA (WI) | terms not yet cleared | 68 |
| IN MyCase, Cook County (IL) | no adapter yet | 59, 54 |
| MN | no search form visible | 138 |

These are rows in the state, not rows blocked only by the portal: the second-pass price is the `gated_portal` count after the court stage has run.

## Numbers for the first-pass report

Gated-source demand (one of the nine numbers): all of the ten largest states' docket portals need either the operator's browser, an account or a fee, a cleared set of terms, or an adapter that does not exist yet; none is searchable by the pipeline today. The rows blocked only by a gated portal, per portal, come from `efp status --coverage` after the court stage has run: **not yet measured**.

## Spot-check guide for the operator

- `reference/docket_probe.csv`: for each portal, open its entry URL yourself and check the probe's call. The probe is a heuristic on one page: a login field with no search form (`account`), fee wording (`fee`), a page empty without JavaScript (`js-only`), and "no search form visible" (`search-not-found`) can each be wrong, which is why they are all `assisted` (you look) and none `manual`.
- Read WCCA's terms; if automated search is allowed, add `"WI"` to `court.docket_terms_cleared`, run `efp court docket probe --state WI`, then `efp selftest --live` and a sample court run.
- `efp review court --assisted --state WI` shows a row's query; searching, saving the case pages, and `efp import --row <id> --source portal <file>` (build 14 adds the `portal` source) completes the loop.

## Decisions needed from the operator

1. **No portal is automated.** The pipeline cannot read terms, so every adapter waits for your clearance in `court.docket_terms_cleared`. Decide which portals' terms to read first (WI has the only adapter).
2. **Assisted rows are non-blocking** (a deviation from the prompt's `needs_review`; approved at planning). Say so if you want them to hold identity until you import a result.
3. **The seed portal list is the builder's knowledge and unverified.** The probe confirmed most addresses load, one (Dallas County) did not and one failed transiently (MS); correct `reference/docket_portals.csv` and re-probe. County-by-county states list only their largest-county portals, so most of the state's rows are not covered by any listed portal.
4. **The probe's account, fee, script-only and no-form calls are heuristics** on one page. All are recorded `assisted`; none is recorded `manual`, because a page that shows no search form does not prove no search exists. `manual` is reserved for a seed entry marked `none` and for an address that is not there.
5. **Wisconsin's request and answer shapes are assumed** (no search was run). The adapter raises on an answer of another shape; the first cleared live search is the check.
6. **The 2022 court-scraper modules were not forked.** court-scraper (ISC licence, last release 2022-12) is on the excluded list and stale; the WCCA adapter was written fresh against the assumed API, so nothing from it needs an ISC notice. Oklahoma OSCN is not in the twenty states and is not built. **juriscraper** covers opinions and PACER, not state dockets, so no court adopts it.
7. **Browsertrix needs Docker**, which was not exercised for real (tests fake it). A JS-only portal that is both cleared and has an adapter would be captured as a WACZ; none exists yet.
8. **`efp review` is built for `court --assisted` only**; the rest of `review` still prints "not built yet" (belongs to a later build).
9. **What a second run adds:** portals whose terms you clear; adapters for newly cleared platforms; the other ten states' row counts and costs once the real database exists (the matrix shows 0 for them now); imported assisted results (build 14); re-probes of the unreachable and transient portals.
10. **The court chain stops at the first document with a case number**, so a row with a press-release hit never reaches the docket module and cannot move from `press_release_only` to `primary_record`. That is the spec's chain ("stops at the first document that yields a case number"). Say so if the docket module should also run after a press-release hit.

## Done-when check

- *"Every one of the twenty states has a `state_adapters` docket row with a mode and reason, and `queues/portal_accounts.csv` names the account or fee each gated portal wants with the rows affected."* **Partly holds.** 19 of 20 states have a row with a mode and reason in the probed database (Mississippi was not judged: transient). The probe writes `portal_accounts.csv` (tested), but with this session's empty database the rows-affected column is zero and the file is not committed; run the probe on the real database.
- *"The `automated` adapters have run on their states' rows (sample, then full); `efp status` shows the court-status distribution by state."* **Does not hold here.** No portal is automated (decision 1) and the court stage was not run on real rows. The distribution by state is rendered by `efp status --coverage` (tested on synthetic rows).
- *"The report says how many rows are blocked only by a gated portal, per portal, which prices the second pass."* **Partly.** The portals and what each wants are above, with the rows in each state for the ten known states; the count of rows blocked only by the portal is the `gated_portal` column after the run (decision 9).

Review checkpoint (yours): `docs/coverage_matrix.md` and `reference/docket_probe.csv`, and the order in which to read portals' terms.

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

## Carried forward

- 10, 14: documents a docket lists are captured as PDFs (`captures` rows for the row, referenced in the record's `basis.document_captures`); the documents stage reads them from there. Build 14 adds `portal` to the `efp import` sources (`--source portal <file>`), the command `efp review court --assisted` prints.
- 12: state docket records are `court_records` with `source_kind = 'state_docket'`, `strategy = 'court.state_docket'`, read through the views; a record is weak when only an initial matched or several dockets matched. Identifiers come through the same extraction as the other court modules.
- 12, 13, 15, 16: assisted rows finish `done` with `court_status = gated_portal`, not `needs_review`, so later stages run provisionally; the constructed query is in `case_stage.summary_json.assisted`, and importing a portal result re-runs the row (`--reset`).
- 14: the queue files `assisted_dockets.csv` and `portal_accounts.csv` live under the data directory's `queues/` and hold names and row ids; they never go in the repository.
- 19, 21: the rows blocked only by a gated portal (per portal) are the `gated_portal` count by state in `docs/coverage_matrix.md`, which `efp status --coverage --write` regenerates; not yet measured.
- operator; 20: no search was run against any portal during this build; adapters run only for states in `court.docket_terms_cleared`, so the compliance review should list which states are cleared and on what reading of their terms.
- 20: `Capture.browser` runs Browsertrix Crawler (AGPL) as a separate Docker container, never linked; the licence register should say so.

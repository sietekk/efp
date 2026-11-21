# Build 09: State docket adapters and the portal probe

Spec: section 6 (state docket adapters: automated / assisted / manual), section 4 (court sub-strategy 4), section 12 (risk: adapters break on portal redesigns; compliance: source terms), section 7 (WACZ via Browsertrix for JS-heavy pages). Task list: D4. Needs: 08. Branch: `build/09-court-state-dockets`. Report: `reports/09-court-state-dockets.md`.

Read first: `CLAUDE.md`, `build/README.md`, `docs/spec.md` section 6 "State docket adapters" (the three modes are the contract), `docs/plan.md` risk table row on court portals, `reports/08-court-state-ag.md` (framework and coverage matrix), `docs/tool-register.md` on juriscraper, court-scraper and Browsertrix.

Stay in plan mode until I approve. Propose the probe procedure (what you check on each portal and how you decide its mode without violating its terms), the adapter protocol, which portals you expect to be `automated`, and how assisted rows are presented to me. Wait.

## What to build

1. **The probe, for the twenty largest states by row count:** MN, TX, FL, NC, CA, WI, OH, KY, IN, IL, NJ, MS, AL, AZ, PA, WV, VA, MO, IA, NH. For each state's public docket search (statewide portal or, where there is none, the county systems that cover the rows in the dataset): read the terms of use and robots.txt, note whether search is possible without an account or fee, whether a CAPTCHA or bot check stands in front of search, whether results can be retrieved by name and year, and whether the system is a known platform (Tyler Odyssey, Wisconsin CCAP, Oklahoma OSCN, Pennsylvania UJS, Florida county clerks, Texas county-by-county). The probe is reading, not scraping: one page load of the search entry point, through the capture layer, at most. Record each as a `state_adapters` row with `kind = 'docket'`, the mode, and the reason, and list every gated portal in `queues/portal_accounts.csv` (portal, what it wants: account, fee, CAPTCHA, terms; rows affected).
2. **Modes, exactly as the spec defines them:**
   - `automated`: search and retrieve by name and year without a human. Build adapters only here. Expected candidates: Wisconsin CCAP and Oklahoma OSCN (the 2022 court-scraper modules may be forked and repaired if their license allows; check and record it), and any other portal the probe clears.
   - `assisted`: the pipeline constructs the search URL or query; I run it in the browser; the result enters through the manual import path (build 14). Odyssey portals behind a CAPTCHA are the typical case. A row here is `needs_review`, not `failed`, with the constructed query attached in `summary_json`, so my manual session is pre-assembled.
   - `manual`: no online docket search at all; county clerk by phone or records request. Recorded; nothing is done.
3. **Adapter protocol.** The adapters together are the `court.state_docket` module; per state the mode decides what runs (`automated` adapter; `assisted` query construction; `manual` record only), and a state without an adapter is skipped with the reason, never an error. Same shape as `AGScraper`: `search(name, year, window) -> list[DocketHit]` and `fetch(hit) -> Capture`, with `DocketHit(court, case_number, caption, filed_date, url, document_urls)`. Hits become `court_records` with `source_kind = 'state_docket'` and feed the identifier extraction; documents are captured as PDFs for build 10. Use `juriscraper` where it already covers a court.
4. **Browser capture, narrowly.** For an `automated` portal that needs JavaScript to render results and whose terms allow a browser, use Browsertrix Crawler in Docker (WACZ, `captures.wacz_path`) through a `ctx.capture.browser(url)` method added here; if Docker is not running, record the rows as deferred. No headless browser is ever pointed at a portal whose terms forbid automation or that presents a CAPTCHA; that is `assisted`.
5. **Assisted queues.** `efp review court --assisted` lists rows in assisted states with the constructed query, the portal URL and the import command to run afterwards (`efp import --row <id> --source portal <file>`; build 14 adds `portal` to the import sources). `queues/assisted_dockets.csv` holds the same for working outside the CLI.
6. **Coverage matrix, completed.** `docs/coverage_matrix.md` now carries the docket column per state: mode, platform, adapter present, rows affected; and the court-status distribution per state (primary record, press release only, gated portal, not found). This is the artifact the plan calls out for Jeff.

## Tests

- Adapter contract tests on cassettes for every `automated` adapter, including a results page with no hits and a portal error page.
- Probe output: a synthetic set of portal descriptions classified into the three modes by the rules, with reasons.
- Assisted path: the constructed query for a synthetic row is deterministic and the row lands in `needs_review` with it attached.

## Done when

- Every one of the twenty states has a `state_adapters` docket row with a mode and reason, and `queues/portal_accounts.csv` names the account or fee each gated portal wants with the rows affected.
- The `automated` adapters have run on their states' rows (sample, then full); `efp status` shows the court-status distribution by state.
- The report says how many rows are blocked only by a gated portal, per portal, which prices the second pass.

## Guardrails that bite here

No CAPTCHA solving, no bot-check evasion, no automation against terms that forbid it, no account creation. The probe loads one page per portal. A blocked portal is a finding, not a problem to route around.

Finish with `reports/09-court-state-dockets.md` and a pull request. Do not merge. Review checkpoint: the coverage matrix and the gated-portal list. This build is the one most likely to run twice; say in the report what a second run would add.

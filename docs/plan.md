# Election Fraud Pipeline: First-Pass Build Plan (Claude Code)

November 2, 2026 · Michael Conroy

> **Amendment, November 21, 2026.** Stage 4 (public social media; task list D10; build prompt 15) is in the first pass, in session S5 after news; its design is unchanged.

The plan for building and running the pipeline's first pass with Claude Code. It implements the Election Fraud Pipeline Technical Specification (`docs/spec.md`) in the order of the Election Fraud Pipeline Task List (`docs/tasks.md`), restricted to sources no person has to grant.

## Scope of the first pass

The first pass runs all 1,481 rows through the pipeline using only sources a program can reach online, or that a self-serve signup unlocks, and reports what resolved, what did not, and why. Nothing in it depends on asking a person for access, approval or a purchase. It is QA work: each Heritage row is checked against a primary record, every value it produces is marked provisional, and the rows that need a gated source come out as a queue rather than a gap.

**In the first pass**

- Stage 1: expand and archive every Heritage source link; find the primary record through the DOJ press-release API, CourtListener and RECAP, state attorney general and district attorney press releases, and the state court portals that allow public search without an account.
- Stage 2: identity confirmation with the population prior and the Splink model, using identifiers pulled from the court record and the captured pages.
- Stage 3: news for 2017 onward through GDELT, Media Cloud and the Wayback Machine; for earlier cases, only what the open web and the archive still hold.
- Documents: PDF parsing and OCR for every filing the pipeline can download.
- Stage 5: extraction and coding run in proposal mode. Codes and evidence tiers are written to the working database, not to Jeff's columns, until the human review in the last session.
- The full population. Jeff has not said whether the study is all 1,481 rows or the 1,252 criminal convictions; case type is a column, so the cut is a query later.

**Deferred, and why**

- NewsBank, Newspapers.com, ProQuest and Westlaw Campus Research: library databases with manual search terms. The queue of pre-2017 rows that need them is a first-pass output.
- PACER (academic fee exemption is an application), the CourtListener EDU tier (an email), Ballotpedia academic access (an email), Reddit for Researchers and the Meta Content Library (applications), the X API (paid).
- State voter files that are requested from an office rather than downloaded. Only North Carolina and Ohio publish a direct download, and they enter only if Jeff says yes to decision 6 in the Summary.
- People-search and data-broker services: excluded by design, not just deferred.

## How the Cowork sessions run

Claude Code builds and runs each session on your Mac in its own branch; you review a one-page session report and spot-check rows before anything merges. You hold the keys, the money and every judgment about a named individual.

**Roles**

| Who | Does | Never does |
| --- | --- | --- |
| Claude Code | Writes the code and tests, runs the stage on the 50-row sample and then on all rows, writes `reports/session-N.md`, opens the pull request | Signs up for anything, buys anything, fills in a form addressed to a person, looks up an account by a person's name, writes to the nine source columns, merges |
| You | Create the repo and the data folder, hold the self-serve keys, review each report, spot-check 10 rows per session with `efp review`, merge, decide every stop-and-ask | Hand-code rows before the review session (the first pass is machine output marked provisional) |

**One session, start to finish**

1. Claude Code reads `CLAUDE.md`, this plan and the spec section for the session, then proposes its approach in plan mode. You approve or redirect before any code is written.
2. It implements on branch `session/N-name` with unit tests, contract tests on recorded cassettes, and `ruff` and `mypy` clean.
3. It runs the stage on the 50-row stratified sample first, then on all 1,481 rows, and stops on any source that wants an account, a fee, a form or a person.
4. It writes the session report: rows attempted and resolved, per-source counts, failures by reason, time and API spend, and the decisions it needs from you.
5. You read the report, spot-check 10 random rows, and either merge or send it back with comments on the report.

**Review checkpoints** (nothing moves past these without you)

- After S0: schema, CLI and append-only triggers; the data folder layout.
- After S1: link-resolution numbers and the domain table, which fixes the adapter priority for S2.
- After S2: court-record coverage by state and the list of portals that need an account.
- After S4: identity thresholds and the 50-row false-confirmation audit (F3 in the task list).
- After S6: extraction results on the sample, before any run at volume.
- Before any export: the round-trip test (F1), so the nine source columns hash identically.

**Standing guardrails** (these go into `CLAUDE.md` verbatim)

- No people-search or data-broker service, ever; no account discovery by a person's name.
- Every network call goes through the capture layer, is rate-limited per domain at 80% of the published limit (1 request per second where none is published), and is logged.
- Every value written carries its source capture and, for extractions, a verbatim span; abstain rather than guess.
- The nine source columns are never written; `rows` is immutable; captures, decisions and the audit log are append-only.
- Secrets live only in `~/.config/efp/.env`; the database and store live only in `~/efp-data/`; neither is ever in the repository.
- Social stage code is not written in the first pass. Handles found in records are stored, nothing is looked up.
- A source that needs an account, a fee, a form or a person is not worked around. It goes in the report as a gated source.

## Source allowlist for the first pass

Everything below is reachable by a program or by a signup you complete yourself in minutes. The spec's section 5 has the full interface catalog; this is the subset the first pass may touch.

| Source | What it gives | Access | Limits and notes |
| --- | --- | --- | --- |
| Heritage source links | The starting URL for 1,322 rows (159 have none) | None | Follow redirects and shorteners; archive on first fetch |
| DOJ press-release API | Federal cases, roughly 2013 onward | No key | Under 4 requests per second; topic filter is ignored, so filter locally |
| CourtListener bulk data | Dockets, parties, courts as monthly dumps | Download, public domain | Load once; search locally before any API call |
| CourtListener REST API and RECAP | Docket search, filings already in the archive | Self-serve token | Throttled per the spec; bulk-before-API rule applies |
| Wayback Machine CDX and Availability APIs | Archived copies of dead source and news pages | No key | Rate-limited; the local WARC stays the evidentiary copy |
| Save Page Now 2 | Background redundancy for every captured page | Self-serve S3 keys | Throttles under load; failures are not errors |
| GDELT DOC 2.0 | News index, 2017 onward | No key | About one request per 5 seconds; nothing before 2017 |
| Media Cloud | Local and regional news index, partial before 2017 | Self-serve key | Coverage is uneven; report hit rate by year |
| State AG and DA press releases | State-case announcements with names and charges | Public web, per-state scraper module | Top states by case count first |
| State court portals with public search | Dockets and dispositions | Public web, no account | S2 probes each portal; the ones that need an account or fee are listed, not worked around |
| OpenFEC | Federal candidates and committees | Self-serve api.data.gov key | 1,000 requests per hour |
| Congress.gov API and congress-legislators | Federal officeholders | Self-serve key; GitHub file | Identity of public figures only |
| OpenStates API and bulk | State legislators | Self-serve key | Bulk first |
| DIME v4, Harvard local elections database, FollowTheMoney mirror | Candidate records and ideology scores for subjects who ran for office | Download | Mirror FollowTheMoney in session 0; it is unmaintained |
| Census surnames and SSA given names | The population prior for identity | Download | Loaded as reference tables |
| Ballotpedia public pages | Candidate biographies for identity confirmation | Public web | Low rate, read only; the API stays excluded |
| Google Programmable Search | Discovery of pre-2017 local coverage still on the open web | Self-serve key | 100 queries a day free, then $5 per 1,000; capped per session, optional |
| Anthropic API | Extraction with span citations; transcription of pages OCR fails on | Self-serve key | Temperature 0; model id and prompt hash logged per call |
| North Carolina and Ohio voter files | Direct downloads, the only two no-request files among the states that matter | Download | Only if Jeff says yes to decision 6; stored as its own column, never used for motive |

**Excluded from the first pass.** NewsBank, Newspapers.com, ProQuest Historical Newspapers and Westlaw Campus Research (library databases, manual search terms). PACER, the CourtListener EDU tier and Ballotpedia academic access (applications or emails). Reddit for Researchers and the Meta Content Library (applications), the X API (paid). Every state voter file that is requested from an election office. All people-search and data-broker services, which stay out after the first pass as well.

## Before session 0: your setup

About an hour, all of it self-serve. Every item is a signup form or a download; none goes to a person.

- [ ] Create a private GitHub repository `efp` with branch protection on `main` (pull requests only). Claude Code works on branches; you merge.
- [ ] Confirm FileVault is on, then create `~/efp-data/` (database, store, manifests, bags) and `~/.config/efp/.env` with mode 600. Neither path is ever inside the repository.
- [ ] Copy the Heritage workbook byte-for-byte to `~/efp-data/source/` and record its SHA-256 in `~/efp-data/source/MANIFEST.txt` (task C1).
- [ ] Install `uv` (Python 3.12), the GitHub CLI, and Docker Desktop. Docker is only for Browsertrix Crawler on portals that need a real browser; the first pass starts without it.
- [ ] Keys into `.env`, one line each: `COURTLISTENER_TOKEN` (free account), `DATA_GOV_KEY` (covers OpenFEC and Congress.gov), `OPENSTATES_KEY`, `IA_ACCESS` and `IA_SECRET` (archive.org account, S3-style keys), `MEDIACLOUD_KEY`, `ANTHROPIC_API_KEY`, and optionally `GOOGLE_CSE_KEY` with `GOOGLE_CSE_ID`.
- [ ] Pick the extraction model and a monthly API budget cap; both go in `config.toml`, not in code.
- [ ] Tell Claude Code which of the Summary's six decisions Jeff has answered. Unanswered ones default as the last section describes.

## Build sessions, in order

Nine sessions, each one Claude Code run with a report at the end. The codebase labels (D1 to D12) are the task list's; the social component D10 is not in the list. Each session names what it produces, what you review, and the test that lets the next one start.

1. **S0. Skeleton (D1).** Schema from spec section 3 (`rows`, `case_stage`, `captures`, `decisions`, `identifiers`, `audit_log`, `ref_*`), append-only triggers, the stage runner with its gates, the `efp` CLI, `config.toml`, tests and CI on recorded cassettes.
    - Produces: `efp ingest` loads 1,481 rows with hashes and the in-scope flag; `efp status` prints the empty matrix.
    - You review: the schema and the data-folder layout.
    - Done when: an export of the untouched workbook hashes identically to the source (the F1 round-trip, run early).
2. **S1. Capture and link resolution (D2, D3).** The capture layer (WARC via `warcio`, SHA-256 content store, weekly RFC 3161 timestamp, Save Page Now in the background), then every Heritage link expanded, followed through redirects, classified live, archived or dead, with the Wayback copy fetched for dead ones and text extracted with `trafilatura`.
    - Produces: link status for all rows and a table of source domains ranked by row count.
    - You review: the domain table; it sets the adapter order for S2.
    - Done when: every row with a source URL has a capture or a logged Wayback attempt, and every capture has a hash and a timestamp.
3. **S2. Court record (D4).** DOJ harvester for federal rows; CourtListener bulk load, then RECAP search; press-release modules for the ten states with the most rows (MN, TX, FL, NC, CA, WI, OH, KY, IN, IL); a probe of the court portals in the twenty largest states that sorts them into public-search adapters and gated portals; identifier extraction (date of birth or age, city, case number, court).
    - Produces: a court-record status per row (primary record, press release only, gated portal, not found) and the coverage matrix by state.
    - You review: the coverage matrix and the gated-portal list. This is the session most likely to run twice.
    - Done when: every row has an attempt logged against every applicable source, and the gated list names the account or fee each portal wants.
4. **S3. Documents (D6).** Download every filing the record points to; `pdfplumber`, then Docling, then OCRmyPDF, then Claude transcription for pages that fail; a quality score per page.
    - Produces: text for every downloaded document with page-level quality flags.
    - Done when: no page below the quality threshold reaches extraction unflagged.
5. **S4. Identity (D5).** Census and SSA reference tables, the population prior per row, the Splink model with parameters set from a 150-pair subsample you label in `efp review`, thresholds at 0.99 and 0.90, and `efp identity explain`.
    - Produces: a confidence per row, the review band queue, the parameter-stability chart.
    - You review: the F3 audit, 50 confirmed rows re-derived by hand, oversampling common surnames.
    - Done when: the audited false-confirmation rate is near zero with its interval reported; otherwise thresholds move before anything flows downstream.
6. **S5. News (D7).** GDELT for 2017 onward, Media Cloud, Wayback CDX queries against the outlets from S1's domain table, and the optional capped Google search; articles captured and linked to rows that passed the identity gate.
    - Produces: article counts per row by era, and the pre-2017 queue for NewsBank with a suggested search string per row.
    - Done when: every identity-confirmed row has a news attempt logged and the hit rate is reported by year band.
7. **S6. Extraction and coding (D8, D9).** LangExtract with the Claude API (Instructor fallback), the five extraction classes with verbatim spans, the codebook rules as pure functions, abstention as a class, prompts under version control, three runs on the sample with disagreement flagged.
    - Produces: proposed codes with evidence for every row that has text, written as provisional.
    - You review: all 50 sample rows against their sources before the run at volume.
    - Done when: no extraction on the sample lacks a verbatim span, and your accept/reject tally on the 50 is recorded.
8. **S7. Export and reporting (D11, D12).** The workbook with the new columns appended under a provisional prefix, the redacted CSV, the coverage matrix, per-stage counts, identity distribution, API spend, and the three gated-source queues (NewsBank rows, PACER dockets, portals needing accounts).
    - Done when: F1 passes on the real export and `efp status` renders the report Jeff will see.
9. **S8. QA review and freeze.** Your stratified review of 100 rows across stage outcomes, one fix cycle, then the first-pass outputs hashed and bagged, with `docs/first-pass-notes.md` recording what resolved, what needs a gated source, and what it cost. This replaces the task list's manual 50-row pilot (E2) with a machine pilot plus your review, and it is the report that goes to Jeff.

## What the first pass hands back

The first pass ends with the provisional workbook, the coverage matrix, three gated-source queues and nine numbers. The numbers are what Jeff sees and what decides the second pass.

| Number | Defined as | What it decides |
| --- | --- | --- |
| Link survival | Of the 1,322 rows with a source URL: live, archived only, dead | How much of Heritage's own evidence base still exists |
| Primary-record rate | Rows with a docket or filing, press release only, gated portal, none; federal and state separately | Whether the court-record stage carries the design, and the case for the PACER application |
| Identifier yield | Rows with a date of birth or age, a city, a case number | How many rows identity can even attempt |
| Identity outcome | Confirmed, review band, unresolved; the F3 false-confirmation rate with its interval, by common-name and rare-name strata | Where the thresholds sit before anything flows downstream |
| News reach by era | Rows with at least one captured article, for 2000–09, 2010–16, 2017–25 | The size of the NewsBank queue, which is the manual labor in the schedule |
| Extraction grounding | Rows with at least one span-cited extraction; abstention rate; three-run disagreement rate on the sample | Whether the model step can be trusted at volume |
| Provisional distributions | Motive type and evidence tier across coded rows | The first substantive number for Jeff; provisional, not a finding |
| Gated-source demand | Rows blocked only by one gated source, per source | The business case for each application or purchase in the second pass |
| Cost and time | API spend, wall-clock per stage, requests per domain | The schedule estimate for the rest of the semester |

**Files.** `reports/first-pass/status.md` (the matrix), `docs/coverage_matrix.md` (per state: portal public or gated, press-release module, rows resolved), `queues/newsbank.csv` (row, name, state, year, suggested search string), `queues/pacer.csv` (docket ids), `queues/portal_accounts.csv` (portal, what it wants, rows affected), the provisional workbook and the redacted CSV, and `docs/first-pass-notes.md`.

**Your review sample in S8.** 100 rows, 25 from each of: primary record found, press release only, identity review band, extraction with run disagreement. Each row gets a verdict (correct, wrong, unverifiable) and a note. The verdicts become the first reliability figures in the methods appendix.

## Risks specific to an online-only pass

The first pass is built to report its own gaps, so most of these end as numbers in the report rather than as blockers.

| Risk | Likelihood | What the plan does |
| --- | --- | --- |
| Court portals block automated search (bot checks, CAPTCHAs, terms that forbid scripts) | High for several large states | The portal goes on the gated list with the rows it affects. Browsertrix only where the terms allow a browser. |
| Pre-2017 news is thin on the open web; GDELT starts in 2017 and local papers are paywalled or gone | Certain: 994 rows (67%) are pre-2017 | Expected. The NewsBank queue with search strings is a deliverable, not a failure. |
| Heritage source links are dead and the Wayback Machine never captured them | Medium | Reported as link survival; the row falls back to court and press-release search by name. |
| State press-release sites restructure and break a module | Medium, over months | Contract tests on cassettes catch it; the module falls back to the Wayback copy of the release index. |
| CourtListener bulk dumps are tens of gigabytes | Certain | Load only dockets, parties and courts; keep the raw dumps outside the repository and off the backup. |
| Identifiers are sparse in state records, so identity stays unresolved for whole states | High for some states | A finding, reported by state; nothing moves downstream unresolved. |
| Model extraction drifts or costs more than planned | Low with temperature 0 and a budget cap | Model id and prompt hash per call; three runs only on the sample; the cap in `config.toml` stops the run. |
| Rate-limit budgets make full runs take days | Certain | Runs are resumable per row; sessions are scheduled around them rather than waiting on them. |
| Jeff's decisions arrive mid-run | Likely | They are flags in `config.toml`; every stage is idempotent, so a rerun is cheap. |

## Effort, cost and timeline

About ten Claude Code sessions over three weeks, 15 to 20 hours of your review time, and no spending beyond the model API.

| Week | Sessions | Your time |
| --- | --- | --- |
| 1 | S0 skeleton, S1 capture and links, S2 court record (likely two runs) | Setup hour; two report reviews; the domain table and the gated-portal list |
| 2 | S3 documents, S4 identity, S5 news | Label the 150 pairs (about 3 hours); the F3 audit of 50 rows (about 3 hours); one report review |
| 3 | S6 extraction and coding, S7 export and reporting, S8 review and freeze | Review the 50 sample extractions (about 2 hours); the 100-row QA review (4 to 6 hours); read the first-pass notes |

**Run time.** Rate-limit budgets, not code, set the pace: a full pass of a stage over 1,481 rows takes hours to a day. Runs resume per row, so a session starts a run and the next session reads its results.

**Model API.** Extraction over every row with text is roughly 1,500 rows times three documents times 4,000 tokens, on the order of $100 to $200 at Sonnet-class pricing and several times that on Opus; the OCR fallback adds a few dollars. The cap in `config.toml` is the real limit. The optional Google search is $5 per 1,000 queries after the free 100 a day.

**Everything else is free.** No portal, archive, index or dataset in the allowlist charges for the access the first pass uses. Disk: the CourtListener dumps are tens of gigabytes and stay off the backup; the capture store should be planned at 20 GB.

Against the Summary's promise to Jeff (foundation in two weeks, a fifty-case pilot), this plan delivers more at week three: every row attempted, the coverage matrix, and the queues that price the second pass.

## Jeff's open decisions and the first-pass defaults

None of the six decisions in the Summary blocks the first pass. Each has a default that keeps the run reversible, and each answer is a flag or a query rather than a rebuild.

| Decision | First-pass default | When Jeff answers |
| --- | --- | --- |
| 1. This depth or the original scope | Run the first pass; it is the evidence for the decision | If he chooses the original scope, S6 and S7 still hand him a provisional ideology column faster than hand coding would |
| 2. All 1,481 rows or the 1,252 convictions | All rows, with `in_scope` and case type as columns | The export filter changes; nothing is rerun |
| 3. What "independent" means | Partisan Direction is stored as the raw party or none; the mapping to his dropdown is a separate rule | The mapping rule changes at export |
| 5. Whether he codes 50 calibration cases | The 50-row sample is drawn and frozen by hash in S0, so his codes, yours and the machine's compare on the same rows | He receives the 50 rows with their captured sources, not the machine's codes |
| 6. People-search for identity | Excluded; no code path depends on it | If yes, it is a flagged stage-2 enrichment in the second pass; the North Carolina and Ohio direct downloads can enter the first pass as a separate column |

One decision from the task list still sits with you rather than Jeff: whether the companion columns live in his workbook or in a separate analysis file. The first pass writes them to a copy of the workbook under a provisional prefix, which keeps both options open.

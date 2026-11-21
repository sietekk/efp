# Election Fraud Pipeline Task List

Sep 22, 2026 · @Michael Conroy

Every task the build needs, in seven sections. Items marked **GATE** unblock something downstream and are named where they do; items marked **needs** cannot start until the named item is done. The research design is in Doc and the architecture in Doc.

## A. Accounts, access and decisions

None of these need code and several have lead times of weeks. Send every email and request in the first sitting.

### Decisions with Dr. Treistman

- [ ] **Confirm the population**: all 1,481 dispositions or the 1,252 criminal convictions. **GATE →** codebook freeze (E1), ingest scope flag (D1)
- [ ] **Define *independent*** in his dropdown: centrist, unaffiliated, or non-partisan in direction. **GATE →** codebook freeze (E1)
- [ ] **IRB status**: ask whether the project has a determination; if not, who files and on what timeline. **GATE →** social component (D10), Reddit for Researchers (A17), Meta Content Library (A18)
- [ ] **Companion columns placement**: in the deliverable workbook or a separate analysis file. **GATE →** export layout (D11)
- [ ] **Ask him to code 50 rows** as a calibration set. Feeds E4

### Accounts and keys

- [ ] **CourtListener EDU membership** on the newhaven.edu address; generate an API token; confirm the tier's request limits on the API usage page. Renews every six months. **GATE →** D4b, D4c. Without it the API allows 125 requests a day
- [ ] **PACER account** (individual), then submit the **multi-court fee exemption request** for academic researchers to `Multi-CourtExemptions@ao.uscourts.gov`. Weeks of lead time. **GATE →** federal document retrieval beyond the $30/quarter waiver (D4c). Decide in writing before wiring: fee-exempt account fetches directly and never through RECAP Fetch, because the exemption forbids internet redistribution and RECAP uploads to a public archive
- [ ] **api.data.gov key** (one key serves OpenFEC and Congress.gov). **GATE →** identity enrichment calls (D5)
- [ ] **Open States / Plural API key** at open.pluralpolicy.com. Low priority; the `people` repo covers history
- [ ] **Internet Archive account** and S3-style access keys for Save Page Now. **GATE →** archival redundancy (D2)
- [ ] **ICPSR MyData account** with the UNH address. **GATE →** NACJD and Klarner downloads (C9)
- [ ] **Media Cloud API key** at search.mediacloud.org, and email `support@mediacloud.org` asking the earliest `publish_date` for US local sources in the v4 archive. The answer decides whether Media Cloud is a pre-2015 discovery layer
- [ ] **LLM provider decision and API key** for extraction. Record the model ID. **GATE →** D8
- [ ] **FollowTheMoney free account** (myFollowTheMoney) for the API and bulk export. **GATE →** C4, urgent
- [ ] **Hunchly** trial or licence, or settle on a manual PDF-capture convention for licensed databases. **GATE →** manual import path (D7d)

### Library

- [ ] **Email Joe Scollo** (`jscollo@newhaven.edu`) in one message: (1) can NewsBank grant a research data licence for a defined 1,481-case extract; (2) can the library establish or use **Perma.cc registrar** affiliation for unlimited links; (3) does CT `researchIT` add newspaper coverage beyond the A-Z list; (4) does UNH license Docket Alarm through any vLex or Fastcase bundle; (5) is a Dave Leip's Atlas licence held. **GATE →** D2 (Perma), pre-2015 strategy (E2)
- [ ] **Test off-campus access** to Access World News, Newspapers.com and Westlaw Campus Research with the UNH login; note any on-campus-only restrictions. **GATE →** E2 pilot on pre-2015 rows
- [ ] **Run five sample state cases through Westlaw Campus Research** public records to see what it actually returns. **GATE →** decision on how much state docket adapter work is needed (D4e)

### External requests

- [ ] **Email Ballotpedia** (`data@ballotpedia.org`): academic or non-commercial access, and historical candidate coverage back-year. Unknown price
- [ ] **Reddit for Researchers** application — **needs** IRB determination; institutional email and sponsor. Only if stage 4 surfaces Reddit handles
- [ ] **Meta Content Library** application via ICPSR/SOMAR — **needs** IRB; 2–6 week review; use the Meta Secure Research Environment route, which is free, not the ICPSR VDE, which now charges. Only if stage 4 surfaces Facebook or Instagram accounts
- [ ] **X API Basic** ($200/month) — decide only if stage 4 surfaces X handles at volume; default is not to buy

## B. Environment and repository

- [ ] **Python 3.12 via `uv`**; initialise the `efp` repository with `pyproject.toml`, `uv.lock`, `ruff`, `mypy`, `pytest`. **GATE →** everything in D
- [ ] **System dependencies via Homebrew**: `tesseract` with English data, `ghostscript`, `qpdf` (OCRmyPDF needs these), `libpostal` only if `pypostal` is used
- [ ] **Docker Desktop**; pull `webrecorder/browsertrix-crawler:1.13` and the Bellingcat `auto-archiver` image; run each once against a known URL to confirm they work. **GATE →** D2
- [ ] **Data directory** `~/efp-data/` with `pipeline.db`, `store/`, `manifests/`, `bags/`; confirm it is outside the repo and in `.gitignore`
- [ ] **Encrypted external drive** and a nightly `rsync` of `~/efp-data/`; verify a restore once
- [ ] **`.env.example`** listing every key from section A; `.env` untracked; `config.toml` with per-domain rate budgets at 80% of published limits, batch sizes, thresholds, User-Agent string naming the project, UNH and a contact address
- [ ] **GitHub repository**, private for now; Actions workflow running unit, cassette-based contract, and integration tests on push; live smoke tests excluded from CI
- [ ] **Pre-commit hooks** for `ruff` and a secret scanner
- [ ] **Licence choice for the code** (MIT or Apache-2.0) recorded in `LICENSE`, with the licence register in `docs/`
- [ ] **Perma.cc API key** once registrar affiliation exists — **needs** A library email answered

## C. Reference data

Downloaded once, hashed, loaded into `ref_*` tables. Bulk before API is a rule: the candidate universe is matched locally and the APIs only enrich confirmed matches.

- [ ] **C1. The source workbook**, copied byte-for-byte into `~/efp-data/source/` and hashed. **GATE →** D1 ingest, everything after
- [ ] **C2. FollowTheMoney state candidate data** — export through the API or bulk download for every state, all cycles through 2024, and hash the archive. **Do this first.** FTM is officially unmaintained mid-migration and OpenSecrets already retired its API. **GATE →** D5 identity candidates for state-level public figures
- [ ] **C3. DIME v4.0** recipients file and the SQLite build from Stanford. ODC-BY. **GATE →** D5; this resolves more public-figure lookups than anything else
- [ ] **C4. FEC Candidate Master** `cn*.zip` 1999–2026 and Candidate–Committee Linkage `ccl*.zip`. Public domain. **GATE →** D5 federal candidates
- [ ] **C5. Harvard local elections database** 1989–2021 from OSF (CC-BY 4.0); load with the imputed-party flag preserved as its own column
- [ ] **C6. `unitedstates/congress-legislators`** current, historical and executive YAML (CC0); this is the ID crosswalk hub
- [ ] **C7. OpenStates `people` repository** clone, including `retired/` and `municipalities/` (CC0)
- [ ] **C8. Census 2010 surname file** (`names.zip`) and **SSA name files**, national and by state (`names.zip`, `namesbystate.zip`). **GATE →** D5 population prior
- [ ] **C9. ICPSR downloads** — Klarner State Legislative Election Returns 1967–2022; browse NACJD for anything covering election offences. **needs** A ICPSR account
- [ ] **C10. CourtListener bulk CSVs** — courts, dockets, opinion clusters; the next quarterly drop is expected around 2026-09-30, so wait for it rather than loading the June set. Public Domain Mark. **GATE →** D4b offline docket search
- [ ] **C11. Public Integrity Section annual reports** *Federal Prosecution of Election Offenses*, all editions covering 2000–2012; capture the PDFs and index the case tables. **GATE →** federal rows pre-2013 in D4a
- [ ] **C12. FEC Combined Federal/State Disclosure and Election Directory** PDF — the authoritative index of which state agency holds candidate filings; cite it in the methods section
- [ ] **C13. WhatsMyName `wmn-data.json`** pinned to a commit, SHA-256 recorded; **Maigret site database** pinned with its `data_sha256`, auto-update disabled in config. **GATE →** D10
- [ ] **C14. 3DLNews outlet directory** (14,000 US local outlets by county) — use as the map of which local paper to query per case state, not as a text source
- [ ] **C15. NCSL State Primary Election Types page** and the **EAC EAVS** report — authoritative citations for which 18 states record no party at registration; needed for the codebook footnote

## D. Codebases in build order

The order retires risk first. Each item produces something runnable on its own, so progress is showable at any point. **needs** and **GATE** are explicit; anything not marked needs only the item above it.

### D1. Skeleton — `db.py`, `runner.py`, `cli.py`, `export/`

- [ ] Schema from the spec: `rows` with the anti-update trigger, `case_stage`, `captures`, `identifiers`, `identity_candidates`, `decisions`, `extractions`, `codes`, supporting tables, `audit_log`; WAL mode; migrations as numbered SQL files
- [ ] `ingest`: load C1, hash each row's nine cells, set `in_scope`, seed `case_stage` with `pending` for every stage
- [ ] `runner.py`: the eligibility query, batch claiming in one transaction, gate evaluation with `gate_reason`, retry cap, `input_hash` idempotence
- [ ] `cli.py` with `ingest`, `run`, `status`, `review`, `import`, `audit`, `export`, `manifest`, `selftest` stubs; every invocation writes `audit_log`
- [ ] `export`: copy the original workbook, append the thirteen columns from J, preserve the data-validation dropdown on Treistman's column, write the redacted CSV; **test that the nine source columns hash identically after export**
- [ ] `efp status` printing counts by stage × status — this is the report Treistman sees

**GATE →** every later item. **needs** B, C1.

### D2. `capture.py`

- [ ] Content-addressed `store/` writer; SHA-256; `captures` insert with tool and version
- [ ] WARC writing via `warcio` for plain HTTP fetches; Browsertrix subprocess wrapper returning WACZ for JS-heavy pages
- [ ] Save Page Now client (`savepagenow`) with the \~6/min authenticated budget as a background queue; Perma client for cited URLs
- [ ] `efp manifest`: weekly list of new `content_sha256` values, hashed, RFC 3161 timestamped via `rfc3161-client` against two TSAs; token stored
- [ ] BagIt bag over `store/` with SHA-256 and SHA-512; `bag.validate()` wired into `selftest`
- [ ] Auto Archiver `orchestration.yaml` and a wrapper that feeds it a CSV of confirmed-relevant URLs and writes results back to `captures`

**GATE →** D3, D4, D7, D10. **needs** A Internet Archive keys, B Docker.

### D3. `resolve_links`

- [ ] Parse the Source cell into individual URLs; handle comma and space separators and bare `herit.ag/...` without scheme
- [ ] Live expansion with redirect cap 10, 10 s timeout, `requests-cache` in front, `pyrate-limiter` per domain, `stamina` retries
- [ ] Wayback CDX fallback on the shortener URL, then the target; `cdx-toolkit` against Common Crawl as second chance
- [ ] Record `expansion_method`; capture every body; report the link-rot rate

**GATE →** D4 (the coverage names the county and court), first numbers to Treistman. **needs** D1, D2.

### D4. `court`

- [ ] **D4a. DOJ API client** — title-keyword search, client-side `topic` filtering, `pagesize` 50, under 4 req/s; index C11 PIN reports for pre-2013 rows
- [ ] **D4b. CourtListener** — official SDK client; loader for C10 bulk CSVs into a local table; search by party name, state, year window. **needs** A EDU token
- [ ] **D4c. RECAP Fetch** — docket and PDF requests, 30/min. **needs** A PACER decision on exemption versus RECAP
- [ ] **D4d. State AG and DA harvester** — framework forked from the `warn-scraper` per-state module pattern; `AGScraper` protocol; modules for the ten states with the most rows first; cassette fixtures per module; `state_adapters` rows with status. **GATE →** coverage matrix (G2)
- [ ] **D4e. State docket adapters** — framework with `automated | assisted | manual` modes; fork and repair `court-scraper`'s Wisconsin CCAP and Oklahoma OSCN modules; `assisted` mode constructs the search URL and parks the row in `needs_review`. **needs** A Westlaw sample test to decide scope
- [ ] Extract identifiers from located records into `identifiers` with spans. **GATE →** D5

### D5. `identity`

- [ ] Reference loaders for C2–C8 into `ref_*` tables with an ID crosswalk keyed on `congress-legislators`
- [ ] Dual name parsing (`nameparser` + `probablepeople`), disagreement queued for review; alias expansion from `aka`
- [ ] Population prior: `N_namesakes` from Census × SSA × state adult population by cohort, with the floor value for absent surnames recorded
- [ ] Splink 4.x model: comparison levels from the spec, TF adjustment on with `tf_minimum_u_value` set, `m`/`u` from the 150 labelled pairs (E3) plus published values; settings JSON versioned and hashed
- [ ] Posterior = prior × evidence; thresholds 0.99 / 0.90; `needs_review` band; `efp review identity` showing waterfall and spans; `decisions` written for every outcome
- [ ] `efp identity explain <row>`

**GATE →** D7 and D10 at volume (they need confirmed identities). **needs** D4 identifiers, C2–C8, E3.

### D6. `documents`

- [ ] `pdfplumber` text with positional data for born-digital PDFs; `pypdf` for splitting and metadata
- [ ] OCRmyPDF pass for scanned filings, original bytes retained and hashed separately; quality score from dictionary-word ratio and mean confidence; PaddleOCR fallback below threshold
- [ ] Docling conversion storing page, bbox and charspan per text item

**GATE →** D8 on court documents. **needs** D4 records, B system deps.

### D7. `news`

- [ ] **D7a.** GDELT DOC 2.0 client (`gdeltdoc`) for 2017–2025 rows, name plus state plus charge language, 250-result cap handled
- [ ] **D7b.** Wayback and Common Crawl retrieval for dead URLs from D3
- [ ] **D7c.** `trafilatura` + `htmldate` extraction; second extractor on disagreement; short bodies flagged
- [ ] **D7d.** Manual import path: `efp import --row --source newsbank|newspapers_com|westlaw <file>`; hashes, `manual_source` set, text extracted via D6, `news` stage advanced. **GATE →** pre-2015 rows can be processed at all
- [ ] **D7e.** Media Cloud discovery, only if the archive-depth email says it reaches pre-2015

**needs** D5 confirmed identities for volume runs; D7d needs only D1, D2, D6.

### D8. `extract`

- [ ] LangExtract extraction classes from the spec; prompts in `prompts/` and hashed; temperature 0; `char_interval` and `alignment_status` persisted; `unaligned` never eligible for coding
- [ ] Instructor fallback with the validated `substring_quote` pattern, kept behind the same interface
- [ ] Validation harness on scikit-learn: per-field `classification_report` with abstention as a class, span-grounding score, Krippendorff's α and Cohen's κ, bootstrap CIs; results appended and keyed to the gold-set hash

**GATE →** D9. **needs** E2 gold set, A LLM key, D6/D7 text.

### D9. `code`

- [ ] `codebook.yaml` interpreter applying ordered rules; writes `codes` with rule fired, extraction used, codebook version
- [ ] `codebook/CHANGELOG.md` and a `efp recode --since <version>` that identifies rows affected by an amendment
- [ ] Disagreement between model proposal and reviewer lands in `needs_review`

**GATE →** D11 export at volume, G4. **needs** E1 frozen codebook, D8.

### D10. `social` — last, and blocked until IRB is answered

- [ ] `allowlist.json` pre-registered with Treistman, hashed
- [ ] Naminter wrapper with input type `Handle | Email` only; raw responses stored; `known`-username self-test at session start written as a calibration capture
- [ ] Maigret with `ranked_sites_dict(names=allowlist)`, auto-update and recursion disabled
- [ ] `atproto` client for Bluesky public reads; Reddit and Meta only through their approved research programmes if granted

**needs** A IRB, C13, D5 confirmed identities, at least one handle in `identifiers`.

### D11. `export` at volume and D12. reporting

- [ ] Full export with the redaction rules for `Private individual` rows
- [ ] `docs/coverage_matrix.md` generated from `state_adapters`
- [ ] Reliability report generator pulling from the harness results and the identity audit
- [ ] `efp audit <row>` walking code → extraction → capture and printing the span in situ

## E. Manual pilot and calibration

The pilot comes *before* automating stage 1, because it is what tells you which adapters are worth writing. Automating from a guess is how a semester disappears.

- [ ] **E1. Codebook v1** — taxonomy, decision rules, the `Nonpartisan race` value, the registration footnote; draft now, **freeze after the pilot**, version and hash. **needs** A population decision, A *independent* definition. **GATE →** D9, E2, E4
- [ ] **E2. 50-row manual pilot** — stratified across year bands (2000–09, 2010–16, 2017–25), fraud types and states; run all five stages by hand; time each stage per row; record which source answered each row and where it stalled. **needs** D1, D3, A library access test. **GATE →** adapter priority list for D4d/D4e, the schedule estimate for pre-2015 rows, the first report to Treistman (link-rot rate, stage-resolution rates, provisional motive distribution)
- [ ] **E3. Identity labelled set** — 150 candidate pairs hand-adjudicated as match / non-match / unresolvable, oversampling common surnames. **GATE →** D5 `m`/`u` parameters and the stability chart
- [ ] **E4. Gold set** — 200–300 rows stratified by year band, fraud type and evidence tier; double-coded by two humans; disagreements resolved by consensus; split 60/40 development / held-out; frozen and hashed **before any prompt is written**. **needs** E1. **GATE →** D8 validation, F4
- [ ] **E5. Treistman calibration** — the 50 rows he agreed to code, compared to yours; any systematic difference feeds a codebook amendment before volume coding. **needs** A decision, E1
- [ ] **E6. Site allowlist** for the social stage, pre-registered with Treistman and hashed. **needs** A IRB answer. **GATE →** D10

## F. Verification checkpoints

Each checkpoint gates running the next stage at volume. None are optional; they are the difference between a dataset and a spreadsheet.

- [ ] **F1. Export round-trip** — the nine source columns hash identically after `efp export`; the dropdown on Treistman's column survives. **GATE →** any export leaving your machine. **needs** D1
- [ ] **F2. Link-rot report** — measured expansion rate by method and by year band. **needs** D3. Goes in the first report to Treistman
- [ ] **F3. Identity confirmation audit** — 50 `confirmed` rows re-derived by hand from the documents, oversampling common surnames and the review band; false-confirmation rate with a bootstrap CI, reported separately for common-name and rare-name strata; parameter-stability chart attached. **needs** D5, E3. **GATE →** D7 and D10 at volume. If the rate is not near zero, tighten thresholds before any row moves downstream
- [ ] **F4. Extraction validation** — per-field precision, recall and F1 on the held-out gold set with abstention as its own class; span-grounding accuracy; model–human α and κ beside human–human α; bootstrap CIs. **needs** D8, E4. **GATE →** D9 at volume. Re-run on every prompt or model change; results appended, never overwritten
- [ ] **F5. Coding reliability** — random 15% (about 220 rows) coded independently by a second coder blind to the first code, or self-recoded after a several-week washout and reported as intra-coder; Krippendorff's α per field; below \~0.70, sharpen the codebook rather than code more. **needs** D9 at volume
- [ ] **F6. Sensitivity re-run** — the substantive distributions recomputed at identity threshold 0.995 and with `Circumstantial`-tier codes excluded; conclusions should not move. **needs** F5
- [ ] **F7. Reproducibility check** — fresh clone, `uv sync`, pinned data files, the captured store: `codes` reproduces exactly. **needs** D9. Run once before the December deliverable and once before any publication
- [ ] **F8. Provenance integrity** — `efp manifest` has run weekly with RFC 3161 tokens stored; the BagIt bag validates; `efp audit` on ten random coded rows prints the span inside the captured page. **needs** D2, D9
- [ ] **F9. Selftests green** — WhatsMyName and Maigret known-username checks; adapter smoke tests; append-only triggers; orphan capture scan. **needs** D2, D4, D10
- [ ] **F10. Clustering caveat documented** — the 339 co-defendant rows flagged with `Case Group`, and the analysis notebook clusters standard errors on it. **needs** D9

## G. Documentation and delivery

Written as you go, not at the end. The documentation is a deliverable in its own right and the toolchain's portfolio value lives here.

- [ ] **G1. README and reproducibility guide** — setup from a fresh machine, the account list from section A, the data directory layout, how to run each stage, how to run the audit
- [ ] **G2. Coverage matrix** — `docs/coverage_matrix.md` generated from `state_adapters`: every state, its AG source, its docket portal, mode (automated / assisted / manual / none), last verified date. **needs** D4d, D4e. This is the artefact nobody else has built
- [ ] **G3. Methods appendix** — identity model and error rates, extraction validation, reliability statistics, sensitivity analysis, licence register, the deliberate-exclusion statement on data brokers and voter files, the Heritage sample-selection limitation, the clustering note. **needs** F3–F6
- [ ] **G4. Codebook** with CHANGELOG, final version, hashed. **needs** F5
- [ ] **G5. Coded workbook v1** and the **redacted analysis CSV**. **needs** F1, D9 at volume
- [ ] **G6. Reliability report** for Treistman — the numbers from F2–F6 in a form that goes straight into a paper. **needs** F6
- [ ] **G7. RO-Crate package** — bag, database export, codebook, gold set, prompts, model settings JSON, licence register. **needs** F7, F8
- [ ] **G8. Public repository hygiene** — confirm no `pipeline.db`, `store/`, `.env`, cassettes containing real subjects, or row-level identity data anywhere in history; licence file present; excluded-tools list in the README. **needs** G1. **GATE →** making the repository public
- [ ] **G9. First report to Treistman** — by the pilot milestone: link-rot rate, per-stage resolution rates, provisional motive distribution, the pre-2015 time estimate, and the three open questions. **needs** E2, F2
- [ ] **G10. Semester deliverable** — G3–G6 assembled; if pre-2015 retrieval is still running, deliver the post-2017 population fully coded and state the plan for the remainder rather than thinning the evidence standard

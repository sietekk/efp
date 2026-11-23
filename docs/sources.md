# Sources (court stage, build 07; reference datasets, build 11)

Every one of these is reached only through the capture layer (`efp/capture.py`): budgeted per
domain, every response a `captures` row. "Verified" below means the
fact is stated in `docs/spec.md`, `docs/plan.md` or `docs/tool-register.md`. "Assumed" means this
build had to pick a value that no project document states and could not check against the live
service from the build environment; the first real run is the check, and the code fails loudly
or abstains rather than guess.

| Source | Where | Auth | Budget | Used by | Status |
| --- | --- | --- | --- | --- | --- |
| DOJ press-release API v1 | `https://www.justice.gov/api/v1/press_releases.json` | none | 3 requests/s (published limit: under 4) | `court.doj` | endpoint and limit verified; the `parameters[title]` query parameter is **assumed** (spec: "by title keyword"). The client checks that the titles returned contain the keyword and, if not, falls back to harvesting the date-sorted feed (`sort=date&direction=DESC&page=N`, `pagesize=50`) into a local index and filtering there |
| DOJ release pages | the `url` of each release, on `justice.gov` | none | 3 requests/s | `court.doj` | the page is captured for citation; if the page cannot be fetched the API's own body is kept instead |
| Public Integrity Section reports | *Federal Prosecution of Election Offenses*, all editions covering 2000-2012 | none | default 1 request per 2 s | `court.pin_reports` | **URLs not supplied.** No project document lists them; set `court.pin_report_urls` in `config.toml`. Reading the PDFs needs `pdfplumber` or `pypdf` (the `documents` extra) |
| CourtListener bulk data | S3 folder listing at `court.bulk_listing_url`, files under `court.bulk_download_base` | none (public domain) | default 1 request per 2 s for the listing; one streaming GET per file | `court.courtlistener_bulk` | quarterly CSVs (spec section 5), "tens of gigabytes" (plan). The bucket address, the file names (`dockets-`, `courts-`, `parties-`, `party-types-` plus a date) and the CSV columns are **assumed**; the loader finds each column by name and stops with the list of columns it saw if one is missing |
| CourtListener REST v4 | `https://www.courtlistener.com/api/rest/v4/search/` | `COURTLISTENER_TOKEN` (free account) | 100 a day (80% of the 125/day default), 0.8/s | `court.courtlistener_api`, the RECAP documents step | endpoint and quota verified; the search parameters (`type=r`, `party_name`, `docket_id`, `filed_after`, `filed_before`) and the result field names are **assumed** from the v4 search documentation and parsed defensively |
| RECAP archive files | `https://storage.courtlistener.com/<filepath_local>` | none | default | the RECAP documents step | only filings already in the archive are captured, as PDFs for build 10 |
| RECAP Fetch | `/api/rest/v4/recap-fetch/` | token + the operator's PACER credentials | 30/min published | `court.recap` | **gated, never used in the first pass.** `court.recap_enabled` defaults to false and nothing sets it |
| PACER | direct | PACER account | $0.10/page | nothing | **excluded** (plan). The academic fee exemption forbids internet redistribution and RECAP Fetch uploads to a public archive, so the two are not combined without written clarification |

## Where each result goes

- `court_records` (one row per located document): `strategy` is the module name; `source_kind` is
  `doj_release`, `pin_report` or `federal_docket`; `document_kind` is `press_release`, `report_entry`,
  `docket` or `filing`.
- `identifiers`: rules-based extraction from the captured text, each with the verbatim span.
- `case_stage.summary_json`: `court_status` (`primary_record`, `press_release_only`, `not_found`;
  `gated_portal` is set by build 09), the federal/state/unknown classification and the signals
  behind it, and every module's outcome.
- `queues/pacer.csv` (under the data directory, `~/efp-data/queues/`): federal dockets whose filings
  are not in RECAP (or were not checked), with what is missing. It lists row ids and case numbers, so
  it stays with the data; decide there what PACER should be asked for.

## The bulk index

`efp court bulk fetch` lists the newest file of each kind with its size and downloads nothing;
`--yes` downloads (refusing above `court.bulk_max_gb`). `efp court bulk load` streams the files into
`paths.bulk/courtlistener.duckdb`, keeping federal criminal dockets filed from 1999, their parties,
and the courts (for the state). The raw files stay in `paths.bulk` (off backup). Each download is a
`captures` row whose body is a manifest (URL, size, SHA-256, ETag), so a located docket can be traced
to the file and quarter it came from.

## State AG press releases (build 08)

Probed 2026-11-22 through the capture layer. Every site is reached at its default budget (1 request
per 2 seconds) except California, whose published Crawl-delay of 10 seconds gives 0.08 requests/s.

| State | Index | Mode | Notes |
| --- | --- | --- | --- |
| TX | `texasattorneygeneral.gov/news/releases?page=N` | automated | ten releases a page; county and district attorneys wait on the domain table |
| CA | `oag.ca.gov/media/news?page=N` | automated | thirty releases a page; budget 0.08/s (Crawl-delay 10) |
| OH | `ohioattorneygeneral.gov/Media/News-Releases/<Month>-<YYYY>` | automated | by month; the cursor is the month |
| IL | `illinoisattorneygeneral.gov/news-room/` | automated | one page holds the whole archive, at present from 2024 only |
| FL, NC | news-release pages | manual | HTTP 403 to automated requests |
| MN | none | manual | no archive or search index |
| WI | `wisdoj.gov` press releases | manual | PDF files only |
| KY, IN | newsroom lists | manual | the lists are loaded by script; no server-rendered list or feed |

`docs/coverage_matrix.md` (generated by `efp status --coverage --write`) has the mode and the
reason for every state, with row counts.

## State docket portals (build 09)

`efp court docket probe` loads each portal's search entry point once, through the capture layer, and
never submits a search; it records the result per state in `state_adapters` (`kind = 'docket'`) and
`reference/docket_probe.csv` (the 2026-11-22 run). `reference/docket_portals.csv` is the seed list
(entry URLs from the builder's knowledge, **unverified**: a wrong address shows up as `unreachable`
in the probe and is fixed in that file). Modes: `automated` only for a state the operator has put in
`court.docket_terms_cleared` after reading that portal's terms and for which an adapter exists;
`assisted` where a search exists that the operator runs in a browser (the pipeline builds the
query); `manual` for no online search or an entry point that is not there.

No search was ever run against a portal during the build. The Wisconsin adapter's shapes were
checked against WCCA's own client code (its React bundle, read once on 2026-11-22), not by a search:

| Adapter | From the portal's client code | Still unconfirmed |
| --- | --- | --- |
| Wisconsin WCCA (`efp/court/adapters/docket/wi.py`) | search is `POST jsonPost/advancedCaseSearch`; the form's fields (`firstName`, `lastName`, `filingDate.start`/`.end`, `includeMissingDob`, `includeMissingMiddleName`, `countyNo`, `caseNo` …) go as plain values; the answer's list is `result.cases` with `caseNo`, `countyNo`, `countyName`, `caption` per item; case pages are `caseDetail.html` | the date format the server expects, whether items carry a filing date, any truncation keys (none found in the client) |

**WCCA is not automatable today.** Its client code shows search gated by a CAPTCHA, and its own text
says the check "protects websites from abuse and unauthorized harvesting or mining of data" and
recognises "a human user, not an automated program"; its terms also say WCCA "may limit the number of
records searched on any single request". The seed list therefore marks WCCA `human-check`, the probe
records WI `assisted` whatever `court.docket_terms_cleared` says, and the adapter is kept but unused.

The probe's observations are heuristics on one page (a login field with no search form, fee wording,
a challenge page, a page that is empty without JavaScript); they classify conservatively (a page
that shows no search form is `assisted`, never "no search exists"). Browsertrix Crawler (AGPL) runs
as a separate Docker container (`court.docket_browser_image`), never linked.

## Reference datasets (build 11)

Bulk datasets loaded once into the `ref_*` tables; raw files live under `~/efp-data/bulk/<dataset>/` and are never committed. Each load is one row of `ref_sources` (URL, time, SHA-256, size, license, attribution, rows) and each download or import is a `stage = 'reference'` capture. `efp reference status` and `efp selftest --reference` report them; re-running a load of a byte-identical file changes nothing. Retrieved 2026-11-23 on the operator's machine.

| Dataset | Part | Vintage | Size (bytes) | Rows | SHA-256 | License |
|---|---|---|---:|---:|---|---|
| `census_population` |  | 2020-2023 | 764,676 | 3,672 | `9ea3dcf173d24ba7e339352b64e060ed8d5ad9ee6b0bad0cd21b566e4ca3e03a` | Public domain (U.S. Census Bureau) |
| `congress` | current | current+historical | 1,081,187 | 539 | `bd32e62b1c89e9585e4ec49df5676563510e9435d779c061ff50139833f73a49` | CC0 1.0 (congress-legislators, unitedstates project) |
| `congress` | historical | current+historical | 8,997,407 | 12,231 | `9641615f336b3113d6f89152e3441128cbdc3f61ccf87c65da3a015076c78c91` | CC0 1.0 (congress-legislators, unitedstates project) |
| `fec_candidates` | 2000 | cycles | 166,844 | 4,529 | `b51443134d48bb6515369c2f8c5d0d808948fa970cf6db88e4f81adf06615156` | Public domain (Federal Election Commission) |
| `fec_candidates` | 2002 | cycles | 146,625 | 3,944 | `0a7a253ba6e86deb1c61d5c140acd7f8fd104a886552d6b40ea48523bde58fde` | Public domain (Federal Election Commission) |
| `fec_candidates` | 2004 | cycles | 142,800 | 3,814 | `be73bea8405b7d60897e602dc7f3cfba4b1698215df2d600c8f4b0aaa78eae65` | Public domain (Federal Election Commission) |
| `fec_candidates` | 2006 | cycles | 139,291 | 3,704 | `85d9dcb04b0fd1fec3de88c2216a9c40b5b6aeed00eeed910354845a7e4d9db5` | Public domain (Federal Election Commission) |
| `fec_candidates` | 2008 | cycles | 153,850 | 4,072 | `ae93d4f986b28ca3fad420f0cd302d959c1f6dbed47b7db7bf5c0445a93fc63b` | Public domain (Federal Election Commission) |
| `fec_candidates` | 2010 | cycles | 190,211 | 5,126 | `01b05f26fc40b4564e1a9c083a144a5bc1a8fe67489ee93d78e8ee00b691cbd0` | Public domain (Federal Election Commission) |
| `fec_candidates` | 2012 | cycles | 207,053 | 5,628 | `53a07683cbc0bf9c47c825502ed8212626313c54c63a27bc40048dc3e38ddf21` | Public domain (Federal Election Commission) |
| `fec_candidates` | 2014 | cycles | 206,265 | 5,536 | `a4f3e83e77035557e705e3e8cfe40d10f16655d38bc73c227fe02c328bcbe3cc` | Public domain (Federal Election Commission) |
| `fec_candidates` | 2016 | cycles | 271,705 | 7,396 | `7dd5b2df23b83ca408d9abd11f02486209c3bf638b3cf601ac13b0e5d19f51da` | Public domain (Federal Election Commission) |
| `fec_candidates` | 2018 | cycles | 271,951 | 7,371 | `fba6302a5c6649b8f3297a6aa8691cebf124a8788bdb12697107bc5ba55a0cd6` | Public domain (Federal Election Commission) |
| `fec_candidates` | 2020 | cycles | 283,545 | 7,758 | `f0229bb4e6b8030b074972ffb27fa163b195729b31f1276005ecca31a0728a39` | Public domain (Federal Election Commission) |
| `fec_candidates` | 2022 | cycles | 313,237 | 8,578 | `8ab8c53d0c29b2807411201d6a28d08b0294abbec0168967b22e67afdc750047` | Public domain (Federal Election Commission) |
| `fec_candidates` | 2024 | cycles | 356,337 | 9,796 | `880d4a197a2e0fa7b823957a7fb40c07d0c9499cfe55f90b7285272ff258d95e` | Public domain (Federal Election Commission) |
| `fec_candidates` | 2026 | cycles | 314,843 | 8,947 | `ca59e6fdb4bf44a31c7e546e27e71a8e90b039541b2ad0f5d02c0f1d74cf5551` | Public domain (Federal Election Commission) |
| `harvard_local` |  | 1989-2021 | 26,699,549 | 126,599 | `86cc4f80b2c9048967b18c74c46b9f932d52fbc561e0c89a86441f0186f35357` | CC-BY 4.0 (Harvard local elections database) |
| `openstates` |  | main | 20,323,384 | 21,889 | `7311a5bb1ec317d88bf437fb8cd7262fee5874126f5150721725a7e8322b79e1` | CC0 1.0 (OpenStates people repository) |
| `wmn` |  | main | 250,787 | 691 | `a8b8385bee4b4c79f1fcdd458749ec89ba9d8dab94230353d09020b90b436324` | CC BY-SA 4.0 (WhatsMyName, Micah Hoffman) |

### Where each came from

| Dataset | Source | Notes |
|---|---|---|
| `census_population` | `www2.census.gov/programs-surveys/popest/datasets/2020-2023/state/asrh/sc-est2023-agesex-civ.csv` | civilian resident population by state, single year of age, estimates 2020-2023; the loader sums to five-year bands (both sexes). Columns are `POPEST<year>_CIV` |
| `census_place_county` | `www2.census.gov/geo/docs/reference/codes2020/national_place_by_county2020.txt` | 2020 national place-by-county codes, pipe-delimited (`STATE\|STATEFP\|COUNTYFP\|COUNTYNAME\|PLACEFP\|PLACENS\|PLACENAME\|TYPE\|CLASSFP\|FUNCSTAT`); identity's "same county" city level (build 12). The 2020 relationship files, which carry land areas, were not found at a stable URL |
| `fec_candidates` | `fec.gov/files/bulk-downloads/<year>/cn<yy>.zip` (redirects to the FEC's S3 bucket; the loader follows it) | one file per two-year cycle 2000-2026; `cn.txt`, pipe-delimited, no header |
| `congress` | `unitedstates.github.io/congress-legislators/legislators-{current,historical}.yaml` | CC0; the bioguide/FEC/OpenSecrets/ICPSR crosswalk |
| `openstates` | `github.com/openstates/people/archive/refs/heads/main.zip` | the `legislature/` and `retired/` folders of every state; the executive and municipal folders are not read |
| `harvard_local` | OSF project `mv5e6` (`Replication/ledb_candidatelevel.csv`), found through the OSF API | candidate-by-election rows; `pid_est` is the database's own party estimate, so **every party value is loaded as imputed** (`party_imputed = 1`) |
| `wmn` | `raw.githubusercontent.com/WebBreacher/WhatsMyName/main/wmn-data.json` | pinned at `~/efp-data/bulk/wmn/wmn-data.json`; its hash is also an instrument (`wmn_data`); read only by build 15 |

### Not loaded (gated), and why

| Dataset | Reason | What to do |
|---|---|---|
| `census_surnames` (Census 2010 surnames, `names.zip`) | the host returns a "Request Rejected" web page to the capture layer, with the project identity **and** with a browser-style `User-Agent` (`reference.download_user_agent`, see below) | download `names.zip` in a browser and run `efp reference import census_surnames names.zip` |
| `ssa_names` (SSA `names.zip` and `namesbystate.zip`) | HTTP 403 for the same two attempts | download both in a browser, then `efp reference import ssa_names names.zip` and `efp reference import ssa_names namesbystate.zip` |
| `dime` (DIME v4.0 recipients) | no download URL is configured: the Stanford Data Repository serves it through its own page and the SQLite build may be very large | set `reference.dime_url` to the file's address, or download it and `efp reference import dime <file>`; `reference.max_gb` caps a download |
| `ftm` (FollowTheMoney state candidates) | no export URL is configured; the site is unmaintained and its API needs an account key | if you have an export, `efp reference import ftm <file>`; otherwise the candidate index simply lacks FTM |

**The `User-Agent` override (an operator decision).** `reference.download_user_agent` is empty by default. When set, a download from a host in `reference.download_user_agent_hosts` that was refused (HTTP error or a web page where a file should be) is tried once more with that `User-Agent`; nothing else about the request changes, no challenge is solved, and the capture's manifest records `headers_overridden: ["User-Agent"]`. It was tried for Census and SSA on 2026-11-23 and did not get past either host's filter, so those two are imported by hand.

### Attribution sentences for the paper

- State population by age: U.S. Census Bureau, Population Estimates Program, State Population by Characteristics.
- Federal officeholders and ID crosswalk: the congress-legislators project (CC0).
- Federal candidates: Federal Election Commission, Candidate Master bulk files.
- Local candidates 1989-2021: Harvard Local Elections Database (Kuriwaki et al.), doi:10.17605/OSF.IO/MV5E6, CC-BY 4.0. Party is imputed where the race is nonpartisan.
- State legislators, current and retired: the OpenStates people repository (CC0).
- Site definitions: WhatsMyName project, Micah Hoffman (WebBreacher), CC BY-SA 4.0; any list derived from it is shared under the same license.
- Population prior: Census 2010 surnames (U.S. Census Bureau) and SSA baby names (Social Security Administration), public domain, once imported.
- Candidate universe: DIME v4.0 (Bonica, Stanford University Libraries, ODC-BY 1.0) and FollowTheMoney (National Institute on Money in Politics), once imported.
- Party in these tables is an identity signal for public figures only; it is never evidence of motive (build 18 enforces this). The Harvard database's party values are estimates and are flagged `party_imputed`.

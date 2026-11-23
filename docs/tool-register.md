# Tool register

Verified September 2026 against PyPI release data, repository source and live documentation. Versions and dates are as checked, not as remembered. Anything marked dead or stale is listed so it can be named and rejected in the methods section rather than found by a reviewer.

## Confirmed available through UNH

Checked against the library's A-Z database list, September 2026. Contact for all of these: Joe Scollo, University Librarian, `jscollo@newhaven.edu`, (203) 479-4186 — subject areas Criminal Justice, National Security, News & Newspapers, Political Science.

| Resource | Coverage | Use in this project |
| --- | --- | --- |
| **Access World News (NewsBank)** | Thousands of papers, early 1980s onward, incl. small local markets | **The pre-2015 solution.** No API, no bulk download. Ask about a research data licence |
| **ICPSR** | Downloadable datasets, free member access | National Archive of Criminal Justice Data; Klarner state legislative election returns. Create a MyData account |
| **Campus Research (Westlaw)** | Case law, statutes, **public records**, news | Available to all students, not just Legal Studies. Test against sample state cases |
| **Newspapers.com** | Digitized small-town papers | Use the separate A-Z entry, not the Ancestry link |
| **Historical Newspapers (ProQuest)** | Hartford Courant 1764–1922, NYT 1851–2003 | NYT depth to 2003 is useful |
| **Hartford Courant** | 1992–present | The Connecticut cases: Bridgeport primary, DeFilippo, Cuevas, Rivernider |
| **US Major Dailies (ProQuest)** | Chicago Tribune, LA Times, NYT, WSJ, WaPo | National coverage of the larger cases |
| **Newspaper Source Plus (EBSCO)** | 860+ papers, TV and radio transcripts | Transcripts occasionally carry statements not in print |
| **HeinOnline** | Law journals, Federal Register, US Code, CFR | Legal framing and statute text |
| **NCJRS** | Criminal justice reports and statistics | Federal, free |
| **Homeland Security Digital Library** | Local, state and federal agency reports | Framing and literature |
| **Criminal Justice (ProQuest)**, **Criminal Justice Abstracts**, **SocINDEX** | Scholarly | Literature review |
| **Ancestry Library Edition** | Court, land, probate, vital records, directories | Identity corroboration only, under the stage-2 standard. Off-campus needs a password from the library |
| **researchIT CT / Connecticut Digital Library** | State-provided to all CT academic libraries | Worth asking whether it adds newspaper coverage beyond the UNH list |
| **GovInfo.gov**, **Data.gov** | Federal publications and datasets | GovInfo carries the USCOURTS opinion collection |
| **New York Times web and app** | Full access | Free with a UNH email, must register from a library computer |

**Confirmed absent: Nexis Uni / LexisNexis.** Not licensed by UNH. This removes the only high-throughput news API from consideration, and with it the LexisNexis clause prohibiting LLM use of retrieved text. Also absent: ProQuest TDM Studio, Factiva, full Bloomberg Law (only the Tax module, restricted to tax courses), and general Westlaw beyond Campus Research.

## Court records and legal data

| Tool | Version / date | Licence | Status | Note |
| --- | --- | --- | --- | --- |
| [courtlistener-api-client](https://github.com/freelawproject/courtlistener-api-client) | 1.2.0, 2026-08-04 | BSD-2 | Active | Official Free Law Project SDK, async, 24+ endpoints |
| [CourtListener bulk data](https://wiki.free.law/c/courtlistener/help/api/bulk-data/bulk-legal-data) | Quarterly, next \~2026-09-30 | Public Domain Mark | Active | No membership, no rate limit. The right entry point for a fixed 1,481-row set |
| [juriscraper](https://github.com/freelawproject/juriscraper) | 3.0.41, 2026-09-14 | BSD-2 | Active | 206 state opinion scrapers, 24 PACER modules. Opinions not dockets |
| DOJ API v1 | Live, no key | US Gov PD | Active | `justice.gov/api/v1/press_releases.json`. Keep under 4 req/sec. Coverage starts \~2013; `parameters[topic]` is silently ignored, filter client-side |
| [warn-scraper](https://github.com/biglocalnews/warn-scraper) | 2026-09-03, 212 releases | Apache-2.0 | Active | Not for this data — the architectural template for a 50-state scraper |
| [court-scraper](https://github.com/biglocalnews/court-scraper) | 0.1.4, 2022-12-13 | ISC | **Stale** | 6 states. Fork and repair, do not depend on |
| Caselaw Access Project | 1658–2018 | Open | Folded into CourtListener | State appellate only; good for 2000–2018 |
| Trellis / UniCourt | — | Commercial | Active | 46 states / 19 states. Quote-only, no academic tier advertised |

## News and archives

| Tool | Version / date | Licence | Status | Note |
| --- | --- | --- | --- | --- |
| [trafilatura](https://github.com/adbar/trafilatura) | 2.2.0, 2026-07-31 | Apache-2.0 | Active | Best extraction accuracy (F .924). Primary choice |
| [htmldate](https://github.com/adbar/htmldate) | 1.10.0, 2026-06-01 | Apache-2.0 | Active | Recovers publication dates from archived HTML |
| [wayback](https://github.com/edgi-govdata-archiving/wayback) (EDGI) | 0.5.1, 2026-06-19 | BSD-3 | Active | Built-in rate limiting. Use over waybackpy |
| [cdx-toolkit](https://github.com/cocrawler/cdx_toolkit) | 0.9.39, 2026-06-01 | Apache-2.0 | Active | Queries Common Crawl and Internet Archive behind one API |
| [news-please](https://github.com/fhamborg/news-please) | 1.6.16, 2025-09-21 | Apache-2.0 | Active | Its CC-NEWS module filters the Common Crawl news archive |
| GDELT DOC 2.0 API | 2017 onward | Free | Active | 250 results max per query, no key. **Starts 2017, not 2015** |
| [Media Cloud](https://github.com/mediacloud/api-client) | 5.1.0, 2026-05-19 | Apache-2.0 | Active | Metadata only, text not downloadable for copyright reasons. Archive depth undocumented |
| Nexis Data Lab / REST API | — | Licensed | Active | 749 searches/hour. **Terms prohibit LLM/ML use of retrieved text** |
| ProQuest TDM Studio | — | Licensed | Active | In-platform Jupyter; export derived data only |
| NewsBank Access World News | Early 1980s onward | Licensed | Active | Deepest local coverage. No API, no bulk scraping. Negotiated research licence only |
| [3DLNews](https://arxiv.org/abs/2408.04716) | 1996–2024 | Academic | — | \~1M article links, 14,000 outlets. Value is the outlet directory, not the text |
| newspaper3k | 0.2.8, 2018 | MIT | **Dead** | 8 years. Still widely recommended in tutorials. Use trafilatura |
| NewsAPI / GNews / Brave / SerpApi | — | Paid | Active | **None reach 2000–2015.** Do not spend budget here |
| Bing News Search | — | — | **Retired 2025-08-11** | Ignore any guide recommending it |

## Entity resolution

| Tool | Version / date | Licence | Status | Note |
| --- | --- | --- | --- | --- |
| [Splink](https://github.com/moj-analytical-services/splink) | 4.0.17, 2026-09-03 | MIT | Active | Fellegi-Sunter, DuckDB, term-frequency adjustment, per-pair waterfall explanation. Pin to 4.x |
| [nameparser](https://github.com/derek73/python-nameparser) | 2.3.0, 2026-09-12 (the lock has 3.1.0; efp uses the 3.x `nameparser.parse`, floor `>=3.1`) | **LGPL** | Very active | Rule-based |
| [probablepeople](https://github.com/datamade/probablepeople) | 0.5.6, 2024-10-29 | MIT | Semi-active | CRF-based. Run both and treat disagreement as a review flag |
| [rapidfuzz](https://github.com/rapidfuzz/RapidFuzz) | 3.14.6, 2026-08-30 | MIT | Very active | C++/SIMD. Full 1,481² matrix in under a second |
| [jellyfish](https://codeberg.org/jpt/jellyfish) | 1.2.1, 2025-10-11 | MIT | Active | Phonetic encodings. Note the move to Codeberg |
| [Census 2010 surnames](https://www.census.gov/topics/population/genealogy/data/2010_surnames.html) | 162,253 names | US Gov PD | — | Surnames occurring 100+ times |
| [SSA baby names](https://www.ssa.gov/oact/babynames/limits.html) | 1880–present, by state | US Gov PD | — | Condition given names on birth cohort |
| dedupe / recordlinkage | 2024-08 / 2023-07 | MIT / BSD-3 | **Stale** | Reference implementations, not dependencies |
| [Name Match](https://github.com/urban-labs/namematch) | 1.2.1, 2022-11-30 | AGPL-3.0 | **Stale** | Built for criminal-justice linkage. Read the algorithm docs even though the code is stale |
| abydos | 0.5.0, 2020 | GPL-3.0+ | **Dead** | Will not install cleanly. Vendor a Double Metaphone implementation instead |

## Documents and extraction

| Tool | Version / date | Licence | Status | Note |
| --- | --- | --- | --- | --- |
| [Docling](https://github.com/docling-project/docling) | 2.129.0, 2026-09-18 | MIT | Very active | **Every text item carries page number, bounding box and character span.** LF AI & Data governed. Best fit for the traceability requirement |
| [pdfplumber](https://github.com/jsvine/pdfplumber) | 0.11.10, 2026-06-15 | MIT | Active | Per-character positional data. Good for stripping captions and line numbers from filings |
| [OCRmyPDF](https://github.com/ocrmypdf/OCRmyPDF) | 17.12.1, 2026-09-16 | MPL-2.0 | Very active | Adds a text layer while preserving the original page image — evidentially important |
| [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) | 3.7.0, 2026-06-11 | Apache-2.0 | Very active | Fallback for degraded scans Tesseract mangles |
| PyMuPDF | 1.28.2, 2026-08-06 | **AGPL** or paid | Very active | Fastest, but AGPL propagates if you publish the pipeline |
| [LangExtract](https://github.com/google/langextract) | 1.7.0, 2026-09-13 | Apache-2.0 | Active | Native `char_interval` + `alignment_status`. **Tells you when the model made something up.** Prototype this first |
| [Instructor](https://github.com/567-labs/instructor) | 1.17.0, 2026-09-09 | MIT | Very active | Fallback, with the documented validated-substring-quote pattern for citations |
| Nougat | 0.1.17, 2023 | MIT | **Dead** | \~3 years |

## Provenance and capture

| Tool | Version / date | Licence | Status | Note |
| --- | --- | --- | --- | --- |
| [Bellingcat Auto Archiver](https://github.com/bellingcat/auto-archiver) | 1.2.9, 2026-09-01 | MIT | Active | SHA-256 + RFC 3161 timestamps + WACZ + OpenTimestamps + CSV feeder. Chain of custody essentially free |
| [warcio](https://github.com/webrecorder/warcio) | 1.8.1, 2026-03-31 | Apache-2.0 | Active | Only maintained pure-Python WARC reader *and* writer |
| [Browsertrix Crawler](https://github.com/webrecorder/browsertrix-crawler) | 1.13.2, 2026-06-20 | **AGPL** | Active | Docker. WARC and WACZ output for JS-heavy pages |
| [bagit-python](https://libraryofcongress.github.io/bagit-python/) | 1.9.0, 2025-06-13 | Public Domain | Active | RFC 8493 fixity manifests |
| [rfc3161-client](https://github.com/trailofbits/rfc3161-client) | 1.0.9, 2026-09-14 | Apache-2.0 | Active | Trusted timestamps. The strongest answer to "prove you had this on that date" |
| savepagenow | 1.3.1 | MIT | Not used | Named in the spec, but it calls `requests` directly, which would bypass the capture layer's budget, cache, WARC and secret stripping. Build 05 calls the SPN2 API through the capture layer instead. Verified from installed metadata |
| [ro-crate-py](https://github.com/ResearchObject/ro-crate-py) | 0.15.1, 2026-07-10 | Apache-2.0 | Active | Research data packaging |
| Perma.cc | — | Free for academic registrars | Active | For the URLs you actually cite, not all 5,000 |
| OCCRP Aleph | — | MIT | **Sunset Dec 2025** | Do not build on it. `followthemoney` (4.11.0) survives and its Person schema is worth reusing |

## Handle pivot and platforms

| Tool | Version / date | Licence | Status | Note |
| --- | --- | --- | --- | --- |
| [Naminter](https://github.com/3xp0rt/Naminter) | 1.0.9, 2026-08-13 | MIT | Active | Library-first. 8-value status enum, retains raw responses, built-in per-site self-test |
| [Maigret](https://github.com/soxoj/maigret) | 0.6.6, 2026-09-18 | MIT | Active | 5,897 sites. Use `ranked_sites_dict(names=[...])` to pin an allowlist. Disable auto-update |
| [WhatsMyName](https://github.com/WebBreacher/WhatsMyName) | Data, 717 sites | **CC BY-SA 4.0** | Active | Two-factor detection (status code AND body string). Share-alike on derived lists |
| [Blackbird](https://github.com/p1ngul1n0/blackbird) | — | **No licence file** | Active | Not redistributable. Naminter reads the same data under MIT |
| snscrape | 0.7.0, 2023-06-22 | — | **Dead** | All four backends broken by the 2023 API lockdowns |
| twscrape | 0.20.1, 2026-08-25 | MIT | Active | Operates from a pool of logged-in X accounts. Terms violation. Name and reject |
| Reddit for Researchers | — | Free, approved | Active | The only authorised research channel. Requires IRB. Hashed user IDs |
| Meta Content Library | — | Free via Secure Research Environment | Active | Prohibits longitudinal individual tracking and surveillance use |
| X API | — | $200/mo Basic | Active | No academic tier since 2023 |
| [atproto](https://github.com/MarshalX/atproto) | 0.0.72, 2026-09-10 | MIT | Active | Bluesky. Unauthenticated public reads. Low base rate for this cohort |
| SpiderFoot / Recon-ng | 2023 deps / no releases | MIT / GPL-3.0 | **Stale** | Cannibalise SpiderFoot's event-graph provenance model, build on neither |

## Political and electoral data

| Source | Coverage | Licence | Status | Note |
| --- | --- | --- | --- | --- |
| [DIME v4.0](https://data.stanford.edu/dime) | 1979–2024, **federal + state + local** | ODC-BY 1.0 | Active | **The single best fit.** Candidate file with party, office and persistent IDs. SQLite build available. Download first |
| [FEC bulk data](https://www.fec.gov/data/browse-data/?tab=bulk-data) | 1979–2026 | US Gov PD | Active | Candidate Master files. Use bulk, not the 1,000/hr API |
| [FollowTheMoney](https://www.followthemoney.org) | State candidates through 2024 | — | **Unmaintained, live** | Party and win/loss verified by staff. **Mirror it now** |
| [Harvard local elections DB](https://doi.org/10.17605/OSF.IO/MV5E6) | 1989–2021, 7 local offices | CC-BY 4.0 | — | \~78k candidates. Party is **imputed** for nonpartisan races — flag, never assert |
| [congress-legislators](https://github.com/unitedstates/congress-legislators) | 1789–present | CC0 | Active | The ID crosswalk hub: bioguide, FEC, OpenSecrets, ICPSR |
| [OpenStates people](https://github.com/openstates/people) | Current + retired | CC0 | Active | Use the repo, not the CSVs — the CSVs are current-only |
| [OpenElections](https://github.com/openelections) | Mostly 2000 onward | Check per repo | Active | Best free down-ballot results in the right year range. Per-state normalisation required |
| Ballotpedia | State + local, current officeholders | Commercial | Active | Only national local roster with party. Sales-gated — ask about academic access |
| OpenSecrets API | — | — | **Retired 2025-04-15** | Bulk data still free for non-commercial use |
| ProPublica Congress API | — | — | **Retired** | Entire ProPublica Data Store no longer updated |
| Vote Smart API | — | — | **Docs stale since 2021** | No visible key process. Do not build on it |

## Plumbing

`requests` + [requests-cache](https://pypi.org/project/requests-cache/) 1.3.3 (SQLite-backed, and the cache DB is itself a provenance artifact) · [stamina](https://pypi.org/project/stamina/) 26.1.0 over tenacity · [pyrate-limiter](https://pypi.org/project/pyrate-limiter/) 4.5.0 with the SQLite backend so limits survive restarts · `sqlite-utils` 4.2.1 for admin · DuckDB attached read-only for analysis · [filelock](https://pypi.org/project/filelock/) 4.2.12 (MIT) for pyrate-limiter's cross-process lock · test-only: [responses](https://pypi.org/project/responses/) 0.26.3 (Apache-2.0) and [vcrpy](https://pypi.org/project/vcrpy/) 9.4.2 (MIT), verified from installed metadata. Set a descriptive User-Agent naming the project with a contact address.

## Methodology citations worth having

- **SEARCH, *Interstate Identification Index Name Check Efficacy*** (1999, NCJ-179358) — 4.9% false positive rate on name-only checks against 93,274 Florida applicants. The empirical baseline for why identity confirmation needs more than name plus state plus year.
- **GUILD** (Gilbert et al. 2018, *J Public Health* 40(1):191–198) — the reporting standard for data linkage: algorithm, match counts, and **estimated linkage error rates**.
- **Enamorado, Fifield & Imai** (2019, *APSR*) — the political-science convention of reporting estimated false discovery and false negative rates at the chosen threshold.
- **Dunivin** (2025, *EPJ Data Science*) — closest methodological template for LLM-assisted qualitative coding: human codebook first, gold set double-coded, report Cohen's κ and F1 per field.
- **Chew et al.** (2023, arXiv:2306.14924) — LLM-assisted content analysis; use the model to identify which codes it handles badly and reserve those for humans.
- **Tibble et al.** (2018, *BMC Med Res Methodol* 18:76) — including aliases raised ascertainment 16% at no measurable false-positive cost. Relevant to the `aka` names in this file.
- **Brennan Center, *Heritage Fraud Database: An Assessment*** — the standing methodological critique of the source data. Read before building.

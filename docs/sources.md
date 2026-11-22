# Sources the court stage reads (build 07)

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

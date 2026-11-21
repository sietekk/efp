# Build 07: Federal court record — DOJ, CourtListener, RECAP

Spec: section 4 (court component and its four sub-strategies), section 5 (DOJ press releases API v1 and RSS, CourtListener REST v4 and bulk, RECAP Fetch, PACER), section 1 (F3). Task list: D4. Needs: 05, 06. Branch: `build/07-court-federal`. Report: `reports/07-court-federal.md`.

Read first: `CLAUDE.md`, `build/README.md`, `docs/spec.md` section 4 "court" contract and section 5 rows for DOJ, CourtListener, RECAP and PACER, the `court_records` and `identifiers` tables from `reports/02-schema.md`, `docs/plan.md` allowlist rows for DOJ and CourtListener and "Excluded from the first pass" (PACER and the EDU tier are gated), `reports/06-resolve-links.md`.

Stay in plan mode until I approve. Propose the sub-strategy interface, the DOJ matching logic, the bulk-load plan (what is downloaded, where, how big), and the identifier extraction rules. Wait.

## What to build

1. **The court package and its modules.** `efp/court/` with one module per sub-strategy on the build 04 `Module` interface, each returning `Located` records (`source_kind`, `jurisdiction`, `court`, `case_number | None`, `document_kind`, `url`, `capture`). Chain under `[stages.court]`, policy `first_hit` on a document that yields a case number, default order: `court.doj`, `court.pin_reports`, `court.state_ag` (build 08), `court.courtlistener_bulk`, `court.courtlistener_api`, `court.recap` (gated: unavailable without PACER credentials), `court.state_docket` (build 09). Every attempt, hit or miss, is recorded per module in `summary_json.modules` so coverage is reported per strategy; `court_records.strategy` names the module that found each document. Modules that later builds implement are registered now with `available()` returning `unavailable("not built until build 08/09")`, so the chain runs end to end today and simply gains modules later; a row that no module resolves completes as `not_found`, never `failed`.
2. **Federal or state.** A row is treated as federal when the Source domain (from `links`) is `justice.gov`, a `*.uscourts.gov`, or `courtlistener.com`, or when `details`/`source_raw` name a U.S. Attorney, "federal court", "U.S. District Court" or a federal statute; otherwise state. Record the classification and its basis in `summary_json`; the identity build needs it.
3. **DOJ press releases API v1** (`justice.gov/api/v1/press_releases.json`, no key, stay under 4 requests per second so budget at 3, `pagesize` max 50). The server-side `topic` parameter is silently ignored: filter client-side. Strategy: query by the subject's surname and given name (and `aka` aliases) with the row's year ±1, then filter titles and bodies locally for election terms (voter, ballot, election, registration, absentee) and the name; capture each candidate release through `ctx.capture`; a release that names the subject and an election charge is a `doj_release` located record, with the case number if the body gives one. Per-office RSS (`field_component`) is a fallback for coverage checks, not the primary path. Coverage is substantive from about 2013; for pre-2013 federal rows, capture the Public Integrity Section annual *Federal Prosecution of Election Offenses* reports once (through the capture layer, listed in `docs/sources.md` with their URLs), index them by name, and match (`source_kind = 'pin_report'`).
4. **CourtListener, bulk before API.** Download the quarterly bulk CSVs for courts, dockets and parties (not opinions) into `paths.bulk` (`~/efp-data/bulk/`, excluded from backup; tens of gigabytes, so stream, verify sizes, and load only the columns needed into a local DuckDB or SQLite index). Search the index by party name, state and year window first. Only then call REST v4 (`COURTLISTENER_TOKEN`; the default quota is 125 requests per day, budget at 100) for dockets the index lacks, and the RECAP archive search for documents already in the archive. A docket hit is a `federal_docket` located record with court and case number; filings already in RECAP are captured as PDFs for build 10.
5. **RECAP Fetch and PACER are gated.** RECAP Fetch needs the operator's PACER credentials and PACER charges per page; the academic fee exemption forbids internet redistribution and RECAP Fetch uploads to a public archive, which the spec says are not combined without written clarification. Wire the client behind `PACER_USER`/`PACER_PASS`, default off, and when a federal docket is found without its documents, write the docket id to `queues/pacer.csv` (row_id, court, case number, docket id, what is missing). Never fetch from PACER in the first pass.
6. **Identifier extraction from every located record.** From press releases and dockets, pull `age` or `dob`, `city`, `case_number`, `office`, `employer`, `party_on_ballot` and `alias` with the verbatim `span`, writing `identifiers` rows pointing at the capture. Rules-based (regular expressions over the captured text) in this build; the model-based `identifier` extraction class (build 16) adds to it later and never replaces it. Ages like "47, of Springfield" are the common form.
7. **Status per row.** `summary_json.court_status` ∈ {`primary_record`, `press_release_only`, `gated_portal`, `not_found`} (the plan's four values; `gated_portal` is set by build 09), plus per-strategy attempt results. The stage is `done` when every applicable strategy has been attempted, including when nothing was found; `failed` only on internal error.

## Tests

- Cassettes for DOJ search and pagination, client-side filtering, a release with and without a case number.
- A synthetic bulk sample (a few hundred invented rows in the bulk CSV layout) for the index loader and the name/state/year search; REST called only when the index misses (assert call counts).
- Identifier extraction on synthetic release texts covering age, "of <city>", case number formats (`1:19-cr-00123`, `No. 19-CR-123`), aliases.
- The sub-strategy order and stop-at-first-case-number rule with fake strategies.
- A `live` smoke test for one DOJ query and one CourtListener search, run by `efp selftest --live` only.

## Done when

- The bulk index is loaded and `efp selftest` reports its row counts and the quarter it came from.
- `uv run efp run court --sample` shows per-strategy results on the 50 rows; the full run (detached) gives every in-scope row a logged attempt for every applicable federal strategy, and `efp status` reports the court-status distribution, federal and state separately.
- `queues/pacer.csv` lists every federal docket whose documents are not in RECAP.

## Guardrails that bite here

Bulk before API; budgets per domain; no PACER purchases; nothing applied for (EDU tier). Every page through the capture layer. A gated source goes in the report with the rows it affects.

Finish with `reports/07-court-federal.md` and a pull request. Do not merge.

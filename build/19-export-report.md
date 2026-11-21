# Build 19: Export, status and audit

Spec: section 4 (export component and contract: nine columns verified, new columns from J onward, Treistman's dropdown preserved, redacted CSV), section 1 (F10, F11, N7), section 7 ("Derivation chain" and the audit script), section 10 (`status`, `audit`, `export`). Task list: D11, D12, F1. Needs: 12, 13, 18. Branch: `build/19-export-report`. Report: `reports/19-export-report.md`.

Read first: `CLAUDE.md`, `build/README.md`, `docs/spec.md` section 4 "export" contract, section 7 "Derivation chain", section 10 CLI; `docs/plan.md` "What the first pass hands back" (the nine numbers and the file list) and "Jeff's open decisions and the first-pass defaults" (decisions 2 and 3 are export-time rules); `docs/summary.md` "What you would receive"; `docs/row_hash.md`; `reports/18-code.md`.

Stay in plan mode until I approve. Propose the workbook-writing approach (how you preserve formatting and the data validation), the redaction rules, the status report layout, and the audit output. Wait.

## What to build

1. **Workbook export.** `efp export [--redacted] [--population all|convictions] [--out PATH]`: open the original workbook with openpyxl (never the read-only mode here; formatting must survive), verify every row's nine source cells still hash to `rows.row_sha256` per `docs/row_hash.md` and abort on any mismatch, then write the thirteen new columns from column J onward with the `export.provisional_prefix` on their headers, preserving the original dropdown data validation on Dr. Treistman's Ideological Motivation column and adding none elsewhere. Row order and `row_id` alignment are by position; nothing is sorted, merged or deleted. The population filter (decision 2) and the Partisan Direction → dropdown mapping, including the meaning of "independent" (decision 3), are export-time rules in `config.toml`, so changing them re-exports without re-running anything.
2. **F1 on the real export.** After writing, re-open the output and assert the nine source columns are byte-identical to the source for every row (the round-trip test from build 03, now on the full file). The export command fails and deletes its output if F1 does not hold.
3. **Redacted CSV.** `--redacted` writes the analysis file that may leave the machine: it drops Social URLs, Capture IDs and free-text notes for any row whose Subject Type is private individual, and never includes dates of birth, addresses, docket numbers joined to names, or identifiers. A test enumerates every column of the redacted file against an allowlist.
4. **`efp status`, completed into the first-pass report.** `reports/first-pass/status.md` regenerated on demand: the stage × status matrix; gate reasons; and the nine numbers with their denominators — link survival; primary-record rate (federal and state separately); identifier yield; identity outcome with the F3 false-confirmation rate and interval by stratum; news reach by era; extraction grounding (span-cited share, abstention rate, three-run disagreement on the sample); provisional motive and evidence-tier distributions; gated-source demand (rows blocked only by one gated source, per source); cost and time (API spend from `llm_spend`, wall-clock per stage from `case_stage`, requests per domain from the capture layer). Also the share of evidence by route (automated versus manual import).
5. **`efp audit <row_id>`.** Walks `codes.extraction_id → extractions.capture_id → captures.body_path` and `codes.decision_id → decisions`, prints each code with its rule, the verbatim span highlighted inside the captured text (with page and bbox for documents), the identity decision with its prior and waterfall, and the capture's URL, retrieval time and hash. `--html` writes the same as a single self-contained page for a reviewer. This is the demonstration a committee member asks for.
6. **Queues and matrix, final form.** `docs/coverage_matrix.md` regenerated; `queues/newsbank.csv`, `queues/pacer.csv`, `queues/portal_accounts.csv` regenerated with current counts and the `queue_status` done marks.
7. **Cited-URL archival as a chain.** `archive.perma` if a key exists, else `archive.spn`; whichever ran is recorded per URL, and the list of URLs with no second copy is written either way. `efp export --archive-cited` submits the URLs behind every exported code through that chain and records the result in `captures`; URLs still lacking a second copy go to `reports/first-pass/cite_urls.csv`.

## Tests

- Export on the synthetic workbook: F1 holds; headers and data validation preserved; columns start at J; prefix applied; population filter and mapping rules change output without touching the database.
- A tampered source cell is detected and the export aborts.
- Redaction allowlist test; private-individual rows lose the three fields; public-figure rows keep them.
- `status` numbers computed on a synthetic database with known answers.
- `audit` output contains the span, page and bbox for a synthetic document-backed code.

## Done when

- `uv run efp export` writes the provisional workbook to `~/efp-data/exports/` and F1 passes on the real file; `--redacted` writes the CSV.
- `uv run efp status` renders `reports/first-pass/status.md` with all nine numbers; `uv run efp audit <row_id>` works on a sample row end to end.

## Guardrails that bite here

The nine source columns are never written; only the redacted CSV may leave the machine; identity resolutions stay in `~/efp-data/`. Perma submissions are of public URLs already captured.

Finish with `reports/19-export-report.md` and a pull request. Do not merge. Review checkpoint: F1 on the real export, before anything is called a deliverable.

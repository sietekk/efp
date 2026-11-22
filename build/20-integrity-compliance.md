# Build 20: Integrity packaging, compliance and the methods appendix

Spec: section 7 (fixity packaging with BagIt, research packaging with RO-Crate, Auto Archiver one-call chain of custody, two copies minimum), section 11 (reproducibility test: fresh clone reproduces codes), section 12 (license register, compliance points), section 10 (backup and the bag manifest). Task list: D12. Needs: 05, 19. Branch: `build/20-integrity-compliance`. Report: `reports/20-integrity-compliance.md`.

Read first: `CLAUDE.md`, `build/README.md`, `docs/spec.md` sections 7, 11 and 12 in full, `docs/sources.md` and the data rows of `docs/license_register.md` from build 11, `reports/05-capture.md`, `reports/17-validation.md`, `reports/19-export-report.md`.

Stay in plan mode until I approve. Propose the RO-Crate structure, the reproducibility procedure, the license register's generation, the compliance checks as tests, and the methods appendix outline. Wait.

## What to build

1. **Research packaging.** `efp package`: validates the BagIt bag over `paths.store` (build 05), exports the database to a read-only copy, and writes an RO-Crate 1.2 (`rocrate` 0.15) describing the bag, the database export, `codebook.yaml` with its version and hash, the gold set with its hash, the prompt files with their hashes, the Splink settings with its hash, the pinned instrument data, and the redacted CSV, with licenses and attributions from the register. Row-level identity data stays inside the bag on the encrypted volume; the crate's public subset is explicit and tested.
2. **Two copies for anything cited.** For every URL behind an exported code: the local WARC/WACZ plus at least one third-party archive (Wayback via SPN, or Perma). `efp package --check-copies` lists cited captures lacking a second copy and retries SPN for them; Auto Archiver (Bellingcat, Docker, `csv_feeder`) runs over the confirmed-relevant URL list as the one-call chain of custody (hash, RFC 3161, OpenTimestamps, WACZ, Wayback, certificate) when Docker is available; otherwise deferred and reported.
3. **Reproducibility test.** `scripts/reproduce.sh` (bash, `set -euo pipefail`): fresh clone, `uv sync`, pinned reference files by hash, the captured store (read-only) and the database's captures and extractions tables as input, re-run `code` and `export`, and diff the codes against the delivered export. Run it quarterly and once before delivery; record the result in the methods appendix. In CI, the 20-row synthetic end-to-end run plays the same role.
4. **License register.** `docs/license_register.md` generated from a `licenses.toml` that lists every dependency group's licenses as spec section 12 gives them (MIT/BSD/Apache for Splink, rapidfuzz, jellyfish, Naminter, Maigret, Instructor, pdfplumber, warcio, auto-archiver; Docling MIT with models separate; nameparser LGPL noted; Browsertrix and PyMuPDF AGPL — Browsertrix as a separate container, PyMuPDF avoided; pywb and krippendorff GPL — separate service and numbers only; WhatsMyName CC BY-SA; Surya/Marker RAIL-M avoided; DIME ODC-BY; Harvard CC-BY with imputed party flagged; OpenSecrets non-commercial if used; CourtListener bulk, FEC, Census, SSA, DOJ public domain) plus the data rows from build 11. A test checks every package in `uv.lock` against the register and fails on an unknown or AGPL/GPL-linked entry.
5. **Compliance checks as tests** (`efp selftest --compliance`): the repository contains no `pipeline.db`, `store/`, `.env` or real workbook (scan the git index); no code path fetches NewsBank, Newspapers.com, ProQuest or Westlaw domains (allowlist of domains the capture layer may contact, with those four denied); PACER credentials and RECAP Fetch are never both enabled; the social stage runs only when `first_pass.social_enabled` is true; the redacted CSV allowlist from build 19; `User-Agent` names the project, institution and contact.
6. **Backup verification.** `efp manifest --verify-backup <path>` validates the bag copy on the external drive against the live bag so the nightly backup is confirmed by manifest, as spec section 10 describes.
7. **Methods appendix.** `docs/methods_appendix.md` generated from the ledger and the status numbers: the identity model (prior, comparisons, thresholds, parameter chart, F3 false-confirmation rate with interval, sensitivity at 0.995), extraction validation (per-field reports, grounding, α and κ with human–human ceiling, bootstrap intervals), inter-coder reliability, link survival and the coverage matrix summary, the share of evidence by route, and the reproducibility result. Numbers come from the database and the ledger; prose scaffolding is static and reviewed by me.

## Tests

- RO-Crate validates; the public subset contains no identity data (column and file allowlist).
- `check-copies` finds a synthetic cited capture without a second copy.
- License test against `uv.lock`; compliance tests each fail on a planted violation in a temporary tree.
- Appendix regeneration is deterministic from a fixed synthetic database.

## Done when

- `uv run efp package` produces a validated bag and crate on my machine; `efp selftest --compliance` passes; `docs/license_register.md` and `docs/methods_appendix.md` are generated and committed.
- `scripts/reproduce.sh` has been run once on the current state and its result is in the appendix.

## Guardrails that bite here

Published code, private data: the crate's public subset is the only thing that leaves. No new network sources. The license and compliance tests are gates in CI from this build on.

Finish with `reports/20-integrity-compliance.md` and a pull request. Do not merge.

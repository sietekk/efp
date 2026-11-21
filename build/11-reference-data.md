# Build 11: Reference datasets and loaders

Spec: section 5 ("Bulk datasets loaded once"; OpenFEC, Congress.gov, OpenStates, FollowTheMoney API rows; the bulk-before-API rule), section 8 (population prior inputs: Census surnames, SSA given names, state population by age band), section 12 (license register entries for DIME, Harvard local elections, WhatsMyName). Task list: D5 (inputs). Needs: 02, 05. Branch: `build/11-reference-data`. Report: `reports/11-reference-data.md`.

Read first: `CLAUDE.md`, `build/README.md`, `docs/spec.md` section 5 bulk table and the three identity-related API rows, section 8 "Population prior", section 12 license register, `docs/plan.md` allowlist rows for DIME, Harvard, FollowTheMoney, Census, SSA, OpenFEC, Congress.gov, OpenStates (and the note: mirror FollowTheMoney early; it is unmaintained), `reports/05-capture.md`.

Stay in plan mode until I approve. Propose each loader's source URL, the file sizes you expect, the `ref_*` schema, the attribution text, and what is downloaded now versus deferred. Wait.

## What to build

1. **Loaders under `efp/reference/`, one module per dataset on the `Module` interface (`reference.census_surnames`, `reference.ssa_names`, ...), each with `download(ctx)`, `load(ctx)`, `verify(ctx)`; a dataset whose source is offline or whose license is undecided is skipped with the reason and the others load, and the identity build's candidate index works over whichever `ref_*` tables exist,** writing to the `ref_*` tables reserved in build 02 and recording the source URL, retrieval time, file SHA-256 and license in a `ref_sources` table. Downloads go through the capture layer's `store_bytes` so the file hash and time are a capture (`stage = 'reference'`, `row_id NULL`); the raw files live under `paths.bulk` and are never committed.

   | Dataset | Into | License (attribute as the register says) |
   | --- | --- | --- |
   | Census 2010 surnames (162,253 surnames occurring 100+ times) | `ref_surname_freq` with proportion per 100k and the floor value for absent surnames recorded in `ref_sources` | Public domain |
   | SSA names by year (national) and by state | `ref_given_freq(name, sex, year, state NULL for national, count)` | Public domain |
   | Census state population by age band (for `N_adults(state, cohort)`) | `ref_population(state, age_band, year, count)` | Public domain |
   | DIME v4.0 recipients file (Stanford, SQLite build) | `ref_dime` (candidate name, state, district, party, cycle, CFscore, bonica_rid) | ODC-BY 1.0 |
   | FEC Candidate Master `cn*.zip` 1999–2026 | `ref_fec_candidates` | Public domain |
   | FollowTheMoney state candidates export | `ref_ftm` — **mirror in this build; the site is unmaintained** | Attribution |
   | Harvard local elections database 1989–2021 (OSF) | `ref_local` with `party_imputed` flagged where nonpartisan | CC-BY 4.0 |
   | congress-legislators YAML (GitHub) | `ref_congress` (ID crosswalk: bioguide, FEC ids, names, terms) | CC0 |
   | OpenStates people repo including `retired/` | `ref_state_leg` | CC0 |
   | WhatsMyName `wmn-data.json` | pinned file under `paths.bulk/wmn/`, hash recorded via `instruments` (used only by build 15) | CC BY-SA 4.0 (share-alike; attribute Micah Hoffman) |

2. **A candidate index for identity.** A view or table `ref_candidates_all` unifying DIME, FEC, FTM, Harvard local, congress and state legislators into one shape (`source`, `source_id`, `surname`, `given`, `middle`, `state`, `office`, `party`, `years`), with names parsed by `nameparser` and nicknames left to build 12's table. This is the universe build 12 matches against, locally, so the API quotas never bind.
3. **API clients, enrichment only:** OpenFEC (`FEC_API_KEY`, 1,000 per hour, budget 800), Congress.gov v3 (`CONGRESS_API_KEY`, 5,000 per hour), OpenStates v3 (`OPENSTATES_KEY`, undocumented and restrictive; budget 1 per 2 seconds). Each exposes `enrich(candidate) -> dict` and is called only for already-confirmed matches (build 12 and build 19's public-figure columns), never for discovery. The FollowTheMoney API is not used after the mirror.
4. **Ballotpedia** public pages are an allowlisted read-only source (robots.txt, low rate) for identity confirmation of public figures; add a thin `ballotpedia.lookup(name, state)` that fetches the candidate page through the capture layer and extracts office, party and years. The Ballotpedia API stays excluded (gated, pending the academic-access question).
5. **Verification.** `efp selftest --reference` reports every table's row count, the source hash and date, and recomputes a few known values (a common surname's proportion, a given name's count in a year) against figures written into the test.
6. **Docs.** `docs/sources.md` lists each dataset with URL, version or vintage, hash, license and the attribution sentence to use in the paper; `docs/license_register.md` (build 20 completes it) gets the data rows now.

## Tests

- Each loader against a tiny synthetic file in the dataset's real format (headers, encodings, the SSA zip layout); the unifying index shape; the surname floor rule.
- API clients with cassettes and asserted budgets; no discovery calls possible (the function signature takes a candidate, not a name).
- `ref_sources` provenance rows written for every load.

## Done when

- All reference tables are loaded on my machine with counts in the report, FollowTheMoney is mirrored, and `efp selftest --reference` passes.
- `ref_candidates_all` exists with per-source counts, and the report states coverage by state and year for the candidate universe (so the identity build knows where it is thin).
- `docs/sources.md` is complete with hashes.

## Guardrails that bite here

Bulk before API; no voter files (decision 6 is unanswered; `first_pass.voter_file_enrichment = false`); no people-search services; reference files never committed. Party fields in these tables are identity signals for public figures only; they are never used to infer motive (build 18 enforces this; note it here).

Finish with `reports/11-reference-data.md` and a pull request. Do not merge.

# Build 12 — Identity resolution

Branch: `build/12-identity` (off `main`; build 11 is merged, nothing stacked) · PR: see the pull request for this branch · Date: 2026-11-23 · Model: claude-sonnet-5-5 · Prompt: `build/12-identity.md` · Spec sections: 8 (in full), 4 (identity), 1 (F3, F4, N5), 3 (`identity_candidates`, `decisions`)

## What this build set out to do

For each row, decide whether the name in the Heritage row is one specific real person: gather candidate person records from the court records and the reference datasets, give each a population prior, add Fellegi-Sunter evidence from a Splink model, and route the posterior to `confirmed` (0.99 and above), `needs_review` (0.90 to 0.99) or `unresolved`, with "none of these" a first-class outcome. Parameters come from a module chain: published priors now (every decision tagged provisional), the operator's 150 labeled pairs later (`efp identity refit`, which rewrites only the rows whose outcome changes). The F3 audit and the review band are built as tools; the operator does the labeling and the verdicts.

The approved plan was followed, with the departures listed under "Decisions needed". The one that matters most for reading the results: **no row has a court record yet** (the first real run of the court stage found none; see "What was run"), so identity today works from the reference datasets alone and resolves nothing.

## What was built

- `efp/identity/`: `candidates.py` (seven candidate modules), `prior.py` (population prior and its stored components), `pairs.py` (pair construction, the independence rule, the prior's cohort), `model.py` (Splink 4.0.17 on DuckDB, fixed m and u), `params.py` (the two parameter modules, the labeled fit), `decide.py` (the decision rule), `store.py`, `rescore.py` (refit, rescore, the sensitivity batch), `labels.py`, `review.py`, `audit.py`, `explain.py`, `strata.py`, `charts.py`, `checks.py`, `normalize.py`, `profile.py`; `nicknames.csv` (versioned, 1,000+ pairs, hashed) and `settings/published_priors_v1.json` (each m and u with its basis).
- `efp/reference/places.py`: the Census place-by-county codes loader (the city comparison's "same county" level); loaded, 33,600 place and county rows.
- Migrations `0013_identity.sql` (decision columns `stage`, `batch`, `candidate_id`; candidate columns `settings_sha`, `scored_at`, `current`, `alias_index`; `identity_label_queue`, `identity_audit_sample`, `ref_place_county`) and `0014_identity_indexes.sql`.
- CLI: `efp identity explain | rescore | refit | labels build|status | audit build|rate`, `efp review identity [--label | --audit]`, `efp selftest --identity`, an identity table in `efp status`.
- `efp/runner.py`: `_identity_outcome` now reads the latest `main`-batch decision, including the operator's later `review` / `override` decision and an `audit_wrong` verdict (read as unresolved); labels, `audit_correct` and the `sensitivity` batch are never read.
- Config: eight keys under `[identity]` (`ssa_state_min_births`, `prior_pseudocount`, `common_surname_rank`, `reviewer_initials`, `label_seed`, `audit_seed`, `bootstrap_resamples`, `tf_adjustment_cap`) and `reference.census_place_county_url`, documented in `config.example.toml`.
- Docs: `docs/identity_model.md` (the model as built, every value with its basis, the approximations), the tool and license registers, `docs/sources.md`, `RUNBOOK.md` (the operator's identity steps in order).
- Tests: nine `tests/test_identity_*.py` files and `tests/identity_helpers.py`, synthetic names only. Gates: `ruff check`, `ruff format --check` and `mypy efp` are clean and the full suite passes (1,327 passed, 2 live tests deselected). Coverage of `efp/identity/`: 85 to 100 per cent per file.

## What was run

| Run | Rows attempted | Rows resolved | Wall clock | Requests | API spend |
| --- | --- | --- | --- | --- | --- |
| `efp run resolve_links --sample` | 50 | 50 done | about 5 min | through the capture layer | $0 |
| `efp run court --sample` | 50 | 50 done, **0 with a court record** (32 `deferred_coverage`, 18 `not_found`) | about 25 min (the DOJ press-release index built first) | through the capture layer | $0 |
| `efp run documents --sample` | 50 | 50 done (no documents: no records) | seconds | none | $0 |
| `efp run identity --sample` (published priors, ceiling prior) | 50 | 50 done: **48 unresolved `no_candidate`, 2 unresolved `below_review_threshold`, 0 review band, 0 confirmed** | seconds | none (identity makes no network call) | $0 |
| All rows (`resolve_links`, `court`, `documents`, `identity`, detached) | 1,481 in scope | **running when this report was written**; `efp status` shows the counts | hours | | $0 |

The 50 sample rows: candidates gathered 2 (one FEC, one legislator record), 0 surname-floor hits, 0 ambiguous given names, every candidate on the ceiling prior, both cohort-free. Module skips: `identity.cand.court` skipped on 50 (no court record), `.dime` and `.ftm` skipped on 50 (not loaded), `.voter_file` skipped on 50 (flag off).

**Why the court stage found nothing** (from `case_stage.summary_json` of the 50): the CourtListener bulk index and API are skipped (`efp court bulk fetch` has not been run), `court.pin_report_urls` is empty, RECAP is off, 32 state rows have no AG adapter or a manual one, no state docket portal has been probed (`efp court docket probe`), and the DOJ module missed the 25 rows it applied to. These are operator steps from builds 07 to 09, not an identity fault, but they decide whether identity can do anything: with no court record a row can only match a public figure.

## Results by source

| Source | Rows attempted | Hits | Misses | Errors | Notes |
| --- | --- | --- | --- | --- | --- |
| `identity.cand.court` | 50 | 0 | 0 | 0 | skipped: no court record for any row |
| `identity.cand.fec` | 50 | 1 | 49 | 0 | |
| `identity.cand.legislators` | 50 | 1 | 49 | 0 | `congress` + `state_leg` |
| `identity.cand.harvard_local` | 50 | 0 | 50 | 0 | |
| `identity.cand.dime`, `.ftm` | 50 | 0 | 0 | 0 | skipped: not loaded (gated, build 11) |
| `identity.cand.voter_file` | 50 | 0 | 0 | 0 | skipped: no voter-file source in this pass |

## Failures by reason

None in the identity stage (0 failed). One finding from the first real run of an earlier stage, not fixed here: `efp selftest --db` reports 10 `no orphan captures` failures, the state-AG harvester's listing pages (stage `court`, no row). Build 08's harvester and build 02's check disagree (decision 12).

## Gated sources encountered (not worked around)

| Source | What it wants | Rows affected | Where logged |
| --- | --- | --- | --- |
| Census 2010 surnames, SSA names (national and by state) | automated downloads refused (build 11); a browser download and `efp reference import` | every candidate's prior (the ceiling prior is used) | `ref_sources`, decisions' `decided_by` (`prior=ceiling`) |
| DIME, FollowTheMoney | a URL or a file (build 11) | candidate sources skipped | module skip reasons |
| Voter file | not in this pass | none | `identity.cand.voter_file` skipped |
| Census 2020 relationship files (land areas) | not found at a stable URL; the place-by-county codes file is used instead | none | `docs/sources.md` |

## Numbers for the first-pass report

- Identity outcome, sample of 50: 0 confirmed, 0 in the review band, 50 unresolved (48 for want of any candidate). The whole-workbook figure waits on the court stage; `efp status` gives it by common and rare surname.
- Unresolved is the honest headline today, and it is a coverage result, not a model result.
- No false-confirmation rate exists: there are no confirmed rows (`efp identity audit rate`: 0 judged, 0 pending).

## Review findings (done-when-reviewer) and what was done

The reviewer (cold, once) found three high-severity problems and several medium ones; all are fixed in this branch and each has a regression test in `tests/test_identity_review_fixes.py`.

| Finding | Fix |
| --- | --- |
| An initial, an empty or an unlisted given name set N_namesakes to 1 ("J. Commonsurname" with an age and a city was confirmed at 0.994) | an initial stands for every name starting with the letter; an empty name says nothing; an unlisted name is bounded by four births a year and sex over the cohort's births |
| A stage re-run (documents added to the input hash; every new label changed it) wrote a new model decision over the operator's | a re-run scores again but never writes over a review decision or an `audit_wrong` verdict; the hash covers the fitted settings, not the label count |
| The age was counted twice (the prior's cohort, then the dob/age evidence) | credited once: the positive dob/age weight is reduced by what the cohort gave the prior (`age_overlap_bits`, in the waterfall); a negative weight is left alone. Probe: exact date of birth plus city on a common name no longer confirms |
| A demotion (a refit, or `audit_wrong`) left news and social live | their rows are set to pending, and are skipped at the gate |
| `_human_after` ignored `audit_wrong` | added; a refit never flips back a row the audit called wrong |
| The audit pool omitted rows the operator confirmed from the band; the band flag counted sensitivity decisions | pool and band come from the gate's outcome, `main` batch only |
| The labeled fit was unweighted over a stratified sample | weighted by stratum population over stratum labeled count, scaled to mean one (the spread over the posterior range inside a stratum is not corrected: documented) |
| `dob.conflict`, `middle.conflict` were tagged `quoted`; the case-number u rested on an unsourced count | re-tagged `derived` / `design`; the notes say so |
| Suffixes (Sr., Jr.) were never compared | a differing suffix is at most reviewed (`suffix_conflict`) |
| (After the PR was raised, from a later review) `place_key` removed every trailing "city", so "Salt Lake City" became `SALTLAKE` and "Jersey City" collided with "Jersey Village", giving a false `same_county` | the loader strips exactly one legal suffix; the identity side never shortens a name as written and tries a suffix-free key only for suffixes other than "city", and only when the whole name finds nothing; tests for both copies and for the collision |
| Low: exact bound used the unweighted n; Wayback copies escaped the independence masking; a rescore sees only the stored top 25 | effective sample size; Wayback URLs unwrapped; the cap is documented |

## Spot-check guide for the operator

- `uv run efp identity explain <row_id>` for any row (the prior with each component, each comparison's level and Bayes factor, the posterior, the spans). Rows 2 of the 50 have a reference candidate; start there.
- `uv run efp selftest --identity` (the "name, state and year alone never reaches 0.90" check on this workbook's ten commonest surnames: highest posterior 0.00055).
- Once court records exist: `efp review identity` for the band, `efp identity labels build` then `efp review identity --label`, `efp identity audit build`, `efp review identity --audit`.

## Decisions needed from the operator

1. **The court stage must have records before identity can confirm anything.** Run `efp court bulk fetch`, `efp court docket probe`, supply `court.pin_report_urls`, then `efp run court` / `efp run identity`. Default: the stage runs on what exists and resolves nothing.
2. **Provisional parameters.** The literature that could be read (Winkler 2006, the Splink theory guide) gives illustrative rates, not an empirical m and u for this record type, so most published values are `derived` or `design`, each marked. Default: use them, provisional, until 150 labels.
3. **Name levels neutral, term-frequency adjustment only downward.** The exact-name levels have m = u (the prior carries the name); the adjustment is on (weight 0.5, minimum u 1e-4) but may only lower a weight (`identity.tf_adjustment_cap`). Default: as stated; a fit keeps exact-name levels neutral.
4. **N_namesakes is never below one** (the prior is at most one half). Default: as stated.
5. **Ceiling prior until the Census and SSA files are imported.** Default: ceiling, tagged `prior=ceiling`; then `efp identity rescore`.
6. **Age credited once** (departure from the spec's literal "prior from the cohort, plus the dob/age comparison"). Default: as in the table above.
7. **N_adults has both sexes** while p(given) is conditioned on one: overstates N by about two (cautious). Default: kept.
8. **A reviewed exception to the migration guard.** Migration 0013 adds three columns to `decisions` (`ADD COLUMN` only). `tests/test_db_guard.py` forbids ALTER of protected tables, so it has a narrow, counted allowance for exactly this. Alternative: a side table. Default: the exception.
9. **Reuse of pure text helpers across packages.** Identity imports `efp.court.names` and `efp.court.identifiers` (the same rules read the row and the court record) and `efp.reference.states`; no stage state is shared. Default: reuse.
10. **The F3 sheet.** The prompt puts it in `reports/identity/f3_audit.md`; because re-deriving a row needs names and spans (row-level identity data), the repository sheet holds ids only and the sheet with names is `~/efp-data/exports/identity/f3_audit_full.md`. It is empty now (no confirmed rows). Default: as stated.
11. **Independence and the review rules.** A fact two sides read from the same document counts once; a weak court record is at most reviewed; two candidates of one source at or above 0.90 go to review (a person with two cases is reviewed too: cautious); a differing suffix is at most reviewed; only a row's 25 best candidates are stored. Default: as stated.
12. **Orphan captures from the state-AG harvester** (stage `court`, no row) fail `efp selftest --db`. Default: the check is left as is; either the harvester's pages move to stage `reference` or the check allows them.
13. **The research plan says "0.99 against 0.95" for the sensitivity check, the spec 0.995.** Default: 0.995 (`efp identity rescore --confirm-threshold 0.995`, a separate batch).
14. **The operator's decision stands.** A stage re-run or a refit never overrides a `review` decision or an `audit_wrong` verdict on a row. Default: as stated.

## Operator decisions

Filled in by the operator at merge time.

1. Decision: ______ · Date: ______
2. Decision: ______ · Date: ______
3. Decision: ______ · Date: ______
4. Decision: ______ · Date: ______
5. Decision: ______ · Date: ______
6. Decision: ______ · Date: ______
7. Decision: ______ · Date: ______
8. Decision: ______ · Date: ______
9. Decision: ______ · Date: ______
10. Decision: ______ · Date: ______
11. Decision: ______ · Date: ______
12. Decision: ______ · Date: ______
13. Decision: ______ · Date: ______
14. Decision: ______ · Date: ______

## Carried forward

- 13: the news gate reads `runner._identity_outcome`: the latest `main`-batch decision, including the operator's `review` decision; `needs_review` blocks news until a decision is recorded; a demotion sets news and social to pending.
- 13, 15, 16: a confirmed row's evidence is `identity_candidates` (`current = 1`; `fields_json` holds the person, each identifier with its capture and span, the prior and the comparison inputs); `decisions.evidence_ids` lists the supporting captures; `decided_by` carries `splink:<version>+model:<sha>`, `params=provisional|fitted`, `prior=census|ceiling`, `nick:v<n>`, and `batch=sensitivity` where it applies.
- 18: party in a candidate's `extras` (`party_imputed` where it is an estimate) is stored and never scored; it is an identity signal for a public figure, never evidence of motive.
- 19: the output column `Identity Confidence` is derived from the gate outcome: `confirmed` is Confirmed, `unresolved` is Unresolved, and `needs_review` has no direct value (the export decides whether it is Probable; the research plan's "Probable" has no model counterpart).
- 17, 19: the sensitivity batch (`decisions.batch = 'sensitivity'`) is the 0.995 re-run; read it by `batch`, not through the gates.
- All: `efp identity rescore` after the Census and SSA files are imported, and `efp identity refit` once 150 pairs are labeled, re-score exactly the rows whose outcome changes and open the downstream rows they free; neither touches an operator-decided row.
- All: rows are not independent (339 share a prosecution), and 127 are exact duplicates: identity resolves each `row_id` separately; nothing is merged.

## Done-when check

- "The labeling queue is built; the stage has run end to end on published priors with every decision tagged provisional; `efp identity refit` is tested against synthetic labels in the suite (never against my data); the parameter chart is in `reports/identity/`." **Holds, with the caveat that the queue can only hold the pairs that exist**: `efp identity labels build` queued 2 of 150 (the sample's two reference candidates) and says so. The stage ran on the 50-row sample (and the all-rows run is in progress), every decision tagged `params=provisional` (`efp status`). Refit: `tests/test_identity_params.py` and `tests/test_identity_review_fixes.py::test_the_refit_command_fits_charts_and_rescores` use synthetic labels only. Chart: `reports/identity/parameter_estimates_published_priors.{html,json}`.
- "The full run assigns every row with a court record an outcome; `efp status` shows confirmed / review band / unresolved overall and by common-name and rare-name strata." **Holds trivially and cannot be shown yet**: no row has a court record, so no row is waiting for an outcome; every sample row has one (unresolved), and `efp status` carries the table (`tests/test_identity_cli.py::test_status_carries_the_identity_table`).
- "The F3 audit set exists with the commands I run to record verdicts; the report computes the false-confirmation rate from whatever verdicts exist at report time and says how many are still pending." **Partly**: `reports/identity/f3_audit.md` exists with the commands, empty because no row is confirmed; `efp identity audit rate` gives 0 judged and 0 pending. The rate cannot be computed until rows are confirmed and the operator records verdicts.
- Review checkpoint (thresholds and the F3 audit): thresholds are pre-registered at 0.99 and 0.90; the audit has nothing to audit yet.

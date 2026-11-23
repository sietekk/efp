# Maintenance 03 — Import the reference files and re-run identity

Branch: `maint/03-reference-import`, **stacked on `build/12-identity`** (PR #16 was open when this was started; the pull request is opened against that branch) · PR: see the pull request for this branch · Date: 2026-11-23 · Model: claude-sonnet-5-5 · Prompt: `build/m03-reference-import.md` · Needs: builds 11, 12

## What this build set out to do

Load the four files downloaded by hand (Census 2010 surnames, SSA national and by state, DIME v4 recipients), fix the loaders where the real files disagree with them, then re-run identity on the real population prior and the larger candidate universe, measuring the effect of the prior and of DIME separately. No new source, no network. FollowTheMoney stays out (verified gated).

## What was built

- **DIME loader (`efp/reference/candidates.py`), `loader_version` 2.** Candidates only (`recipient.type = cand`), the committee rows counted in `ref_sources.notes_json`; the party code mapped (100 `DEM`, 200 `REP`, 328 `IND`, any other code no party) with the raw code kept in `party_code`; suffix, gender, general and primary outcome, incumbent/challenger/open-seat status and FEC year; a candidate whose `lname` is empty (1,816 rows in the real file) is split from the full name instead of dropped. `nname` is read if a DIME build has one (v4.0 does not).
- **Migrations `0015_dime_fields.sql`** (new `ref_dime` columns), **`0016_given_name_index.sql`** and **`0017_given_name_index_order.sql`** (the index the prior's lookups needed, first `(name, state, year)`, then reordered to `(state, name, year)` on the review's finding that an initial's name range would walk every state).
- **Identity:** `identity.cand.dime` reads the candidate's DIME rows by `bonica_rid` at gather time (suffix feeds the existing suffix check, any nickname becomes an extra given-name form, party code, outcome, status and FEC year go into `extras`; gender is carried and never used). `efp/identity/prior.py`: the initial lookup is cached and uses the new index. `efp selftest --identity`: a second check, `identity.guarantee_rare`, for the ten rarest workbook surnames.
- **Known values** for SSA by state and DIME in `efp selftest --reference`.
- Docs: `docs/sources.md`, `docs/license_register.md`, `docs/identity_model.md`, `RUNBOOK.md`, the `dime_url` / `ftm_url` comments in `config.toml` and `config.example.toml`. `reports/11-*` and `reports/12-*` are untouched.
- Tests: `tests/test_reference_m03.py` (synthetic DIME file in the real column layout; party map; committees; no-surname rows; suffix, outcome, status; the index; `--part` and two files called `names.zip`; the identity hash and rescore across new loads; the rare-surname probe and its failure). `ruff check`, `ruff format --check`, `mypy efp` clean; full suite the full suite passes (2 live deselected).

## What was run

Preconditions: the all-rows `court` run (1,481 done) and `documents` (1,481 done) had finished; I stopped the wrapper script before it reached `identity` so no run scored on the ceiling prior. No load started while a stage was writing.

| Run | Rows attempted | Rows resolved | Wall clock | Requests | API spend |
| --- | --- | --- | --- | --- | --- |
| `reference import census_surnames` | 162,253 surnames | loaded | 4 s | none | $0 |
| `reference import ssa_names --part national` | 2,181,032 | loaded | 15 s | none | $0 |
| `reference import ssa_names --part state` | 6,696,687 | loaded | 44 s | none | $0 |
| `reference import dime` | 479,502 file rows | 359,653 candidate rows loaded (119,849 committee rows set aside) | 49 s | none | $0 |
| `reference index`, `selftest --reference` | | 408,460 candidates; 37 checks, 0 failed | 5 s | none | $0 |
| `identity rescore` (step 5a) | 2 rows with stored candidates | 0 changed | seconds | none | $0 |
| `identity --sample --recheck` (5b) | 50 | 50 done: 3 below the review threshold, 47 no candidate | 11 s | none | $0 |
| `identity --recheck`, all rows (5b) | 1,431 claimed (50 no-op) | 1,418 done, 13 `needs_review`, 0 failed | 3 min | none | $0 |
| `identity --recheck` again after the review fixes (tool version 3) | 1,481 | the same: 13 `needs_review`, 1,468 unresolved | 5 min | none | $0 |
| `identity rescore --confirm-threshold 0.995` (5d) | 170 rows with candidates | sensitivity batch: 13 review, 157 unresolved | seconds | none | $0 |

### The loaded files

| Dataset (part) | Load id | Rows | SHA-256 | Vintage |
| --- | ---: | ---: | --- | --- |
| `census_surnames` | 37 | 162,253 | `117c41cb4668727b7627b2845b6df3f83eb2a22a1813f42c0ff4bdcab86de135` | 2010 |
| `ssa_names` (national) | 38 | 2,181,032 | `cd78e975ed7bb358e018dd62fbe14ced89295e9581c49172ca4eedcb011b3724` | 1880-latest |
| `ssa_names` (state) | 39 | 6,696,687 | `e8aaf58e3838ec2a2aa880635014744ba20f74941a2688ac36723acaf604d66a` | 1880-latest |
| `dime` | 40 | 359,653 | `adc8e06da2b30a6b4e3dd6cd8b3f6e4d09ecf9b5b9294d67ad88f0aae770219e` | 4.0 |

`ref_sources` put each file in the dataset and part it was given (the two files called `names.zip` did not collide). The Census floor note: `floor_prop100k` 0.01695 (smallest listed count 100, `population_denominator` 294,977,843); SMITH 828.19 passes. The smallest SSA count is 5 in both files, so the prior's "unlisted name" bound of four a year holds.

### What the real files disagreed on (counts and column names only)

- DIME v4.0 has **no `nname` column**: there are no DIME nicknames, so no DIME alias is added (the code path exists and is tested on a synthetic build that has one).
- `fname` is the first name; `ffname` is first name plus middle (it extends `fname` in 172,879 candidate rows); `mname` is a single letter in 121,555 of 176,102 non-empty cases.
- `lname` is empty for 1,816 candidate rows that do have a `name` ("LAST, FIRST"): the old loader dropped them.
- 731 candidate rows carry a numeric `state` (not a postal code): loaded as no state, so those candidates are never gathered for a row (see decision 4).
- `party` is 100, 200, 328 for most rows with a tail (500, 400, 402, 825, `GRE`, `REF`, `SWP`, `CST`, `CRV`, `UNK`, blank); `seat` is `level:office` (`federal:house`, `state:lower`, `local:mayor`, ...); `gwinner`/`pwinner` are `W`/`L`/blank; `ico.status` is `I`/`C`/`O`/`U`/blank; `cand.gender` is `M`/`F`/`U`/blank; `suffix` is lower-case (`jr`, `iii`, `sr`, `iv`, and `jr jr` in 10 rows).
- Census: `(S)` occurs only in the percentage columns (132,002 lines); rank, count and proportion never. SSA state files are `state,sex,year,name,count`; national are `name,sex,count`. The loaders needed no change for these.

### Before and after

| `ref_candidates_all` source | Before | After |
| --- | ---: | ---: |
| congress | 12,770 | 12,770 |
| fec | 41,978 | 41,978 |
| harvard_local | 126,598 | 126,598 |
| state_leg | 21,889 | 21,889 |
| dime | 0 | 205,225 |
| **total** | **203,235** | **408,460** |

Thinnest jurisdictions (candidates), before: OL 2, DK 9, PI 13, MP 24, GU 33, AS 34, VI 41, PR 359, ND 599, DC 625, WY 648, SD 765. After: OL 2, DK 9, PI 13, MP 34, AS 52, GU 58, VI 62, PR 394, `??` 407 (the candidates with no state), DC 653, NE 1,628, ND 1,690. Among the 50 states the thinnest went from 599 (ND) to 1,628 (NE); DC is still the thinnest of those (625 to 653).

DIME, distinct candidates: by level, state 120,868, local 40,950, federal 20,547, other 6; by decade of first cycle, 1980s 3,531, 1990s 23,418, 2000s 54,776, 2010s 75,063, 2020s 16,727; by mapped party, DEM 64,017, REP 61,829, IND 55,441, none 340.

Identity outcomes (as the gates read them; confirmed / review band / unresolved):

| Surnames | Before (build 12 sample, 50 rows, ceiling prior) | After (all 1,481 rows, census prior) |
| --- | --- | --- |
| all | 0 / 0 / 50 | 0 / 13 / 1,468 |
| common | 0 / 0 / 21 | 0 / 3 / 687 |
| rare | 0 / 0 / 29 | 0 / 10 / 781 |

The two steps separated: **the prior alone changed nothing** (`identity rescore`: 2 rows scored, 0 written; only 2 rows had stored candidates, so the prior's effect on the sample is nil). **The effect is the wider run, with the court records and DIME**: the 87 court candidates (52 rows) and 97 DIME candidates (83 rows) are what produced the 13 review-band rows. Reasons: 1,311 `no_candidate`, 157 `below_review_threshold`, 6 `competing_candidate`, 7 `in_review_band`; the 13 rest on 12 court candidates and 1 FEC record.

Candidates, 170 rows with any: 1 candidate on 97 rows, 2 on 42, 3 on 24, 4 on 6, 5 on 1. By source (current): court 87 (52 rows), dime 97 (83 rows), harvard_local 53, fec 24, state_leg 17, congress 4. `identity.cand.dime`: 83 hits, 1,398 misses. The prior's mix over the 282 candidate rows: surname listed 253, floor 29, ceiling 0; given-name cohort: state file 237, national 21, national and absent in the cohort 3, state and absent in the cohort 7, unlisted 7, no given name 7; 15 ambiguous-sex names; 249 cohort-free. No `prior=ceiling` decision remains in the main batch (1,481 decisions, all `params=provisional;prior=census`). Sensitivity batch at 0.995: 13 review, 157 unresolved: identical to the main batch.

### The guarantee, at both ends

`efp selftest --identity`: highest posterior for a candidate agreeing only on name and state, **0.036 for the ten commonest workbook surnames and 0.5 for the ten rarest** (the namesake floor of one caps the prior at one half). Neither reaches 0.90, so **no extra rule was added**; thresholds are untouched at 0.99 and 0.90.

### Speed

Before the index, one candidate's prior took about 150 ms for a national name lookup and about 170 ms for an initial, and 0.8 s once per batch for the national totals. After migrations 0016 and 0017 a name lookup takes 0.2 to 0.7 ms and an initial about 12 ms (cached per letter and cohort). The all-rows identity run took 3 minutes, and 5 minutes on the second pass.

### Labels, audit, downstream

- `efp identity labels build`: the queue now holds **150 pairs** (148 added): common/dated/court 5, common/undated/court 18, common/undated/reference 55, rare/dated/court 19, rare/undated/court 14, rare/undated/reference 39. Nothing is labeled (yours).
- `efp identity audit build`: 0 confirmed rows, so no sample; `reports/identity/f3_audit.md` is unchanged (ids only, empty).
- News and social: `case_stage` reads `pending` for all 1,620 rows of both stages; none is skipped (a row is skipped at the gate when the stage runs). No row is `confirmed`; the 13 in the review band do not pass the news gate until you decide them (`efp review identity`).

## Results by source

| Source | Rows attempted | Hits | Misses | Errors | Notes |
| --- | --- | --- | --- | --- | --- |
| `identity.cand.court` | 1,481 | 52 | 0 | 0 | skipped on 1,429 (no court record) |
| `identity.cand.dime` | 1,481 | 83 | 1,398 | 0 | new |
| `identity.cand.fec` | 1,481 | 22 | 1,459 | 0 | |
| `identity.cand.harvard_local` | 1,481 | 49 | 1,432 | 0 | |
| `identity.cand.legislators` | 1,481 | 21 | 1,460 | 0 | |
| `identity.cand.ftm`, `.voter_file` | 1,481 | 0 | 0 | 0 | skipped: FTM gated, voter file off |

## Failures by reason

None. The `identity` stage failed 0 rows. One environment note: a gate run found Pillow (the `documents` extra) missing from the environment, so the document tests could not be collected; `uv sync --extra documents --offline` restored it and the suite passed.

## Gated sources encountered (not worked around)

| Source | What it wants | Rows affected | Where logged |
| --- | --- | --- | --- |
| FollowTheMoney | an account key (about 1,000 records a year per account); the site is unmaintained since the OpenSecrets merger; data to 2024 | every row's candidate set lacks FTM | module skip reason; `docs/sources.md` |

## Numbers for the first-pass report

- Identity outcome on the real prior, all 1,481 in-scope rows: 0 confirmed, 13 review band, 1,468 unresolved (1,311 for want of any candidate).
- Candidate sources, rows with a hit: court 52, DIME 83, Harvard 49, FEC 22, legislators 21.
- Prior: 253 of 282 candidate surnames listed in the Census file, 29 on the floor.
- The unresolved count is dominated by coverage: 1,429 rows have no court record (the court stage found DOJ releases for 52 rows).

## Review findings (done-when-reviewer) and what was done

The reviewer (cold, once) found no high-severity problem and confirmed the loader, the absence of real data in the repository, and `CandDime.enrich`. Everything it raised is fixed, each with a test:

| Finding | Fix |
| --- | --- |
| The new SSA by-state known value fails when only the national part is loaded; the DIME one fails on any small load | `Known.when`: a known value from the real file is reported "not checked" unless that file is loaded; tests for a national-only load and for both outcomes of the DIME check |
| The DIME known-value test never evaluated the known value | rewritten: skipped on the small fixture, fails when forced with a wrong value, passes with the right one |
| The rare-surname probe took the first ten count-1 surnames alphabetically and left out the year | ranked by the prior's own measure (floor first, then the smallest Census proportion), scored with the row's year; `ranked_surnames` is tested. The guarantee holds as before (0.5) |
| `_p_initial` was not cached and the index order did not serve it | cached; migration 0017 puts `state` first, so `state IS NULL` is an equality ahead of the name range (12 ms) |
| `INDEXED BY` crashes on a database without the index | removed; the planner uses the index without it |
| DIME missing-value markers (`NA`) were not normalised | normalised for suffix, status and nickname (the real file has none, so no reload was needed); tested |
| Identity `TOOL_VERSION` not bumped | bumped to 3; every row re-run, outcomes unchanged |
| Docs: 205,225 called candidates; claims before the report existed; no source pages; "DIME refuses downloads" | corrected; source pages added; the claims now rest on this report |

## Spot-check guide for the operator

- `efp review identity` walks the 13 review-band rows (12 rest on a court record, 1 on FEC); `efp identity explain <row_id>` shows the prior, comparisons and spans for any row.
- `efp identity labels status` (150 queued, 0 labeled), then `efp review identity --label`.
- `efp status` carries the identity table; `efp reference status` the loads.

## Decisions needed from the operator

1. **How was the Census `names.zip` obtained** (the Census site's own link, the Wayback copy of 2026-10-11, or the Census API assembled into `names.zip`)? `docs/sources.md` states only what the file shows (its members and dates) and that it was downloaded by hand. Default: as written; tell me and I add the sentence.
2. **DIME code 328 as `IND`.** The prompt maps 328 to Independent; it is the commonest DIME code (173,888 candidate rows, 55,441 distinct candidates), which looks more like "nonpartisan or independent" than a small party. Party is an identity signal only and is never scored, so nothing downstream changes. Default: 328 maps to `IND` as instructed.
3. **Gender is stored and not used** by the prior. Default: as instructed.
4. **DIME candidates with no usable state** (731 numeric `state` values, 407 candidates after collapsing) are never gathered for a row, because a candidate is never gathered on a name alone. Default: unchanged.
5. **The rare-surname rule was not added**: the measured maximum is 0.5, below 0.90. Default: none; the existing rule (a candidate needs an agreeing level beyond the name) still applies.
6. **The prior alone changed no decision** (only 2 rows had stored candidates), so the effect on the other 1,479 rows comes from the wider candidate run. Nothing to decide; stated so the two effects are not confused.
7. **The 13 review-band rows are yours.** Default: they stay in the band until you decide each; news and social do not pass them.
8. **Migrations 0016 and 0017 add one index on `ref_given_freq`** (8.9 million rows; a few seconds to build). Default: kept.
9. **Two known values read from the real files** (Mary, F, born 1950, Connecticut: 614; DIME candidate rows for a U.S. House seat in the 2012 cycle: 1,823). They carry a `when` condition (the by-state table has over a million rows; DIME has over 100,000) and are reported "not checked" on a smaller load, so a national-only or synthetic load does not fail the selftest.

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

## Carried forward

- 13, 15: the 13 review-band rows block the news gate until a `review` decision is recorded; 0 rows are `confirmed`, so no row is open to news or social yet; the stages read `pending` for every row.
- 13, 16, 19: every main-batch identity decision now carries `params=provisional;prior=census;nick:v1`; there is no `prior=ceiling` decision. A decision's `decided_by` says which load of the reference tables it rests on only through the settings tag: a new load changes every row's input hash, so `efp run identity --recheck` revisits them.
- 16, 18: a DIME candidate's `identity_candidates.fields_json.person.extras` carries `party_code` (raw), `won`, `primary_won`, `ico_status`, `fecyear`, `gender` and, when a build has one, `nicknames`; its mapped party (`DEM`/`REP`/`IND`) is `ref_candidates_all.party` and is never scored. Party is an identity signal, never evidence of motive.
- 19: DIME contributes no alias in v4.0 (no `nname`); the candidate's `person.suffix` is the latest non-empty DIME suffix.
- All: the 150 label pairs are queued (`efp identity labels status`); the audit has nothing to sample until a row is confirmed.

## Done-when check

- "`efp reference status` shows `census_surnames`, `ssa_names` (both parts) and `dime` loaded with row counts; `efp selftest --reference` passes with known values for all three; `ref_candidates_all` has a non-zero `dime` count and the report carries the before/after coverage tables." **Holds**: loads 37 to 40; 37 checks, 0 failed, with SMITH 828.19, Mary 1880 national, Mary 1950 Connecticut (614) and the DIME 2012 House count (1,823); `dime` 205,225; the tables are above.
- "`efp identity rescore` and `efp run identity --recheck` (sample, then all rows) have run; `efp status` shows no `prior=ceiling` decision in the main batch; `efp selftest --identity` passes with the extended guarantee and the report states both highest posteriors." **Holds**: 0 ceiling decisions; both guarantees pass, 0.036 and 0.5.
- "Tests, ruff, ruff format, mypy pass; the docs are updated; the report has every TEMPLATE section." **Holds**: 1,342 passed; ruff, format and mypy clean; the review section is filled in.

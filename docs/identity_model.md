# Identity model (build 12)

How a row's name becomes one specific, confirmed person, or does not. This is the specification in
`docs/spec.md` section 8 as built, with every number and every departure written down.

## What is decided, and what is not

For each row the stage decides whether the person named in the Heritage row is one specific real
person, so that anything found later attaches to the right human being. It is a one-to-many
**verification**: a few candidate records per row, and a live outcome "none of these". Three
outcomes, as the gates read them:

| outcome | meaning | what happens next |
| --- | --- | --- |
| `confirmed` | posterior at or above `identity.confirm_threshold` (0.99) | the news stage may run |
| `needs_review` | posterior from `identity.review_threshold` (0.90) to the confirm threshold | stops until the operator decides (`efp review identity`) |
| `unresolved` | below 0.90, no candidate, or no corroboration beyond the name | the chain stops (the news gate reads it) |

Nothing is ever confirmed or reviewed on name, state and year alone. Party, where a reference record
carries it, is stored on the candidate and is never scored (it is an identity signal for a public
figure, never evidence of motive).

## Candidates (module chain `[stages.identity]`, policy `all`)

| module | source | gathered by |
| --- | --- | --- |
| `identity.cand.court` | `court_records_current` and `court_identifiers_current` (builds 07 to 09) | one candidate per distinct case; the name as the court text writes it (with its middle name); the identifiers with their spans |
| `identity.cand.fec`, `.dime`, `.ftm`, `.harvard_local`, `.legislators` | `ref_candidates_all` (build 11) | surname key and state, for the row's name **and every `aka` alias**; kept when the given name is equal, a nickname, an initial or Jaro-Winkler 0.92 or better |
| `identity.cand.voter_file` | none | unavailable: `first_pass.voter_file_enrichment` is false and no voter-file source exists in this pass |

A module whose source is not loaded is skipped with the reason and the stage carries on. A reference
candidate is never gathered for a row with no state.

The row itself (the left side of every comparison) is the Name cell, one person per alias, plus the
identifiers the row's own `details` text and the documents its links resolved to say about the
subject, read with the same rules as the court record (`efp.court.identifiers`).

### Independence

A fact only counts as agreement when the two sides read it from **different documents**. A press
release the Heritage row links to and the same press release found by the court stage are one
observation; the age, city and case number in it are listed under `masked` and take no part in the
comparison. Documents are compared by content hash and by URL.

## Population prior

`N_namesakes = p(surname) x p(given | sex, birth cohort) x N_adults(state, cohort)`, prior odds
`1 / N_namesakes`. All three components, where each came from and the flags below are stored with the
candidate (`identity_candidates.fields_json.prior`) and printed by `efp identity explain`.

- **p(surname)**: the Census 2010 surname proportion; a surname absent from the file takes the floor
  recorded when the file was loaded (half the smallest listed proportion, `ref_sources.notes_json`).
  The number of floor hits is reported.
- **p(given | sex, cohort)**: SSA counts of the name in the cohort's birth years over all births of
  that sex in those years (names given fewer than five times a year are not in the files, so the
  totals are slightly low and the share slightly high). The sex is the name's SSA majority sex;
  a name with 10 to 90 per cent of its births in each sex is *ambiguous* and pooled over both sexes
  (counted in the report). The by-state file is used where the state had at least
  `identity.ssa_state_min_births` births of that sex in the cohort and lists the name.
- **cohort**: an exact date of birth gives its year; an age (document year minus age, two years) is
  widened by five years each side; with neither the prior is **cohort-free** over all adults, which
  is deliberately weak. The cohort comes from the candidate's age or date of birth, or the row's own.
- **N_adults**: the state's population in the age bands the cohort falls in (uniform within a band)
  at the latest vintage in `ref_population` (2023), adults (18 and over) when the prior is
  cohort-free. Applying a 2023 population to a cohort of an earlier case is an approximation.
- **N_namesakes is never below one.** The person is a namesake of themself, so a prior never
  exceeds one half. A name so rare that the arithmetic gives less than one is floored, and is
  flagged `floored_at_one`.

### The ceiling prior (until the Census and SSA files are imported)

The Census surname and SSA files could not be fetched automatically (build 11), so `ref_surname_freq`
and `ref_given_freq` are empty. Until they are imported the prior uses a **ceiling**, and says so on
every decision (`prior=ceiling`):

- p(surname) = 828.19 per 100,000, the most common surname, Smith (U.S. Census Bureau, Frequently
  Occurring Surnames from the 2010 Census: 2,442,977 occurrences; the per-100,000 figure from
  NameCensus and 247wallst, which agree to 828.2);
- p(given) = 62.9 per 1,000, the highest male given-name rate in 1900 to 1997 (John, 1900; SSA
  Office of the Chief Actuary, Actuarial Note 139);
- N_adults from the Census population table when loaded.

A ceiling can only overstate N_namesakes, so the prior can only be too cautious: with it no name is
treated as rarer than the most common one, and only a case number or a date of birth with other
evidence can carry a row. When the files are imported, `efp identity rescore` computes the true prior
and writes new decisions for exactly the rows whose outcome changes.

## Evidence model

Splink 4.0.x on DuckDB with m and u fixed (never EM: 1,481 rows are too few for stable unsupervised
estimation). Candidates are gathered before Splink sees them, so Splink is used for the comparison
levels, the Bayes factors and the per-comparison explanation; its own match probability is not used
because it would bring a prior of its own. Evidence is the sum of the log2 Bayes factors, and
`posterior = prior odds x 2^evidence`.

| comparison | levels (best first; `null` is absent on either side and has Bayes factor 1) |
| --- | --- |
| surname | exact; Jaro-Winkler >= 0.92; Double Metaphone equal; else. Term-frequency adjustment on (weight 0.5, `tf_minimum_u_value` 1e-4) |
| given_name | exact; nickname (the versioned table `efp/identity/nicknames.csv`); Jaro-Winkler >= 0.92; else |
| middle | exact full; initial match; conflict. **Absent is not evidence against** |
| dob / age | exact date of birth; birth years within 1; within 3; conflict (strongly negative) |
| city | exact; same county (`ref_place_county`, Census place-by-county codes); same state; conflict |
| case_number | exact on a normalised key (year, type, number; the division and the judge's initials are not part of it); else |

Cities are normalised with `usaddress` where the string looks like an address. A place is looked up by the name as written ("Salt Lake City" stays `SALTLAKECITY`, so "Jersey City" and "Jersey Village" never collide); only a legal suffix other than "city" ("Hartford town") is dropped, and only as a second try. The Census table strips exactly one legal suffix from its own names. Double Metaphone is
the `doublemetaphone` package (Artistic License), since `jellyfish` has none and the dead `abydos` is
excluded.

### Two decisions that keep the prior honest

1. **The exact-name levels are neutral** (m equals u, Bayes factor 1): the prior was computed for the
   row's name and every candidate was gathered by it, so name agreement is already in the prior and
   counting it again would defeat the guarantee below. A fit from labels keeps them neutral.
2. **The term-frequency adjustment can only lower a weight**, never raise it
   (`identity.tf_adjustment_cap`): the rarity of a name is already in the prior, and a rare name in
   a 1,481-row workbook must not be rewarded twice. Frequencies are those of the whole workbook,
   so a batch never changes a score. The raw adjustment stays in the waterfall.

### The guarantee

A candidate that agrees only on name, state and year has evidence zero (at most a hair below it,
from the term-frequency adjustment) and a posterior equal to the prior. For a common name that is
far below 0.90. For a name so rare that the prior is high, N_namesakes is floored at one, so the
prior is at most one half and is still below 0.90. And independently of arithmetic, the decision
rule refuses to confirm or review a candidate with no agreeing level beyond the name
(`no_corroborating_evidence`). `tests/test_identity_guarantee.py` proves all three for ten common
surnames under a census prior and the ceiling prior; `efp selftest --identity` runs it on this
workbook's own ten most common surnames and prints ranks and posteriors only.

## Published priors (provisional parameters)

`efp/identity/settings/published_priors_v1.json` holds m and u for every level. Every value carries a
**basis** and a note:

- `quoted`: a number published in a source that was read (see the sources below). Only the exact-agreement figures taken from the Splink guide and Winkler's example error rates are quoted, and both are illustrations, not estimates for this kind of record;
- `derived`: arithmetic on quoted numbers or on a stated assumption, with the derivation;
- `design`: a choice made for this study, with the reason. These are not literature values, and the
  report lists them as such.

The literature that could be read gives illustrative rates, not an empirical m and u for this kind of
record: Winkler's example error rates and the Splink guide's examples. Most of the m and u below are
therefore derived or design values. They are provisional on purpose: every decision made with them is
tagged `params=provisional`, and the 150 labeled pairs replace them.

Sources read for this table:

- **census2010**: U.S. Census Bureau, Frequently Occurring Surnames from the 2010 Census: Smith, 2,442,977 occurrences, 828.19 per 100,000 (the ceiling prior's p(surname))
- **splink_theory**: Splink documentation, Fellegi-Sunter topic guide (https://moj-analytical-services.github.io/splink/topic_guides/theory/fellegi_sunter.html), read 2026-11-23: for date of birth 'almost 100%, say 98%' exact agreement among true matches, 'maybe a 2% chance of a data error', and ~10,000 possible dates of birth over a flat 30-year range (u = 0.01%). Illustrative figures, not empirical estimates.
- **ssa_note139**: SSA Office of the Chief Actuary, Actuarial Note 139: the highest male given-name rate 1900-1997 is John in 1900, 62.9 per 1,000 male births (the ceiling prior's p(given))
- **winkler2006**: Winkler, W. E. (2006). Overview of Record Linkage and Current Research Directions. U.S. Census Bureau Statistical Research Division, Research Report Series (Statistics #2006-2), section 3 ('If first name has 10% typographical error rate and last name, day-of-birth, month-of-birth, and year-of-birth have 5% typographical error rates, then exact character-by-character matching ... could miss 25% of matches'). Read 2026-11-23 at https://www.census.gov/content/dam/Census/library/working-papers/2006/adrm/rrs2006-02.pdf

| comparison | level | m | u | Bayes factor | basis | derivation |
| --- | --- | ---: | ---: | ---: | --- | --- |
| case_number | else | 0.4 | 0.999999 | 0.4 | design | m: the same person's other case (design). Bayes factor 0.4. |
| case_number | exact | 0.6 | 1e-06 | 6e+05 | design | u: one in a million, an assumed order of magnitude (about 5,000 federal criminal cases a district-year, 94 districts, 20 years is an unsourced estimate), so the Bayes factor is large but not infinite. m: 60% of true-match pairs share the case (a person may have several cases and a document may cite another). |
| city | conflict | 0.05 | 0.05 | 1 | design | m: a person recorded in another state (design). u: candidates are gathered by state, so this is rare; neutral (Bayes factor 1) until labels say otherwise. |
| city | exact | 0.7 | 0.02 | 35 | design | m: a person recorded in the same city in two documents (design). u: a namesake in the same city, about 2% of those in the same state (sum of squared city shares, order of magnitude). |
| city | same_county | 0.1 | 0.03 | 3.33 | design | m: moved within the county (design). u: a namesake in another city of the same county. |
| city | same_state | 0.15 | 0.9 | 0.167 | design | m: moved within the state (design). u: the default for a namesake in the same state. |
| dob | conflict | 0.03 | 0.767 | 0.0391 | derived | m: the Splink guide's illustrative 2% data error for a date of birth plus a 1% remainder (design), 3%. u: the remainder after the other levels. |
| dob | exact | 0.45 | 9.1e-05 | 4.95e+03 | derived | u: 1 / (365.25 x 30), a flat 30-year birth range (Splink theory guide: about 10,000 dates). m: an assumed half of true-match pairs compare two dates of birth (design; the other half involve an age), times 0.857 = 0.95^3, the day, month and year each at the 5% typographical error rate of Winkler 2006 = 0.43, rounded up to 0.45 (the Splink guide's 98% exact agreement is the upper bound). |
| dob | within_1 | 0.45 | 0.0999 | 4.5 | derived | u: 3 of 30 years (a gap of at most 1) less the exact share. m: true-match pairs with an age, which agree to within a year when recorded without error (design: the other half of pairs). |
| dob | within_3 | 0.07 | 0.133 | 0.526 | derived | u: 7 of 30 years less the inner levels. m: the remaining true-match share (design). |
| given_name | else | 0.01 | 0.04 | 0.25 | design | Different given names (Bayes factor 0.25). |
| given_name | exact | 0.85 | 0.85 | 1 | design | Name agreement adds no evidence: the prior is computed for the row's name and every candidate is gathered by it, so m = u (Bayes factor 1). |
| given_name | jaro_winkler | 0.025 | 0.05 | 0.5 | design | Approximate name agreement is weaker than exact; a one-bit penalty (Bayes factor 0.5) until labels replace it. |
| given_name | nickname | 0.05 | 0.1 | 0.5 | design | Approximate name agreement is weaker than exact; a one-bit penalty (Bayes factor 0.5) until labels replace it. |
| middle | conflict | 0.1 | 0.93 | 0.108 | derived | m: Winkler 2006 gives, as an illustration, a 10% typographical error rate for a name field; used here as the rate of a recorded middle name that disagrees (an assumption that a middle name behaves like a first name). u: the remainder. |
| middle | exact | 0.7 | 0.01 | 70 | derived | m: 1 - the 10% first-name typographical error rate (Winkler 2006) less the share of true matches where one source gives only an initial (design, 20%). u: assumes about 100 effective distinct middle names (sum of squared shares), to be replaced by the SSA sum when the files are imported. |
| middle | initial | 0.2 | 0.06 | 3.33 | derived | m: true matches where one source records an initial only (design, 20%). u: about 17 effective initials (sum of squared shares ~ 0.06). |
| surname | double_metaphone | 0.015 | 0.03 | 0.5 | design | Approximate name agreement is weaker than exact; a one-bit penalty (Bayes factor 0.5) until labels replace it. |
| surname | else | 0.005 | 0.02 | 0.25 | design | No agreement on the surname at all: Bayes factor 0.25. Candidates are gathered by surname, so this level is rare (an alias pair). |
| surname | exact | 0.9 | 0.9 | 1 | design | Name agreement adds no evidence: the prior is computed for the row's name and every candidate is gathered by it, so m = u (Bayes factor 1). |
| surname | jaro_winkler | 0.025 | 0.05 | 0.5 | design | Approximate name agreement is weaker than exact; a one-bit penalty (Bayes factor 0.5) until labels replace it. |

## Parameters from labels (`identity.params.labeled_pairs`)

Once `identity.labeled_pairs_required` (150) pairs are labeled `match` or `non-match` (`unsure` does
not count) the parameter chain, policy `first_available`, uses `labeled_pairs` instead of
`published_priors`. The fit is a shrinkage estimate, not EM: for each comparison the m (u) of a level
is the labeled count among matches (non-matches) with that level, plus `identity.prior_pseudocount`
pairs' worth of the published value, over the total. The fitted settings are saved in
`~/efp-data/identity/settings/` (they come from the operator's labels) and their hash goes into
`decided_by`. The stability chart (Splink's `parameter_estimate_comparisons_chart`) is written to
`reports/identity/` for whichever module ran: with labels it shows the published value beside 200
bootstrap fits of the labels; with the published priors there is nothing to compare, and the report
says so.

The 150 pairs are drawn by `efp identity labels build` with a fixed seed, stratified by common and
rare surname, by whether either side has a date of birth or an age, and by source (court or
reference), and spread over the posterior range rather than concentrated near a threshold. They are
shown blind: the model's score is not displayed while labeling.

## Decision rule

See the table at the top. Additions: a candidate from a court record the court stage marked `weak` is
at most reviewed (`weak_record`); two candidates of one source with different identifiers both at or
above the review threshold go to review (`competing_candidate`). Thresholds are pre-registered in
`config.toml`. `efp identity rescore --confirm-threshold 0.995` writes the sensitivity check to a
separate batch (`decisions.batch = 'sensitivity'`) that no gate reads and that never overwrites the
main one.

## Audit (F3)

`efp identity audit build` draws 50 confirmed rows, equal allocation over (common, rare) x (never in
the review band, was in the review band), capped by each cell, with a fixed seed and the inclusion
weight stored. `efp review identity --audit` records `audit_correct` or `audit_wrong` with a
rationale; `audit_wrong` withdraws the confirmation (the gate reads it as unresolved).
`efp identity audit rate` reports the weighted false-confirmation rate by common and rare surnames
with a bootstrap interval and, when no verdict is wrong, the exact one-sided 95% upper bound
(a bootstrap of zero errors is degenerate). The sheet with names and spans stays in
`~/efp-data/exports/identity/`; the repository sheet holds ids only.

## Known approximations (also in the build report)

- The prior's cohort and the dob/age evidence use the same age (the spec prescribes both). The age
  is credited once: the positive dob/age weight is reduced by what the cohort already gave the
  prior (`age_overlap_bits` in the waterfall), never below zero; a negative weight is left alone.
- A given name that is an initial stands for every name that starts with the letter; an empty given
  name says nothing; a name not in the SSA files is bounded by four births a year and sex over the
  cohort, not by all years.
- The operator's decision stands: a re-run of the stage scores again but never writes over a review
  decision or an `audit_wrong` verdict, and a refit leaves such rows alone. A demotion (a refit that
  drops a confirmed row, or `audit_wrong`) sets the news and social stages of that row to pending.
- The labeled fit weights each label by its stratum's population over its stratum's labeled count;
  the spread over the posterior range inside a stratum is not corrected.
- A candidate whose suffix (Sr., Jr.) differs from the row's is at most reviewed (`suffix_conflict`).
- Only a row's 25 best candidates are stored; a rescore sees only those.
- N_adults has both sexes while p(given) is conditioned on one, which overstates N by about two:
  conservative.
- A 2023 population is applied to cohorts of earlier cases.
- The research plan's "0.99 against 0.95" sensitivity wording differs from the spec's 0.995; the spec
  is followed.

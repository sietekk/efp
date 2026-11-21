# Build 21: QA review and the first-pass freeze

Plan: `docs/plan.md` "Build sessions" S8, "What the first pass hands back", "Your review sample in S8". Spec: section 9 (reviewer decisions are logged), section 7 (fixity before delivery). Task list: E2 (replaced by a machine pilot plus this review). Needs: 19, 20. Branch: `build/21-qa-freeze`. Report: `reports/21-qa-freeze.md`.

Read first: `CLAUDE.md`, `build/README.md`, `docs/plan.md` the three sections above, `reports/first-pass/status.md`, and every `reports/NN-*.md` so far.

Stay in plan mode until I approve. Propose the stratified draw, the review interface, how verdicts are stored, the fix cycle's scope rule, and the freeze procedure. Wait.

**Operator refinement, not a stop:** build the review queue, the fix-cycle tooling and the freeze, and run the freeze with the review status recorded as it stands (`pending`, `partial: n/100`, or `complete`). My verdicts, whenever they arrive, re-run the fix cycle and regenerate the notes; `first-pass-v1` can be re-tagged as `first-pass-v1.1` after them. Do not review rows yourself and do not fill in verdicts.

## What to build

1. **The review sample.** 100 rows, 25 from each of: primary record found; press release only; identity review band; extraction with run disagreement on the sample or `low_confidence` at volume. Drawn with a seed, frozen by hash under `reports/first-pass/qa_sample.json` (row ids and hashes only).
2. **`efp review qa`.** For each row: the source cells, the court-record status and documents, identifiers with spans, the identity decision with prior and waterfall, linked articles, every extraction with its span in context, the provisional codes with the rule that fired, and the gate history. I record a verdict per row (`correct`, `wrong`, `unverifiable`) and a note, plus an optional per-code verdict; each is a `decisions` row of kind `review` with `decided_by` my initials. Resumable; progress shown.
3. **The fix cycle.** Built now, run whenever verdicts exist: group the `wrong` and `unverifiable` cases by cause (a stage, a rule, a source), propose fixes in plan mode, and after approval make one pass of fixes, re-running only the affected stages for the affected rows (`--reset` on those rows; captures are never re-fetched). Re-present the changed rows for a second verdict. One cycle; anything left goes into the notes as known issues.
4. **Freeze.** Regenerate the exports and `reports/first-pass/status.md`; lift the provisional prefix only if I say so (default: keep it; the first pass is machine output marked provisional); `efp manifest --timestamp`, `efp package` (bag and crate), and tag the repository `first-pass-v1` with the bag's top-level hash in the tag message.
5. **`docs/first-pass-notes.md`**, written for Dr. Treistman to read without the code: what resolved at each station and how often; what needs a gated source, with row counts per source and what each wants (the business case for every application or purchase in the second pass); what it cost (API spend, wall-clock per stage, my hours as I report them); the review verdict tally as the first reliability figures; the nine numbers in one table; the open decisions and what each would change; and what the second pass should do first. Plain language; the pipeline metaphor from the Summary; no identifiers of private individuals anywhere in it.
6. **Handover list.** The file list from the plan, checked: `reports/first-pass/status.md`, `docs/coverage_matrix.md`, `queues/newsbank.csv`, `queues/pacer.csv`, `queues/portal_accounts.csv`, the provisional workbook and the redacted CSV in `~/efp-data/exports/`, `docs/first-pass-notes.md`, `docs/methods_appendix.md`, the bag and crate.

## Tests

- Stratified draw and hash; the review command records decisions and resumes; the fix cycle's reset touches only affected rows and never deletes captures; the notes generator contains no name from `rows` (scan).

## Done when

- The review queue works end to end (tested with synthetic verdicts in the suite); the freeze has run with the review status recorded; the exports are regenerated and F1 still holds; the tag exists; `docs/first-pass-notes.md` is written with the verdict tally section filled from whatever verdicts exist and a clear "pending" where none do.

## Guardrails that bite here

Verdicts are mine alone. Nothing is deleted; the store is never re-fetched; the notes carry no row-level identity data. Lifting "provisional" is my decision, not the default.

Finish with `reports/21-qa-freeze.md` and a pull request. Do not merge.

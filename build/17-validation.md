# Build 17: Gold set and validation harness

Spec: section 9 ("Gold set", "Validation harness"), section 11 (model-level tests: gold-set harness re-run on any model or prompt change), section 1 (F11: coding reliability on demand). Task list: D8, E2. Needs: 03, 16. Branch: `build/17-validation`. Report: `reports/17-validation.md`.

Read first: `CLAUDE.md`, `build/README.md`, `docs/spec.md` section 9 "Gold set" and "Validation harness" (the five numbered items are the contract), the `gold_set` table, `docs/plan.md` "Jeff's open decisions" (decision 5: the calibration set) and S6, `reports/16-extract.md`.

Stay in plan mode until I approve. Propose the stratification, the coding interface for the two human coders, the consensus step, the split, the metric implementations and the results-ledger format. Wait.

**Operator refinement, not a stop:** the gold set needs two human coders, and that happens when it happens. Build the full harness and the coding tools now and run the harness on a chain of label sources (`validate.labels.gold_set`, `validate.labels.sample_tally`, `validate.labels.none`, policy `first_available`): with no human labels at all it still reports the automatic metrics (span grounding, abstention rate, three-run disagreement) and says plainly that model–human agreement is pending. Do not synthesize human labels.

## What to build

1. **Gold-set draw.** `efp gold draw`: 200–300 rows stratified by year band (2000–09, 2010–16, 2017–25), fraud type, and evidence tier as observed in the first extraction pass (so the strata exist); the 50 frozen sample rows are included. Frozen and hashed (`set_version`, `set_sha256` via `instruments`) *before any prompt is iterated on it*. Split 60/40 into `dev` (prompt iteration) and `heldout` (touched exactly once for the reported numbers); the split is part of the hash.
2. **Double coding.** `efp gold code --coder A|B`: presents each gold row's captured sources (text with the relevant passages, never the model's proposals) and records, per field, the coder's value and span into `gold_set(coder_a | coder_b)`. `efp gold resolve`: shows disagreements to both and records `consensus`. Coders never see each other's values before resolution. Human–human agreement is computed and reported as the ceiling the model is measured against.
3. **The harness** (`efp validate`), hand-rolled on scikit-learn, per spec:
   1. Per field, `classification_report` giving precision, recall and F1 per label, with **abstention as its own class**.
   2. Span grounding scored separately: does the returned span exist verbatim in the source (automatic), and does a human agree it supports the value (recorded in the coding interface as a yes/no per model extraction shown after the human's own coding).
   3. Model–human agreement as Krippendorff's α and Cohen's κ, alongside human–human α (the `krippendorff` package computes numbers only and is not linked into the published package; note the license).
   4. Bootstrap confidence intervals over cases for every headline number.
   5. Results appended to `reports/validation/ledger.jsonl`, never overwritten, keyed to the gold-set hash, the prompt hashes, the model id and the codebook version; a Markdown summary regenerated from the ledger.
4. **Held-out discipline.** The harness refuses to score `heldout` more than once per (prompt hashes, model id) tuple unless `--i-know` is passed, and logs every scoring in the ledger, so "touched exactly once" is auditable.
5. **First-pass mode.** `efp validate --sample-tally` scores the model's sample extractions against my accept/reject tally from build 16 (`reports/first-pass/sample_tally.md`, parsed), producing the same report shape with the smaller n and wider intervals, clearly labeled as the first-pass stand-in.

## Tests

- Stratified draw reproducibility and hash; split membership stable; the 50 sample rows included.
- Metric implementations against hand-computed values on tiny synthetic label sets (including abstention as a class and missing values for α).
- Held-out guard; ledger append-only; summary regeneration.
- Coding interface records values and spans and hides the other coder's values.

## Done when

- `uv run efp validate` runs today on whatever labels exist (the tally if I have filled it, otherwise automatic metrics only) and writes the first ledger entry and summary, labeled with which label source it used.
- `uv run efp gold draw` produces the frozen gold set with its hash, and `efp gold code` works end to end on two synthetic coders in the test suite.
- `docs/methods_appendix.md` has a validation section that the summary regenerates (build 20 completes the appendix).

## Guardrails that bite here

No synthetic human labels; the held-out set is scored once; results are appended, never overwritten. Gold-set rows' sources are captures; the coding interface shows nothing fetched live.

Finish with `reports/17-validation.md` and a pull request. Do not merge.

# Build 18: Coding — the codebook as data

Spec: section 9 ("Coding": ordered rules, first that fires wins; the rule and the extraction it consumed are written with the codebook version), section 4 (code component contract: rules are data in `codebook.yaml` applied by a small interpreter), section 1 (F9), section 3 (`codes` table). Task list: D9. Needs: 16, 17. Branch: `build/18-code`. Report: `reports/18-code.md`.

Read first: `CLAUDE.md`, `build/README.md`, `docs/spec.md` section 9 "Coding" and the `codes` and `decisions` tables, **`docs/research-plan.md` in full** (the taxonomy, the evidence tiers, the subject-type distinction and the decision rules are the codebook; the thirteen output columns and their names are defined there), `docs/summary.md` "What I am proposing" (what Dr. Treistman was told the three outputs are), `reports/16-extract.md`, `reports/17-validation.md`.

Stay in plan mode until I approve. Propose `codebook.yaml`'s structure, the interpreter, the column list, the public-figure rule, and the review flow. If `docs/research-plan.md` is missing or does not define the thirteen columns, do not invent a research design: build the interpreter and the rules the Summary and the spec do define, name the columns from the spec's own terms (Motive Primary, Ideological Motivation, Partisan Direction, Subject Type, Evidence Tier, Identity Status, Identity Confidence, Court Record Status, Capture IDs, Social URLs, Codebook Version, Rule Fired, Notes) under the provisional prefix, record the gap as the first decision in the report, and make the column names a mapping in `codebook.yaml` so the research plan's names replace them at export without a recode. Wait.

## What to build

1. **`codebook/codebook.yaml`**, transcribed from the research plan: the motive taxonomy (ideological or partisan cause, personal electoral gain, financial, eligibility confusion, convenience, acting for a family member, protest, official misconduct, identity concealment, unknown), the evidence tiers (stated_by_subject, court_document, news_reporting, circumstantial, none), subject type (public figure: candidate, officeholder, party operative; private individual), partisan direction (as the research plan defines it; the raw party is stored and the mapping to Dr. Treistman's dropdown, including what "independent" means, is a separate rule because decision 3 is open), and the ordered decision rules. `codebook/CHANGELOG.md` with a frozen version string; the file is hashed through `instruments` and written to `codes.codebook_version`.
2. **The interpreter.** `efp/code/` applies the rules as data: each rule names the extraction classes and attributes it consumes, the conditions, the output column and value, and the evidence tier it assigns. Rules are ordered and the first that fires wins: stated by subject → court document → news reporting → circumstantial → unknown. The interpreter writes `codes` with `extraction_id` (the extraction consumed), `decision_id` (when a human decision was involved), `evidence_tier`, `codebook_version`, and records in `summary_json` the rule id that fired. No conditionals about motive live in Python; a test asserts the interpreter contains no category names.
3. **Columns.** The thirteen output columns from the research plan, each written under the `export.provisional_prefix` until build 21's review lifts it. Among them, as the spec names them: Motive Primary, Subject Type, Partisan Direction, Evidence Tier, plus identity confidence and status, court-record status, source capture ids, social URLs (populated only for rows the social stage reached), and notes. Take the exact names and order from the research plan.
4. **What may drive a code.** Only extractions with `alignment_status != 'unaligned'`, from unflagged pages, not rejected by a reviewer, for rows whose identity is confirmed or review-band-resolved to confirmed. Party registration, reference-table party fields and anything about a private individual's politics that does not come from the captured record for *this case* never feed a motive code; the only party inputs are `political_role.party_on_ballot` and `beneficiary.party` from the captured text. Public figures (per `political_role`) may be coded on their public record; for private individuals the honest default is a non-ideological motive or unknown, and the rules encode that.
5. **Amendments.** `efp code diff --from <version> --to <version>` lists exactly which rows a codebook change would alter, by replaying the interpreter on stored extractions; `efp run code --reset` re-codes only those rows. This is what the version on every `codes` row buys.
6. **Review.** `efp review code` walks rows where the model-derived code and a human reviewer's code differ, or where the reviewer wants to override; the resolution is a `decisions` row (kind `code` or `override`) with rationale, and the `codes` row points at it. Nothing is coded as final without a person signing off: in the first pass every value stays provisional; build 21's review and the gold-set work are what lift it.
7. **Numbers.** Provisional distributions of motive type and evidence tier across coded rows, by subject type and by year band; the share of rows coded `unknown` by gate reason (identity unresolved, no text, abstention).

## Tests

- Interpreter against a synthetic codebook and synthetic extractions: rule order, first-fires-wins, tier assignment, abstention → unknown, unaligned excluded, rejected excluded.
- The category-name scan over `efp/code/*.py`.
- Private-individual rule: identical evidence yields a non-ideological or unknown code for a private individual where it would code a public figure on their record.
- `code diff` identifies exactly the rows a rule change affects.

## Done when

- `codebook.yaml` matches the research plan (the report lists every rule with its source paragraph).
- `uv run efp run code --sample` then the full run produce provisional codes with tiers and pointers for every row that has extractions; `efp status` shows the provisional distributions.
- `efp audit <row_id>` (build 19 completes it) can already walk code → extraction → capture for a coded row.

## Guardrails that bite here

Party registration never infers motive; private individuals and public figures are treated differently; every code carries its evidence; nothing is final without a human. The codebook is data, versioned and hashed.

Finish with `reports/18-code.md` and a pull request. Do not merge. Review checkpoint (shared with build 16): the 50 sample rows, now with their codes, against their sources.

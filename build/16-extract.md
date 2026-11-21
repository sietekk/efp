# Build 16: Extraction with verbatim spans

Spec: section 9 ("Extraction schema": the five classes, LangExtract primary, Instructor fallback, Claude citations option; temperature 0; three runs), section 4 (extract component and contract: unaligned never codes; abstention is valid), section 1 (F8), section 7 (pinned prompts). Task list: D8. Needs: 10, 13. Branch: `build/16-extract`. Report: `reports/16-extract.md`.

Read first: `CLAUDE.md`, `build/README.md`, `docs/spec.md` section 9 "Extraction schema" (the class table is the schema) and the `extractions` table in section 3, `docs/research-plan.md` (the motive taxonomy and evidence tiers the classes feed), `docs/plan.md` S6 and the review checkpoint after it, `reports/10-documents.md`, `reports/13-news.md`.

Stay in plan mode until I approve. Propose the Pydantic schema per class, the prompt files, the engine wrapper with fallback, how spans resolve to page and bbox, the budget guard, and the three-run disagreement rule. Wait.

## What to build

1. **Inputs.** For each row with text: document text captures (build 10, unflagged pages only), article text captures (builds 13 and 14), and social captures (build 15) for rows the social stage reached. Predecessors: `documents` and `news` (a `skipped` news stage still allows extraction over court text).
2. **Classes, exactly as spec section 9:**

   | Class | Attributes | Feeds |
   | --- | --- | --- |
   | `stated_motive` | speaker (subject / counsel / prosecutor / reporter), context (plea / interview / post / filing) | Motive Primary, Evidence Tier |
   | `political_role` | role (candidate / officeholder / official / agent / operative), office, party_on_ballot, race_year | Subject Type, Partisan Direction |
   | `organization` | name, orientation_as_reported | Partisan Direction, Motive |
   | `beneficiary` | candidate_or_measure, party | Partisan Direction |
   | `identifier` | kind (middle_name / dob / age / city / case_number / handle / email) | identifiers table (adds to build 07's rule-based rows, never replaces them) |

   Every field returns `(value, verbatim_span, char_start, char_end, alignment_status)`; `alignment_status` ∈ {match_exact, match_greater, match_lesser, match_fuzzy, unaligned}. An unaligned extraction is stored and never eligible for coding. Abstention (`value = NULL`) is valid and scored.
3. **Engines as modules** (`extract.langextract`, `extract.instructor`, `extract.anthropic_citations` under `[stages.extract]`, policy `first_available`, so a missing package or a provider outage swaps engines without stopping the stage; the engine used is recorded per extraction). LangExtract 1.7 as primary (its Extraction carries `char_interval` and `alignment_status`, and returns None for an interval it cannot locate). Instructor 1.17 as fallback with the exact-citations pattern: a `substring_quote` list on the model and a validator that drops any quote not found verbatim in the source, then any fact with no surviving quote. If `llm.provider` is Anthropic, Claude's native document citations are a third option that composes with Instructor; implement behind a flag and compare on the sample. The engine wrapper hides the choice; each `extractions` row records `model_id`, `prompt_sha256`, `temperature` (always 0) and `extracted_at`.
4. **Prompts as instruments.** One file per class under `prompts/` (`stated_motive.md`, `political_role.md`, `organization.md`, `beneficiary.md`, `identifier.md`), under version control, hashed through `instruments` per call. The prompt tells the model it may use only the supplied text, must quote verbatim, and must abstain when the text does not support a value. The model never receives another row's text, never receives a name without its evidence, and never receives anything from outside the captures.
5. **Spans to pages.** For document text, resolve `char_start`/`char_end` through build 10's canonical text to page and bounding box and store them in `extractions` (add columns by migration: `page`, `bbox_json`); for articles, the article capture and offsets suffice.
6. **Budget and cadence.** Before each batch, estimate tokens and refuse to exceed `llm.max_monthly_usd` (spend tracked in a `llm_spend` table from the API's usage fields). On the frozen 50-row sample run every class `llm.sample_runs` (3) times; a field whose value differs across runs is flagged `low_confidence` in `summary_json` and is reported. At volume, one run. The volume run follows the sample run in the same build unless `stages.extract.require_sample_approval = true` in `config.toml` (default false): with it on, the stage records the sample results and waits for `efp run extract --approve-volume`; with it off, the budget cap is the only brake. Either way the code is complete and the tally file is written for my review.
7. **Review surface.** `efp review extract` shows a row's extractions with the span highlighted in the captured text; a reviewer can mark an extraction `rejected` (a `decisions` row of kind `review`), which removes it from coding eligibility without deleting it.

## Tests

- Schema validation per class; alignment statuses on synthetic texts; unaligned stored but ineligible; abstention stored.
- Instructor fallback validator drops unfound quotes; the wrapper falls back when LangExtract fails.
- Prompt hashing recorded; a changed prompt file changes `prompt_sha256`.
- Budget guard stops a batch; spend table updated from stubbed usage.
- Span-to-page resolution on the synthetic PDFs from build 10.
- No live model calls in CI (stub the client); one `live` test on a synthetic paragraph for `efp selftest --live`.

## Done when

- The sample has run three times; `reports/16-extract.md` lists all 50 rows with proposed values, spans, alignment status and run agreement, so I can check each against its source, and `reports/first-pass/sample_tally.md` is pre-filled with one line per row and an empty verdict column (accept / reject / unsure, with a note) for me to complete whenever I do; build 17 parses it.
- The full run gives every row with text an extraction attempt per class (or, with `require_sample_approval` on, is ready to start with one command); abstention rate and grounding rate (extractions with a verbatim span that exists in the source) are in the report, with spend.

## Guardrails that bite here

Temperature 0; model id and prompt hash per call; captured text only; no row-level identity data in any prompt beyond the captured text itself; budget cap enforced in code. Fixtures synthetic.

Finish with `reports/16-extract.md` and a pull request. Do not merge. Review checkpoint for me: the 50 sample rows against their sources; my tally refines the prompts and the validation numbers, it does not gate the pipeline.

# Build prompts: the specification broken into Claude Code units

Each file in this folder is one message to Claude Code. Together they cover the whole of `docs/spec.md`; each carries the part of the specification it implements, so Claude Code does not have to find it, and names the spec sections to read for the rest. One prompt is one branch, one pull request, one report. `CLAUDE.md` holds the rules that apply to every one of them.

`prompts/` is **not** this folder: the spec reserves `prompts/` for the extraction prompt files, which are hashed per model call (prompt 16).

## The units, in dependency order

| # | Prompt | Spec sections | Task list | Needs | Session | Model | Operator refinement (never blocks) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 01 | [Scaffold and toolchain](01-scaffold.md) | 10 (configuration, runtime), 11 (layout, dependencies, testing) | D1 | — | S0 | opusplan | — |
| 02 | [Database schema and migrations](02-schema.md) | 3, 7 (store path), 11 (data tests) | D1 | 01 | S0 | **opus** | review the schema |
| 03 | [Ingest and the round-trip guarantee](03-ingest.md) | 4 (ingest), 1 (F1, F10), 11 (integration test) | D1, C1 | 02 | S0 | opusplan | — |
| 04 | [Stage runner and state machine](04-runner.md) | 2 (gates), 10 (orchestration, CLI) | D1 | 02, 03 | S0 | **opus** | — |
| 05 | [Capture layer and provenance](05-capture.md) | 4 (capture), 7, 10 (rate limiting), 5 (Wayback, SPN2, Perma, TSAs) | D2 | 02, 04 | S1 | **opus** | — |
| 06 | [Link resolution](06-resolve-links.md) | 4 (resolve_links), 6 (Heritage shorteners), 5 (CDX, Common Crawl) | D3 | 04, 05 | S1 | opusplan | the domain table |
| 07 | [Federal court record: DOJ, CourtListener, RECAP](07-court-federal.md) | 4 (court), 5 (DOJ, CourtListener, RECAP, PACER) | D4 | 05, 06 | S2 | opusplan | — |
| 08 | [State AG and DA press-release harvester](08-court-state-ag.md) | 6 (harvester), 4 (court) | D4 | 07 | S2 | sonnet | — |
| 09 | [State docket adapters and the portal probe](09-court-state-dockets.md) | 6 (docket adapters), 12 (risks) | D4 | 08 | S2 | opusplan | the coverage matrix and the gated list |
| 10 | [Documents: text, OCR and page provenance](10-documents.md) | 4 (documents), 1 (F6), 7 (original bytes), 10 (runtime) | D6 | 07 | S3 | opusplan | — |
| 11 | [Reference datasets and loaders](11-reference-data.md) | 5 (bulk datasets, OpenFEC, Congress.gov, OpenStates, FTM), 12 (licenses) | D5 | 02, 05 | S3 | sonnet | — |
| 12 | [Identity resolution](12-identity.md) | 8, 4 (identity), 1 (F4) | D5, F3 | 07, 10, 11 | S4 | **opus** | label 150 pairs (then `efp identity refit`); the F3 audit |
| 13 | [News retrieval](13-news.md) | 4 (news), 5 (GDELT, Media Cloud, CDX), 1 (F5) | D7 | 06, 12 | S5 | opusplan | — |
| 14 | [Manual import path for licensed sources](14-manual-import.md) | 6 (licensed sources), 4 (news, documents) | D7 | 10, 13 | S5 | sonnet | — |
| 15 | [Social: handle-only pivot](15-social.md) | 4 (social), 1 (F7), 12 (compliance) | D10 | 12, 13 | S5 | opus | record the IRB determination in `config.toml` `[irb]` |
| 16 | [Extraction with verbatim spans](16-extract.md) | 9 (extraction schema), 4 (extract), 1 (F8) | D8 | 10, 13 | S6 | **opus** | the 50-row tally (volume run proceeds unless `stages.extract.require_sample_approval`) |
| 17 | [Gold set and validation harness](17-validation.md) | 9 (gold set, harness), 11 (model tests) | D8, E2 | 03, 16 | S6 | opusplan | double coding of the gold set |
| 18 | [Coding: codebook as data](18-code.md) | 9 (coding), 4 (code), 1 (F9) | D9 | 16, 17 | S6 | **opus** | review the 50 sample rows |
| 19 | [Export, status and audit](19-export-report.md) | 4 (export), 1 (F10, F11), 7 (derivation chain), 10 (CLI) | D11, D12, F1 | 12, 13, 18 | S7 | opusplan | F1 on the real export |
| 20 | [Integrity packaging, compliance and methods](20-integrity-compliance.md) | 7 (BagIt, RO-Crate, manifests), 11 (reproducibility), 12 (license register, compliance) | D12 | 05, 19 | S7 | opusplan | — |
| 21 | [QA review and first-pass freeze](21-qa-freeze.md) | plan "What the first pass hands back" | E2 (replaced) | 19, 20 | S8 | opusplan | review 100 rows (freeze runs with status recorded; re-tag after) |

**Two rules every prompt obeys** (they are in `CLAUDE.md`): everything in a unit is built completely, with nothing left for later because an operator step has not happened; and every stage is a chain of swappable modules (build 04 defines the interface), so a missing key, a gated source, an absent tool or a pending human input is skipped with its reason and the step completes on what is available. The "Operator refinement" column lists your inputs; each one refines results when it arrives and never gates a build or a run. The IRB determination is in hand (November 21, 2026); prompt 15 only asks that it be recorded in `config.toml` `[irb]` so the stage and the methods appendix can cite it.

Not written, by design: the Postgres migration (spec section 3, "Postgres path") is a documented trigger, not a build unit; write a prompt for it only if a second operator joins. Purchases and applications named in the spec (PACER, CourtListener EDU, Perma registrar, Ballotpedia academic access, Reddit for Researchers, Meta Content Library, X API) are gated sources: the prompts that touch them wire the configuration and record the gate; nothing applies, signs up or pays.

## Models and token discipline

Three different model choices are in play, and only one of them is the pipeline's.

**1. The Claude Code agent that builds.** `.claude/settings.json` (committed) sets the project default to `opusplan`: Opus while in plan mode, Sonnet once you approve and it executes. That is the token-efficient shape for most builds, because the expensive thinking happens on the short proposal and the long tool-heavy implementation runs on the cheaper model. The **Model** column overrides it:

- `opus` for the builds whose contract is the product and whose mistakes propagate: schema (02), runner and gates (04), capture layer (05), identity (12), extraction (16), coding (18). Launch those with `claude --model opus --permission-mode plan`.
- `sonnet` for builds that are many similar modules or loaders against a fixed interface: state AG modules (08), reference loaders (11), manual import (14). Launch with `claude --model sonnet --permission-mode plan`.
- `opusplan` for everything else: the default from settings, so just `claude --permission-mode plan`.

Aliases track the current version of each tier on your account (`/model` shows what they resolve to), so nothing here pins a model id. Effort stays at each model's default; raise it with `/effort high` only inside a build where the plan-mode proposal was wrong twice.

**2. Subagents the builder delegates to** (`.claude/agents/`, committed, each with its own model):

| Agent | Model | Job | Why that model |
| --- | --- | --- | --- |
| `spec-brief` | haiku | Copies the named spec sections, tables and report facts into one brief | Verbatim retrieval; no judgment needed |
| `test-runner` | haiku | Runs ruff, mypy, pytest and returns only failures with path:line | Keeps hundreds of lines of test output out of the main context |
| `Explore` (overrides the built-in) | haiku | Read-only search of the repo | Small codebase; exploration does not need Opus |
| `done-when-reviewer` | opus, effort high | Cold review of a finished build against its "Done when" list, the guardrails and the spec contract, before the report | The one place quality is checked by a model that did not write the code |

`CLAUDE.md` tells the builder to use them: brief first, tests through the runner, reviewer before the report. Net effect per build: one Opus pass to plan, Sonnet to build, Haiku for the chatter, one Opus pass to grade.

**3. The pipeline's own model** (`config.toml` `[llm]`): the extraction engine and the OCR fallback that the *code* calls, billed to the API key, capped by `max_monthly_usd`. Sonnet-class for extraction (the plan's estimate: on the order of $100–200 over the file; several times that on Opus for no gain on a fixed-schema task), the same for page transcription (few pages), temperature 0, three runs only on the 50-row sample. Confirm the current Sonnet model id in build 01 and pin it there; the id is logged on every call.

## How the first-pass sessions bundle them

`docs/plan.md` runs the first pass as nine sessions. Each session is a sitting; each prompt within it is still its own branch, PR and report.

| Session | Prompts | Review checkpoint after it |
| --- | --- | --- |
| S0 Skeleton | 01, 02, 03, 04 | schema, CLI, append-only triggers, data folder |
| S1 Capture and links | 05, 06 | link survival and the domain table |
| S2 Court record | 07, 08, 09 | coverage matrix and the gated-portal list |
| S3 Documents and reference data | 10, 11 | page-quality counts; reference tables loaded |
| S4 Identity | 12 | thresholds and the F3 audit |
| S5 News and social | 13, 14, 15 | news reach by era; the NewsBank queue; rows the social stage touched |
| S6 Extraction and coding | 16, 17, 18 | the 50 sample rows against their sources |
| S7 Export and reporting | 19, 20 | F1 on the real export |
| S8 QA and freeze | 21 | the first-pass notes |

## Running one

```
cd <repo> && git checkout main && git pull && uv sync
claude --permission-mode plan                 # opusplan from settings
claude --model opus --permission-mode plan    # builds 02, 04, 05, 12, 16, 18
claude --model sonnet --permission-mode plan  # builds 08, 11, 14
```

Paste the prompt file. Claude Code reads `CLAUDE.md`, this index, the spec sections the prompt names (through `spec-brief`), and the reports of the prompts it needs; proposes; waits; builds on `build/NN-<name>`; runs the gates through `test-runner`; has `done-when-reviewer` grade the result; writes `reports/NN-<name>.md`; opens the PR. You review the report, spot-check with `efp review`, merge, and paste the next one. One build per Claude Code session: a fresh session is the cheapest context there is.

Every prompt ends the same way: the "Done when" lines are the acceptance test, and the report says whether each holds, with evidence.

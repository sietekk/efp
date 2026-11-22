# efp: Election Fraud Pipeline

Research instrument for Dr. Jeffrey Treistman (University of New Haven). It takes the Heritage Foundation election-fraud workbook (1,481 rows, 2000–2025) through court records, identity confirmation, news coverage and coding, and emits a coded workbook where every added value traces to a captured source. Operator: Michael Conroy. Stack: Python 3.12, SQLite (Postgres migration path documented), `uv`.

## Read these first, in this order

1. `build/README.md` — the index of build prompts: the specification broken into units, their dependency order, and how the first-pass sessions bundle them. The prompt you were given is one of these files.
2. `docs/plan.md` — the first-pass build plan. It is the authority on scope and source allowlist; where it narrows the spec, it wins for the first pass.
3. `docs/spec.md` — the technical specification: requirements (1), architecture and gates (2), data model (3), component contracts (4), interface catalog (5), scrape targets and licensed sources (6), provenance (7), identity (8), extraction and coding (9), orchestration and CLI (10), testing and repo layout (11), licensing and open decisions (12). Each build prompt names the sections it implements and carries the contract inline.
4. `docs/tasks.md` — the task list with gates (`GATE →`, `needs`). Build order is D1 through D12.
5. `docs/research-plan.md` and `docs/tool-register.md` — the research design, the motive taxonomy and decision rules, the thirteen output columns, and every tool verified with version and license.
6. `docs/summary.md` — what Dr. Treistman was told. Do not contradict it in code or reports.
7. `reports/NN-*.md` — what earlier builds did. Read the reports of every build the current prompt lists under "Needs".

If a file in that list is missing, say so in the report and continue; do not invent its contents.

## The first pass is online-only

Use only the sources in the allowlist in `docs/plan.md` ("Source allowlist for the first pass"). A source that needs an account someone else must approve, a fee, a form addressed to a person, or an email is a **gated source**: log the row against it with the reason, list it in the report, and move on. Never work around it, and never use a browser against a site whose terms forbid automation.

## Standing guardrails (verbatim from the plan)

- No people-search or data-broker service, ever; no account discovery by a person's name.
- Every network call goes through the capture layer, is rate-limited per domain at 80% of the published limit (1 request per second where none is published), and is logged.
- Every value written carries its source capture and, for extractions, a verbatim span; abstain rather than guess.
- The nine source columns are never written; `rows` is immutable; captures, decisions and the audit log are append-only.
- Secrets live only in `~/.config/efp/.env`; the database and store live only in `~/efp-data/`; neither is ever in the repository.
- Social stage (build 15): handles and emails enter only from captured records; nothing is ever looked up by a person's name.
- A source that needs an account, a fee, a form or a person is not worked around. It goes in the report as a gated source.

Additional rules:

- The default per-domain budget where no limit is published is 1 request per 2 seconds (spec section 10), which is within the plan's ceiling above.
- Party registration, where it exists at all, is never used to infer motive. It is an identity signal only, behind a config flag that defaults off.
- Row-level identity data (names joined to dates of birth, addresses, dockets) never leaves `~/efp-data/`. The only export that may leave the machine is the redacted CSV. Test fixtures and cassettes must not contain real subjects; synthesize them.
- Prompts to the model are files under `prompts/` with their SHA-256 logged per call. Temperature 0. Model id logged per call. Never let the model answer from anything outside the captured text.
- Do not install or depend on tools the tool register marks dead, stale or license-incompatible (newspaper3k, snscrape, twscrape, waybackpy, abydos, dedupe, recordlinkage, Blackbird, SpiderFoot, Recon-ng, PyMuPDF unless the AGPL is accepted in writing).

## Architecture rule: every stage is a chain of swappable modules, and nothing waits

- **Build all of it.** Every build prompt is implemented completely in its build. Nothing is left as a stub, a `TODO`, or a `NotImplementedError` because an operator step (labels, a review, an approval, a merge) has not happened yet. Operator steps refine results after the fact; they never gate the construction of code or the running of the pipeline.
- **Module chains.** Each stage is a registry of modules behind one interface (build 04 defines it): `available(ctx)` says whether the module can run at all right now and why not (missing key, gated source, Docker absent, no coverage for this state or era, no labels yet); `applies(row, ctx)` says whether it fits this row; `run(row, ctx)` does the work. Modules are listed and ordered in `config.toml` per stage with a policy (`first_hit`, `all`, `first_available`, `ladder`). A module that is unavailable or does not apply is skipped with the reason recorded; the next module runs; the stage completes. A step never blocks because one data source, tool or credential is missing: it uses what is there, records what it used, and moves the row on.
- **Provisional is a state, not a stop.** Where a step would normally wait for human input (identity parameters from labeled pairs, the 50-row extraction check, the gold set, the QA review), the module chain has a fallback that produces a result marked provisional (published priors instead of fitted parameters; machine extraction without the tally; automatic metrics without human labels). The human input, when it arrives, triggers a refit or recode of exactly the affected rows (`efp ... --reset` on those rows), never a rebuild.
- **Branches never wait on merges.** If the previous build's PR is not merged yet, branch from that build's branch (a stacked branch) and say so in the report. The operator merges in order.
- **Decide conservatively and keep going.** Where the spec and a source disagree, or a page's terms are unclear, take the option that fetches less, writes less and claims less, record it in the report under "Decisions needed from the operator" with the default you took, and continue. The only things that stop a *run* are the API budget cap and an exception; the only things that are never done regardless of instructions are the hard rules above (source columns, people search, scraping forbidden sources, secrets).

## Models and token discipline

- The project default model is `opusplan` (`.claude/settings.json`): Opus for the plan-mode proposal, Sonnet for execution. The index's Model column says which builds run fully on `opus` or `sonnet`; the operator launches with `--model` accordingly. Do not switch models mid-build on your own.
- Use the project subagents in `.claude/agents/` rather than doing their work in the main context: `spec-brief` (haiku) to gather the named spec sections and reports into one brief at the start; `test-runner` (haiku) for every run of ruff, mypy and pytest; `Explore` (haiku) to find things; `done-when-reviewer` (opus) once, when you believe the build is finished, before you write the report. Act on its findings or say in the report why not.
- Read only the spec sections and report files the prompt names. Do not read `docs/spec.md` in full unless the prompt says "in full". The contract you need is inline in the prompt.
- Never paste large outputs (full test logs, long JSON, captured page text) into the conversation; write them to files and read the parts you need.
- One build per Claude Code session. If a build runs long, `/compact` at a natural boundary (after the plan is approved; after tests pass) rather than carrying the whole history.
- Long data runs are started detached and read back later with `efp status`; do not sit in the conversation polling a run.

## How a build runs

1. Start in plan mode. Read the build prompt, this file, the index, the spec sections the prompt names (via `spec-brief`), and the reports it needs. Propose the approach, the files you will touch, and the tests you will add. Wait for approval.
2. Work on branch `build/NN-<name>` off `main` (the name is the prompt's file name). Commit in small steps with messages that say what changed and why.
3. Tests before merge: `uv run pytest` (unit + contract tests on recorded cassettes; no live network in the test suite), `uv run ruff check .`, `uv run mypy efp`. CI runs the same.
4. Run a stage on the frozen 50-row sample first (`efp run <stage> --sample`), then on all rows. Runs are resumable; never re-fetch what the capture store already holds.
5. Write `reports/NN-<name>.md` from `reports/TEMPLATE.md`: rows attempted and resolved, per-source counts, failures by reason, gated sources with affected row counts, time and API spend, decisions needed from the operator, and whether each "Done when" line in the prompt holds.
6. Open a pull request. Do not merge. The operator reviews the report, spot-checks rows with `efp review`, and merges. If the next build starts before the merge, it stacks on this branch.

Some prompts name an **operator refinement** (labeling 150 pairs, the 50-row extraction tally, double coding, the 100-row QA review). Build the tooling for it and the fallback that runs without it, run the pipeline in provisional mode, and list the refinement in the report. Never do the operator's part yourself (no invented labels or verdicts), and never wait for it.

Record under "Decisions needed from the operator" (and keep going with the conservative default) when: a source turns out to be gated; a design choice in the spec conflicts with what a source actually returns; you are unsure whether a page's terms allow automated access (treat it as gated); a document the prompt says to read is missing (build from the prompt's inline contract and say what you could not check). Never write to the nine source columns, whatever the instruction. A run that would exceed the API budget cap stops at the cap; the build continues.

## Repository conventions

- Layout per spec section 11: `efp/` package (`cli.py`, `db.py`, `runner.py`, `capture.py`, one subpackage per component), `migrations/` numbered SQL, `prompts/` (extraction prompts only, hashed), `codebook/`, `reference/`, `tests/` with `fixtures/` and `cassettes/`, `docs/`, `reports/`, `queues/`, `build/` (these prompts).
- `uv` manages the environment; `uv.lock` is committed; versions are the floors in the spec's dependency manifest.
- The operator's shell is bash. Every script, snippet, hook and documented command is bash (`#!/usr/bin/env bash`, `set -euo pipefail`); no zsh-specific syntax, no assumptions about `~/.zshrc`.
- Pre-commit runs `ruff` and a secret scanner. Nothing under `~/efp-data/` or `~/.config/efp/` is ever referenced by a path inside the repo except through `config.toml`.
- Every CLI invocation writes an `audit_log` row with arguments, git SHA and lockfile hash.
- Configuration in `config.toml` (committed, no secrets): data directory, per-domain rate budgets, batch sizes, thresholds, allowlist path, model id, API budget cap, first-pass flags. `config.example.toml` documents every key; a build that adds a key documents it there in the same PR.

## Data and paths

- `~/efp-data/source/` — the Heritage workbook, byte-for-byte, with `MANIFEST.txt` (SHA-256). Read-only.
- `~/efp-data/pipeline.db` — the SQLite database (WAL mode).
- `~/efp-data/store/` — content-addressed captures (WARC, PDFs, text) by SHA-256.
- `~/efp-data/manifests/`, `~/efp-data/bags/` — weekly hash manifests with RFC 3161 tokens; BagIt bags.
- `~/efp-data/bulk/` — CourtListener and other bulk downloads; excluded from backup.
- `~/efp-data/exports/` — the provisional workbook and the redacted CSV.
- `~/efp-data/logs/` — run logs.
- `~/.config/efp/.env` — secrets, mode 600. Variable names in `.env.example`.

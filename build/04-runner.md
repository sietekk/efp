# Build 04: Stage runner and the per-row state machine

Spec: section 2 (components, per-row state machine, gates), section 10 (orchestration: the eligibility query is the scheduler; CLI `run` and `status`). Task list: D1. Needs: 02, 03. Branch: `build/04-runner`. Report: `reports/04-runner.md`.

Read first: `CLAUDE.md`, `build/README.md`, `docs/spec.md` sections 2 and 10 and the `case_stage` table in section 3, `docs/figures/figure-3-row-state-machine.png`, `reports/02-schema.md`, `reports/03-ingest.md`.

Stay in plan mode until I approve. Propose the predecessor map, how a stage with two predecessors fits the spec's one-predecessor query, the claiming transaction, and the `status` output. Wait.

## What to build

1. **The component contract.** `efp/runner.py` loads a component by stage name and calls its `gate(row) -> bool | str` and `run(batch, ctx) -> list[StageResult]`. `StageResult` carries `row_id`, `status` (`done`, `failed`, `needs_review`, `skipped`), `input_hash`, `tool_version`, `error`, and a free `summary_json` the report can aggregate. Components never import each other; they share only the database and `ctx`.
1a. **The module chain (`efp/modules.py`).** Every stage's work is done by modules behind one interface, so a missing source, key, tool or human input swaps out rather than stops:

   ```python
   class Module(Protocol):
       name: str            # "court.doj", "news.gdelt", "extract.langextract", "identity.params.published_priors"
       stage: str
       def available(self, ctx: Context) -> Availability: ...   # ok, or unavailable(reason: str)
       def applies(self, row: Row, ctx: Context) -> bool | str: ...  # e.g. GDELT: year window reaches 2017
       def run(self, row: Row, ctx: Context) -> ModuleResult: ...   # hit | miss | error, plus what it wrote
   ```

   A registry maps names to classes (entry points in `pyproject.toml` or an explicit dict; no import-time magic). `config.toml` lists each stage's modules in order with a policy: `first_hit` (stop at the first module that produces the stage's result; court, link resolution), `all` (run every applicable module; news, identity candidate sources), `first_available` (use the first module that can run; extraction engines, identity parameter sources), `ladder` (per item, try modules in order until one meets a quality bar; document text). The stage wrapper in the runner iterates the chain, records every module's outcome in `summary_json.modules` as `{name, status: hit|miss|skipped, reason, ms}`, and completes the row when the policy is satisfied or every module has been attempted. An unavailable module is `skipped` with its reason, never an error; a row is never left `failed` because a module could not run. `efp modules [--stage]` prints every registered module with its availability and reason, which is also the gated-source list for the report. Later builds register their modules here; this build ships the framework with two fake modules under test and the per-stage config keys documented in `config.example.toml`.
2. **Stage order and predecessors.** Stages, from `config.toml` `runner.stages`: `resolve_links`, `court`, `identity`, `news`, `documents`, `social`, `extract`, `code` (`export` is a command, not a stage). Predecessors: `resolve_links` ← ingest (the row exists); `court` ← `resolve_links`; `identity` ← `court`; `documents` ← `court`; `news` ← `identity`; `social` ← `news`; `extract` ← `documents`, `news` and `social`; `code` ← `extract`. The spec's eligibility query names one predecessor; generalize it to a list where every predecessor must be `done` or `skipped`, keeping it one query (a correlated `NOT EXISTS` over the predecessor list is enough). A `skipped` predecessor counts as satisfied so that a row stopped at a gate still reaches `code` and is coded `unknown` with the gate recorded.
3. **Gates, exactly as spec section 2:** `identity` requires `court` done; `news` requires `identity` done with the identity outcome not `unresolved`; `social` requires `news` done, identity `confirmed`, at least one `identifiers.kind IN ('handle','email')`, `first_pass.social_enabled` and `irb.cleared` (else the gate reason is `social_disabled` or `irb_not_recorded`); `code` requires `extract` done. A failed gate marks the row's stage `skipped` with `gate_reason` set, and every downstream stage of that row is also marked `skipped` with the same reason, so `efp status` can say how many rows stopped where.
4. **Claiming.** One transaction selects up to `runner.batch_size` eligible rows with the spec's ordering (`year DESC, row_id`), filtered by `scope.population`, `--state`, `--year-from`, `--limit`, and `--sample` (the frozen `tests/fixtures/sample_rows.json`), sets them `running` with `started_at` and `attempts + 1`, and commits. Processing happens outside the transaction; results are written per row as they finish, so an interrupted run loses nothing but the rows still `running`, which a `--recover` flag (or the next run after a configurable staleness) resets to `pending` without incrementing `attempts`. `attempts < runner.max_attempts_per_stage` (3) bounds retries; the fourth attempt needs `--retry-failed`.
5. **Idempotence by input hash.** Each component computes `input_hash` over the inputs it reads for the row (the runner offers a helper that hashes a list of values). If the stored `input_hash` for `(row, stage)` equals the new one and the status is `done`, the runner records a no-op and does not call `run`. `--reset` clears the stage's status to `pending` for the selected rows but never deletes captures; `--force` ignores the hash for one run.
6. **Dry run.** `--dry-run` prints the rows that would be claimed, their gate results and the predecessor status, and writes nothing except the audit-log row.
7. **`efp status [--stage] [--state] [--format md|json]`.** The stage × status matrix over in-scope rows, then gate reasons by stage with counts, then per-stage attempt and error summaries (top `last_error` strings), then the frozen-sample subset of the same. The Markdown form is what later builds extend into the first-pass report; make the renderer reusable.
8. **Long runs.** Runs are safe to start detached (`nohup uv run efp run <stage> > ~/efp-data/logs/<stage>.log &`); document in `RUNBOOK.md` and make sure a second concurrent `efp run` of the same stage sees `running` rows and does not double-claim (the claim is the transaction; test it with two processes).

## Tests

- Module chain: each policy with fake modules (available / unavailable / not applicable / error); outcomes recorded; an unavailable module never fails a row; `efp modules` lists availability with reasons.
- Eligibility: a predecessor `failed` blocks; `skipped` satisfies; two-predecessor `extract` requires both.
- Gates: each of the four gates, both outcomes, with downstream `skipped` propagation and `gate_reason` text.
- Idempotence: same input hash is a no-op; `--force` reruns; `--reset` repends without touching captures.
- Claiming under concurrency: two runners, no row claimed twice.
- Attempts: third failure leaves `failed`; `--retry-failed` reruns once more.
- `status` renders the matrix for a synthetic database and sums to the row count.

## Done when

- `uv run efp run resolve_links --dry-run` lists claimable rows after ingest; every other stage reports zero eligible rows with the predecessor named as the reason.
- `uv run efp status` renders the empty matrix for 1,481 rows and the sample subset; `uv run efp modules` lists the framework's registered modules.
- An interrupted run (kill the process mid-batch in a test) resumes without duplicate work.

## Guardrails that bite here

The runner is the only code that changes `case_stage`. It never writes to `rows`, never deletes anything, and writes an `audit_log` row for every invocation. The social gate must name its reason (`social_disabled`, `irb_not_recorded`, `no_handle`, `identity_not_confirmed`) rather than fail.

Finish with `reports/04-runner.md` and a pull request. Do not merge. Builds 01 to 04 are the S0 review checkpoint: I review the schema, the CLI and the data-folder layout; build 05 stacks on this branch if the merges are pending.

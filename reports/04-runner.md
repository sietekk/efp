# Build 04 — Stage runner and the per-row state machine

Branch: `build/04-runner` (off `main` at bd4294e; build 03 is merged, so nothing is stacked) · PR: see the pull request for this branch · Date: 2026-11-21 · Model: claude-sonnet-5-5 · Prompt: `build/04-runner.md` · Spec sections: 2, 10, and the `case_stage` table in 3

## What this build set out to do

Build the runner: a component contract, the module-chain framework (`available` / `applies` / `run`, four policies), the predecessor map with a multi-predecessor eligibility query, the four gates, one-transaction claiming, idempotence by input hash, `--dry-run`, `efp status` and `efp modules`, and safe detached runs. Everything else (court, identity, news and so on) registers into this later.

Changes the approved proposal made to the prompt (all operator-approved before coding):

- **Gate cascade narrowed.** The prompt says a failed gate skips every downstream stage and also that a stopped row still reaches `code`. Read literally, a `no_handle` skip on `social` would skip `extract` for nearly every row. A gate failure now cascades only along the structural dependency: `identity` failures skip `news` and `social`; `news` and `social` failures cascade nowhere; `extract` and `code` are never cascaded. The `code` gate passes when `extract` is `done` or `skipped`.
- **`--recheck` added.** The eligibility query only selects `pending` and `failed` rows, so a `done` row with an unchanged hash could never be seen. `--recheck` also looks at `done` rows and skips those whose `input_hash` is unchanged; `--force` is `--recheck` ignoring the hash.
- **New config key** `runner.stale_after_minutes = 120`.

## What was built

- `efp/runner.py` (the only writer of `case_stage` after ingest seeds it): `PREDECESSORS`, `CASCADE`, the gates (`GATES`), the eligibility query, the claiming transaction, `recover`, `reset_rows`, `run_stage`, `dry_run`, `input_hash`, component loading.
- `efp/modules.py`: `Module` protocol, `Availability`, `ModuleResult`, `Registry` (explicit dict), `run_chain` (first_hit / all / first_available / ladder), `run_stage` (every chain of a stage; records `summary_json.modules`), `describe` (the gated-source list).
- `efp/status.py`: `StatusReport` with `build_report`, `render_md`, `render_json`; the earlier minimal matrix functions are kept.
- `efp/cli.py`: `efp run` (all flags), `efp status [--format md|json]`, new `efp modules [--stage]`.
- `efp/types.py`: `StageResult` gains `input_hash`, `tool_version`, `error`, `summary_json`.
- **Schema:** migration `0005_case_stage_summary.sql` adds `case_stage.summary_json` (the prompt has `summary_json` on the result but the table had nowhere to keep it). Migration tests that hard-coded four migrations were updated.
- **Config:** `runner.stale_after_minutes` in `config.toml`, `config.example.toml`, the test fixture and `efp/config.py`. `pyproject.toml`: `S608` is ignored for `efp/runner.py` and `efp/status.py` only (SQL built from fixed fragments with bound values).
- Docs: `RUNBOOK.md` (detached runs, recovery, flags), `migrations/README.md`.
- Tests: `tests/test_modules.py`, `tests/test_runner.py`, `tests/test_status.py`, helpers `tests/runnerhelpers.py` and `tests/runner_worker.py` (a real second process); existing tests adjusted for `run` no longer being a stub, the new status layout and migration 0005.
- Fix for a flaky test on `main` (added at the operator's request): `efp/db.py` and `efp/ingest/loader.py` (see "Failures by reason"), with `tests/test_concurrent_open.py`.
- Results: `uv run pytest -m "not live"`: 510 passed. `ruff check`, `ruff format --check` and `mypy efp`: clean. Coverage 95% overall (`runner.py` 93%).

## What was run

| Run | Rows attempted | Rows resolved | Wall clock | Requests (per domain) | API spend |
| --- | --- | --- | --- | --- | --- |
| 50-row sample | 0 | 0 | n/a | none | $0 |
| All rows | 0 | 0 | n/a | none | $0 |

No stage has a component yet, so there was nothing to run on the real workbook. The runner was exercised on synthetic databases and the synthetic 20-row workbook only. **I could not read `~/efp-data` from this session** (the shell refused access, and I did not work around it), so the Done-when commands below were not run against the real 1,481-row database; they are listed under "Done-when check" for you to run.

## Results by source

Not applicable: this build makes no network calls and registers no modules. `efp modules` lists every configured module as `absent` ("not built yet") until later builds register theirs.

## Failures by reason

None in the build's own runs. One pre-existing flaky test, now fixed: `tests/test_ingest_loader.py::test_two_ingests_at_once_load_once_and_the_other_is_a_no_op` failed on a clean `main` in 10 of 15 runs. It was three real races when several processes open a brand-new database together, not a bad test:

| Reason | Where | Fix |
| --- | --- | --- |
| `database is locked` on `PRAGMA journal_mode = WAL` (SQLite refuses a concurrent WAL switch at once, without consulting the busy timeout) | `efp/db.py` `_configure` | `_enable_wal` reads the mode first and retries the switch with back-off until `busy_timeout` expires; other errors are not retried |
| `FileExistsError` creating the data directory (check-then-create) | `efp/db.py` `connect` | `mkdir(..., exist_ok=True)` |
| `sample_rows.json already exists` when the winning ingest and a no-op ingest both write the frozen sample | `efp/ingest/loader.py` `_sample_action` | a lost atomic create is re-compared with the file; an identical sample is `verified`, a different one still refuses |

After the fixes the test passed 120 of 120 consecutive runs (it had failed 3 in 80 after the first two fixes alone). `tests/test_concurrent_open.py` has six deterministic tests, one or more per race; the directory and sample ones fail without the fixes.

## Gated sources encountered (not worked around)

None. (`efp modules` is where this list will come from once modules register.)

## Numbers for the first-pass report

None yet. `efp status` is the renderer later builds extend: stage × status matrix over the population, gate reasons by stage, attempt and error summaries, and the same matrix for the frozen 50-row sample; `--format json` gives the same data.

## Spot-check guide for the operator

- `uv run efp run resolve_links --dry-run` lists claimable rows (id, year, gate result, predecessor status). Check the order is year descending, then row id.
- `uv run efp run court --dry-run` should say zero eligible and name `resolve_links` as the blocker.
- `uv run efp status` and `uv run efp status --format json`; the sample section should show 50 rows.
- Schema review for S0: `migrations/0005_case_stage_summary.sql` (one added column), the `efp/cli.py` command set, and the data-folder layout, which this build does not change.

## Decisions needed from the operator

1. **Gate cascade narrowed** (above). Default taken: identity → news, social only.
2. **`--recheck` flag** (above).
3. **`runner.stale_after_minutes = 120`** and **migration 0005** (`case_stage.summary_json`).
4. **Gate reason names** the spec does not define: `court_not_done`, `identity_not_done`, `identity_unresolved`, `news_not_done`, `social_disabled`, `identity_not_confirmed`, `no_handle`, `extract_not_done`. A missing identity decision is treated as `unresolved`, never as a pass. `gate_news` blocks only `unresolved` or no decision, as the spec words it; say so if you want an allowlist (`confirmed` plus reviewed outcomes) instead.
5. **`needs_review` or `failed` on a predecessor blocks** downstream stages, as in the spec's query. A row waiting in the identity review band therefore does not reach `news` until a decision is recorded. Say so if that should not hold the row.
6. **Stage order.** `config.toml` lists `documents` before `news`; the prompt and spec list `news` first. The predecessor map is the authority, so the order only affects display. Left as is (build 03 decision 10).
7. **Flaky loader test fixed in this PR** (above). It touches `efp/db.py` and `efp/ingest/loader.py`, which are build 02 and 03 code; no behavior changes except that concurrent first-time opens no longer fail.
8. **No generic stage wrapper in the runner.** `modules.run_stage` iterates a stage's chains for one row, but the runner does not call it: each component's `run(batch, ctx)` loops its rows and calls it. Left that way so components keep control of batching; say so if you want a `chain_component` adapter.
9. **Small known edges:** module records carry `status: "error"` and a `chain` key beyond the prompt's `hit|miss|skipped`; `--recover` while another run of the same stage is alive causes double processing of its rows (documented in RUNBOOK); a row recovered after a crashed `--retry-failed` attempt sits `pending` with attempts at the limit; `--reset` cannot be combined with `--limit` (it raises); `efp review` still prints "not built yet (build 04)", which is stale and belongs to a later build.
10. **`efp status` and the sample.** A missing or unreadable sample file simply omits the sample section.

## Done-when check

- *"`uv run efp run resolve_links --dry-run` lists claimable rows after ingest; every other stage reports zero eligible rows with the predecessor named as the reason."* Holds on the synthetic workbook: `tests/test_status.py::test_run_dry_run_after_ingest_and_every_other_stage_names_its_predecessor` and `tests/test_runner.py::test_only_the_first_stage_is_eligible_after_ingest`. The dry run opens the database without migrating (`test_cli_dry_run_and_status_change_nothing_on_a_database_at_migration_4`). **Not yet run on the real database; please run it.**
- *"`uv run efp status` renders the empty matrix for 1,481 rows and the sample subset; `uv run efp modules` lists the framework's registered modules."* Holds on synthetic data (`test_the_empty_matrix_for_a_fresh_ingest`, `test_the_frozen_sample_gets_its_own_matrix`, `test_modules_command_lists_every_configured_module`). No module is registered yet, so `efp modules` lists every configured module as `absent`. **Not yet run on the real database; please run it.** On the operator's database (at migration 0004) `status` and `--dry-run` do not migrate; the first real `efp run` applies 0005.
- *"An interrupted run (kill the process mid-batch in a test) resumes without duplicate work."* Holds: `test_a_killed_run_resumes_without_duplicate_work` hard-exits a real worker process mid-batch, then a second process with `--recover` finishes; every row is processed exactly once and the interrupted claim is not counted as an attempt. `test_two_processes_never_claim_the_same_row` runs two real processes behind a start barrier and checks both claimed rows and no row was claimed twice.

Independent review (`done-when-reviewer`) found no high-severity issues. It found three medium ones, all fixed with tests: the dry run applied a migration (M1), error text reached `case_stage` unredacted (M2, now passed through `efp.logging.redact`), and `--reset` ignored `--limit` (M3, now rejected). Also fixed: the concurrency test could pass without contention (now has a start barrier), `efp modules` omitted the `import`, `archive` and `validate` chains (now listed), and `--dry-run --reset` now reports how many rows it would reset.

## Operator decisions

Filled in by the operator at merge time. Pre-filled only where a later report explicitly records the decision (quoted, with its source); every other item is blank.

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

## Carried forward

- 12, 13, 15, 16: the gate cascade is narrowed: an `identity` failure skips `news` and `social`; `news` and `social` failures cascade nowhere; `extract` and `code` are never cascaded; the `code` gate passes when `extract` is `done` or `skipped` (decision 1).
- 12, 13, 15, 16: the gate reason names are `court_not_done`, `identity_not_done`, `identity_unresolved`, `news_not_done`, `social_disabled`, `identity_not_confirmed`, `no_handle`, `extract_not_done`; a missing identity decision is treated as `unresolved`, never as a pass; `gate_news` blocks only `unresolved` or no decision (decision 4).
- 12, 13: a predecessor in `needs_review` or `failed` blocks downstream stages, so a row waiting in the identity review band does not reach `news` until a decision is recorded (decision 5).
- 12, 13, 15, 16: `runner.stale_after_minutes = 120`: a stage row still `running` after that long is reset to `pending` without counting an attempt (decision 3).
- 09-18: the runner has no generic stage wrapper; each component's `run(batch, ctx)` loops its rows and calls `modules.run_stage` itself, so components own batching (decision 8). A stage result carries `input_hash`; `--recheck` revisits `done` rows whose hash changed and `--force` ignores the hash.

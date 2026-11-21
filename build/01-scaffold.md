# Build 01: Scaffold and toolchain

Spec: section 10 (configuration and secrets, rate limiting, runtime environment), section 11 (testing strategy, repository layout, dependency manifest). Task list: D1. Needs: nothing. Branch: `build/01-scaffold`. Report: `reports/01-scaffold.md`.

Read first: `CLAUDE.md`, `build/README.md`, `docs/spec.md` sections 10 and 11, `docs/tool-register.md` (pinned versions and the excluded tools), `docs/plan.md` "Standing guardrails".

Stay in plan mode until I approve. Propose the file tree, the `pyproject.toml` contents, the CI workflow, and how configuration and secrets are loaded. Wait.

## What to build

1. **Package and environment.** `pyproject.toml` for a package named `efp` on Python 3.12, managed by `uv`, with the console script `efp = "efp.cli:app"`. Dependency floors from the spec's manifest, grouped as it groups them (core, http, archive, court, identity, news, documents, social, extraction, validation, dev); heavy optional groups (`documents`, `social`, `extraction`, `validation`) as `uv` extras so the skeleton installs fast. Commit `uv.lock`. Do not add anything the tool register marks dead, stale or license-incompatible (newspaper3k, snscrape, twscrape, waybackpy, abydos, dedupe, recordlinkage, Blackbird, SpiderFoot, Recon-ng, PyMuPDF).
2. **Layout** per spec section 11: `efp/` with `cli.py`, `db.py`, `runner.py`, `capture.py` and one subpackage per component (`ingest/ resolve_links/ court/ identity/ news/ documents/ social/ extract/ code/ export/`), `court/adapters/ag/` and `court/adapters/docket/`, `codebook/`, `prompts/` (extraction prompts only; leave a README saying so), `reference/`, `allowlist.json` (empty list with a version field for now), `tests/` with `fixtures/` and `cassettes/`, `docs/`, `reports/`, `queues/`, `migrations/`. Component subpackages get an `__init__.py` exposing the two functions every component must have, as stubs that raise `NotImplementedError`: `run(batch: list[Row], ctx: Context) -> list[StageResult]` and `gate(row: Row) -> bool | str`. Define `Row`, `Context`, `StageResult` as Pydantic models in `efp/types.py`; `Context` carries the config, the database connection, the capture client (added in build 05) and a logger.
3. **Configuration.** `efp/config.py` loads `config.toml` from the repository root (schema: every key in `config.example.toml`, validated with Pydantic; unknown keys are an error) and secrets from the file named by `paths.env_file` (default `~/.config/efp/.env`, which must be mode 600 or the loader refuses with a clear message). Secret names, per the spec with two documented aliases: `COURTLISTENER_TOKEN`, `PACER_USER`, `PACER_PASS`, `FEC_API_KEY`, `CONGRESS_API_KEY`, `OPENSTATES_KEY`, `IA_ACCESS`, `IA_SECRET`, `PERMA_API_KEY`, `MEDIACLOUD_KEY`, `ANTHROPIC_API_KEY` (the spec's `LLM_API_KEY`; the SDK reads this name), `GOOGLE_CSE_KEY`, `GOOGLE_CSE_ID`. Secrets are never logged, never written to the database or the store, and never appear in exceptions. All `~/efp-data/...` paths are resolved from `config.toml`; no path under the home directory is hard-coded anywhere else.
4. **CLI shell.** `efp/cli.py` with Typer: every command from spec section 10 registered now (`ingest`, `run`, `status`, `review`, `import`, `identity explain`, `audit`, `export`, `manifest`, `selftest`), each a stub that exits with "not built yet (build NN)" except `selftest`, which reports: Python and package versions, which secrets are present (names only, never values), whether the data directory and env file exist with the right modes, and which optional extras are installed. A `--version` flag prints the package version and git SHA. The audit-log hook (every invocation writes an `audit_log` row) is wired here as a callback and starts writing once build 02 creates the table.
5. **Quality gates.** `ruff` (lint and format), `mypy --strict` on `efp/`, `pytest` with coverage; `pre-commit` running ruff and a secret scanner (gitleaks or detect-secrets) over every commit; GitHub Actions workflow running `uv sync`, `ruff check`, `mypy`, `pytest -m "not live"` on every push. A `live` pytest marker exists and CI never runs it.
6. **Logging.** Structured logs (JSON lines) to `~/efp-data/logs/efp.log` plus a readable console stream; log level from `config.toml`. Every log record carries `row_id` and `stage` when known.
7. **Docs.** `README.md` already describes the kit; add a short "Developing" section: `uv sync`, `uv run pytest`, `uv run efp selftest`. Every shell snippet, hook and script in the repository is bash (`#!/usr/bin/env bash`); the operator does not use zsh.

## Tests

- Config loader: valid file loads; unknown key fails; missing env file fails with the path in the message; env file with mode 644 is refused.
- CLI: every command is registered and `--help` works; `selftest` runs with no data directory present and says what is missing.
- Secret hygiene: a test that greps the log output and any exception text produced during the suite for every secret value injected by the fixtures; it must find none.

## Done when

- `uv sync && uv run efp --version && uv run efp selftest` work on a fresh clone with no data directory.
- `uv run ruff check . && uv run mypy efp && uv run pytest` pass; CI is green on the PR.
- `pre-commit run --all-files` passes and the secret scanner is active.
- No file under the repository references `~/efp-data` or `~/.config/efp` except `config.example.toml`, `config.toml`, `RUNBOOK.md` and `CLAUDE.md`.

## Guardrails that bite here

Secrets only in the env file; nothing under `~/efp-data/` or `~/.config/efp/` is ever referenced by a path inside the repository except through `config.toml`. Test fixtures are synthetic. Do not download reference data or hit any network service in this build.

Finish with `reports/01-scaffold.md` from `reports/TEMPLATE.md` and a pull request. Do not merge.

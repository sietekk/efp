# Build 01 — Scaffold and toolchain

Branch: `build/01-scaffold` · PR: this PR · Date: 2026-11-21 · Model: claude-opus-5-5 (plan), claude-sonnet-5-5 (build) · Prompt: `build/01-scaffold.md` · Spec sections: 10, 11

## What this build set out to do

Lay the foundation for every later build: the `efp` package on Python 3.12 under `uv` with the spec's dependency floors; the layout from spec section 11; a strict Pydantic configuration loader and a secrets loader that refuses a file that is not mode 600; a Typer CLI with every spec section 10 command registered (all stubs except `selftest`); structured logging; and the quality gates (ruff, mypy `--strict`, pytest, pre-commit with a secret scanner, a GitHub Actions workflow). No data is touched and no network call is made.

Changes from the approved plan: `auto-archiver` moved to an `archive-docker` extra and two extra pairs are declared conflicting (decision 4); the review below added fixes that the plan did not list (secret-leak fix, audit columns, exit codes, root-logger redaction, rate ceilings).

## What was built

- **Package and environment.** `pyproject.toml`, `uv.lock` (462 packages). Core groups (core, http, archive, court, identity, news) are always installed; `documents`, `ocr-fallback`, `social`, `extraction`, `validation` and `archive-docker` are extras. `hishel` is not declared (decision 9).
- **Layout.** `efp/` with `cli.py`, `config.py`, `secrets.py`, `logging.py`, `audit.py`, `selftest.py`, `types.py`, `version.py`, placeholder `db.py`, `runner.py`, `capture.py`, the ten component subpackages (`run` and `gate` stubs raising `NotImplementedError` naming their build), `court/adapters/ag/` and `docket/`; `codebook/`, `reference/`, `migrations/`, `allowlist.json` (`{"version": 1, "platforms": []}`), `tests/{fixtures,cassettes}`. <!-- pragma: allowlist secret -->
- **Configuration.** Every key in `config.example.toml` is a field; unknown keys are an error; paths have no defaults. A test asserts the schema and `config.example.toml` have the same keys, so a later build that adds a key without documenting it fails CI.
- **Secrets.** 13 names; `LLM_API_KEY` accepted as an alias for `ANTHROPIC_API_KEY`; never written to `os.environ`; held as `SecretStr`; redacted from log output as a second line of defence.
- **CLI.** All ten commands registered. `selftest` reports versions, config validity, the data directory and each `paths.*` directory, env file mode, secret names present (never values), extras installed, IRB flag, Docker. It never creates a directory. `--strict` exits 1 if a required item is missing.
- **Logging.** JSON lines to `paths.logs/efp.log` (only if that directory exists) plus a console stream; `row_id` and `stage` via `efp.logging.bind`.
- **Quality gates.** `.pre-commit-config.yaml` (ruff check, ruff format, detect-secrets with an empty baseline, a bash path-check script); `.github/workflows/ci.yml`; `live` marker.
- **Config changes** (`config.toml` and `config.example.toml`, both): `[logging].level` added and documented; `user_agent` closing parenthesis added (`config.toml` only); `llm.model` and `transcription_model` pinned to `claude-sonnet-5-5`; FEC rate 0.25 to 0.22 and GDELT 0.2 to 0.16 (decision 7).
- **Results (local, identical to the CI steps):** `uv run ruff check .` pass; `uv run ruff format --check .` pass; `uv run mypy efp` pass (26 files); `uv run pytest -m "not live"` **111 passed**, 94% coverage.

## What was run

Not applicable: no pipeline stage exists yet.

| Run | Rows attempted | Rows resolved | Wall clock | Requests (per domain) | API spend |
| --- | --- | --- | --- | --- | --- |
| 50-row sample | n/a | n/a | n/a | 0 | $0 |
| All rows | n/a | n/a | n/a | 0 | $0 |

## Results by source

n/a. No source was contacted.

## Failures by reason

n/a.

## Gated sources encountered (not worked around)

None. (Separately, GitHub Actions is blocked by an account billing lock: see decision 1.)

## Numbers for the first-pass report

None from this build.

## Spot-check guide for the operator

- `uv run efp selftest` on the real machine; check that the env file shows mode 0600 and the secrets you expect are listed by name.
- `uv run efp run court` should print `not built yet (build 04)` and exit 2.
- Read `efp/secrets.py` and `tests/test_zz_secret_hygiene.py`: they are the guardrail for credentials.

## Decisions needed from the operator

Numbered. Each with the default the code currently takes.

1. **CI did not run.** The push triggered the workflow, which failed in 2 seconds with "The job was not started because your account is locked due to a billing issue." I cannot fix or work around that. Default: I ran the same steps locally (above), but "CI is green on the PR" is **not demonstrated**. Please resolve the billing lock and re-run the workflow.
2. **A `.env` file sits in the repository root, mode 0644.** It is gitignored and untracked, and I did not read it. CLAUDE.md puts secrets only in the file named by `paths.env_file`, mode 600, and no code loads the repo-root copy. Default: untouched. Please move it there, `chmod 600`, and delete the copy. `efp selftest` currently reports 0 of 13 secrets present in the configured env file.
3. **The "no file references the data or secrets paths" Done-when line fails as written** for files that already existed or were not mine to change: `README.md`, `.gitignore`, `.env.example`, `BUILD.txt`, `.claude/settings.json`, `.claude/agents/done-when-reviewer.md`, `docs/{plan,spec,tasks}.md`, and the build prompts under `build/` (which became tracked when decision 11 un-ignored them). Default: the rule is applied to everything this build wrote (`efp/`, `tests/`, `scripts/`, `.github/`, `pyproject.toml`, `.pre-commit-config.yaml`), enforced by `tests/test_layout.py::test_no_home_paths_in_code_tests_or_workflows` and a pre-commit hook; those files are clean. Please amend the line or approve the exceptions.
4. **Dependency conflicts.** `uv lock` could not resolve the spec's floors together. (a) `naminter` pins `rich==15.0.0` and `instructor` needs `rich<15`, so the `social` and `extraction` extras cannot share an environment. (b) `auto-archiver` (through `seleniumbase`) clashed with `paddleocr` and the others; it moved to an `archive-docker` extra (the spec runs it through Docker anyway), and `ocr-fallback` and `archive-docker` are declared conflicting. No floor was lowered or dropped. Consequence: stages 15 (social) and 16 (extract) cannot run from one environment. Default: run them in separate invocations, `uv run --extra social ...` and `uv run --extra extraction ...`. Build 04 or 15 should decide how the runner invokes them.
5. **`[irb]` holds placeholders.** `cleared = true`, but `reference = "test"`, `scope_notes = "test"`, and `date = "2026-10-10"` (CLAUDE.md says the determination was recorded on November 21, 2026). The social gate will open on these values. Default: untouched; they are the operator's to enter.
6. **Model id.** `claude-sonnet-5-5` is pinned in `config.toml` per the index's instruction to pin it in build 01. It could not be checked against the API (no network in this build). First real use is build 10 or 16.
7. **Rate limits.** Two values predated the 80% rule: FEC (0.25/s against a 0.278/s published limit, 80% is 0.222) and GDELT (0.2/s equals 100% of one request per 5 s). Default: lowered to 0.22 and 0.16, in the direction that fetches less. Added a validator that `default_rps` is at most 0.5 (1 request per 2 s, spec 10). The plan's wording (1 req/s) differs from the spec (1 req/2 s); CLAUDE.md says the spec's, which is what is used.
8. **Audit log contract.** The hook writes one row at exit using build 02's column names (`invoked_at`, `finished_at`, `command`, `args_json`, `git_sha`, `lockfile_sha256`, `config_sha256`, `exit_code`), skipping silently while the table does not exist. `build/02-schema.md` item 6 says to write at start and update at exit, but item 4 says triggers reject UPDATE on `audit_log`; build 02 must resolve that. `--version`, `--help` and a bare `efp` write no audit row. Argument redaction is build 02's.
9. **`hishel`** (spec 11, "if httpx is used anywhere"): httpx arrives only transitively through `courtlistener-api-client` and `juriscraper`; efp code does not use it, so `hishel` is not declared. Revisit if the capture layer (build 05) uses httpx.
10. **Transitive risk for build 15.** The `social` extra (and `yt-dlp` through `maigret`) pulls in `curl-cffi` (browser TLS impersonation), and `archive-docker` pulls in `seleniumbase`. Neither is on the excluded list, but build 15 must not turn impersonation on: CLAUDE.md forbids evading bot checks.
11. **`build/` was gitignored.** `.gitignore` had a Python `build/` rule that hid all 21 build prompts and the index. I removed that line and committed them (first commit on this branch). `dist/` is still ignored.
12. **Excluded-tool list.** The spec, the prompt and the tool register disagree. I excluded the union and a test checks `uv.lock`: newspaper3k, snscrape, twscrape, waybackpy, abydos, dedupe, recordlinkage, Name Match, court-scraper, Nougat, PyMuPDF, Blackbird, SpiderFoot, Recon-ng, OCCRP Aleph. Only `krippendorff` (GPL, numbers only) and `nameparser` (LGPL, used as a dependency) are in the lock as known licence notes.
13. **Stubs.** The prompt explicitly requires `NotImplementedError` stubs for each component's `run` and `gate` and "not built yet" CLI commands; CLAUDE.md's "no stubs" rule is about operator steps, so I followed the prompt. Each later build replaces its own.
14. **macOS hidden `.pth` files.** In this checkout (under `~/Documents`) the editable-install `.pth` files in `.venv` carried the macOS `hidden` flag, so Python skipped them and `uv run efp` failed with `ModuleNotFoundError` while `uv run pytest` still passed (pytest is given `pythonpath = ["."]`). The cause is not established; a fresh clone under `/tmp` was unaffected. Fix: `chflags nohidden .venv/lib/python3.12/site-packages/*.pth`, now in the README. Keeping the repository out of iCloud-synced folders may avoid it.
15. **Minor.** `.env.example` says "two aliases are noted" but only one (`LLM_API_KEY`) is defined; left as is. `config.toml` is the operator's file, so every edit to it is listed above.

## Done-when check

1. *"`uv sync && uv run efp --version && uv run efp selftest` work on a fresh clone with no data directory."* **Holds.** Fresh clone of the branch at `018ac6f` into a scratch directory: `uv sync --locked` succeeded; `efp --version` printed `efp 0.1.0 (git 018ac6f)`; `efp selftest` exited 0. This machine has a real data directory, so I also ran selftest from the clone with a config pointing at a nonexistent one: it printed `[MISSING] data directory` and `[MISSING] env file`, exited 0, and the directory was not created. `tests/test_selftest.py::test_selftest_with_no_data_dir_says_what_is_missing` asserts the same.
2. *"`uv run ruff check . && uv run mypy efp && uv run pytest` pass; CI is green on the PR."* **Partly.** The three pass locally (111 tests). **CI is not green: it never ran** (decision 1).
3. *"`pre-commit run --all-files` passes and the secret scanner is active."* **Holds.** All four hooks pass. The scanner is demonstrated active: `detect-secrets-hook` on a file holding a fake AWS key exits 1 and names the finding. The baseline is empty (synthetic test sentinels carry `pragma: allowlist secret`).
4. *"No file under the repository references `~/efp-data` or `~/.config/efp` except `config.example.toml`, `config.toml`, `RUNBOOK.md` and `CLAUDE.md`."* **Fails as written; holds for this build's own files** (decision 3).

Independent review: `done-when-reviewer` graded the build and returned "not ready to report" with 13 findings. Acted on: the bare-pasted-key leak (HIGH, reproduced first; fixed with regression tests), audit column drift, exit codes for usage errors (Typer vendors its own Click), root-logger redaction, user-agent must contain the contact, rate ceilings, `row_id` and status types aligned to the spec, and the undocumented conflict reasons. Left to the operator or recorded above: decisions 2, 3, 5, 9, 10, 15. I did not re-run the reviewer after the fixes; the fixes are covered by new tests instead.

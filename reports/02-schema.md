# Build 02 — Database schema and migrations

Branch: `build/02-schema` · PR: this PR · Date: 2026-11-21 · Model: claude-opus-5-5 (plan), claude-sonnet-5-5 (build) · Prompt: `build/02-schema.md` · Spec sections: 3, 7, 11 (and 2 for the stage list)

**Read first (not about the schema):** the repository is inside an iCloud-synced folder (`FXICloudDriveDesktop = 1`), so a `.env` file sitting in the repository root has been synced to iCloud. I never opened it. If it holds real keys, move it to the configured env file, `chmod 600`, and consider rotating them. See decision 14.

## What this build set out to do

Make the database real: the spec section 3 schema as numbered migrations, append-only enforcement by trigger, the content-addressed store path, the audit-log hook, `efp selftest --db`, and a read-only DuckDB helper. This is a review checkpoint: you read the schema (`migrations/0001_core.sql`, `0002_supporting.sql`, `0003_triggers.sql`).

Changes from the approved plan: raw-connection protection was moved into the schema itself (decision 7); the migration runner owns its own transaction (decision 8); and the independent review added the fixes listed at the end.

## What was built

- **Migrations** (`migrations/`): `0001_core.sql` (the eight core tables, spec section 3 verbatim, plus 12 indexes), `0002_supporting.sql` (eight supporting tables, 12 indexes), `0003_triggers.sql` (12 triggers), `README.md`. `schema_migrations(id, name, sha256, applied_at)` is created by `efp/db.py` before the first migration.
- **`efp/db.py`:** `connect()`, `migrate()`. WAL, `foreign_keys`, `recursive_triggers`, `busy_timeout` from config, SQLite defensive mode. Refuses a database inside the repository or outside `paths.data_dir`; creates the parent directory mode 700 and a new database file mode 600; a read-only open never creates anything. Each migration runs as individual statements inside one `BEGIN IMMEDIATE` transaction together with its `schema_migrations` row; applied state is re-read under the lock; `BEGIN/COMMIT/END/ROLLBACK/SAVEPOINT` and unterminated statements in a migration file are rejected; an applied file that changed or disappeared is an error.
- **`efp/store.py`:** `store_path`, `body_relpath`, `resolve_body_path`, `write_body` (atomic, never overwrites, verifies what is already there), `read_verified`.
- **`efp/audit.py`, `efp/redact.py`:** an `audit_log` row is written when the invocation starts and completed at exit. Arguments are redacted first (known secret values, secret-named flags and assignments, key shapes, URL credentials, secret query parameters, `Authorization`/`X-Api-Key`/`Bearer` values, `-p <value>`).
- **`efp/dbcheck.py`, CLI:** `efp selftest --db` (12 checks), new `efp db migrate`.
- **`efp/analysis.py`:** `open_readonly()` attaches the database to DuckDB read-only, with extension auto-install and auto-load switched off.
- **Config:** new `[database]` section (`busy_timeout_ms = 5000`, `selftest_capture_sample = 200`), documented in `config.example.toml`.
- **Results:** `uv run ruff check .` pass; `ruff format --check .` pass; `uv run mypy efp` pass (30 files); `uv run pytest -m "not live"` **274 passed**, 95% coverage; `pre-commit run --all-files` pass.

## What was run

No pipeline stage exists yet. I ran `efp db migrate` and `efp selftest --db` against scratch databases in a scratch directory, from a fresh clone of the branch. No data was loaded, no network request was made, and no real data directory was touched.

| Run | Rows attempted | Rows resolved | Wall clock | Requests (per domain) | API spend |
| --- | --- | --- | --- | --- | --- |
| 50-row sample | n/a | n/a | n/a | 0 | $0 |
| All rows | n/a | n/a | n/a | 0 | $0 |

## Results by source

n/a.

## Failures by reason

n/a.

## Gated sources encountered (not worked around)

None.

## Numbers for the first-pass report

None from this build.

## Spot-check guide for the operator

You are reviewing the schema. Suggested order:

1. Read `migrations/0001_core.sql` next to `docs/spec.md` section 3. `tests/test_db_schema.py::test_core_ddl_matches_spec_section_3` already compares them from the spec file itself (normalised DDL, `table_info` and foreign keys for all eight tables), and the only additions are the `CREATE INDEX` lines at the bottom.
2. Read `migrations/0002_supporting.sql`. The spec gives these tables as prose, so every column and enum is my reading of `build/02-schema.md`. The `NOT NULL` additions are listed in decision 9.
3. Read `migrations/0003_triggers.sql` and the "What is and is not protected" table in `migrations/README.md`.
4. Try it on a scratch config: `efp db migrate`, then `efp selftest --db`, then open the file with any SQLite client and try an `UPDATE`, `DELETE` or `REPLACE` on `rows`.

## Decisions needed from the operator

Numbered. Each with the default the code currently takes.

1. **`audit_log` append-only versus "update at exit".** The spec and prompt item 4 say append-only; item 6 says write at start and update at exit. Default: a trigger allows exactly one UPDATE per row, filling `finished_at` and `exit_code` from NULL with every other column unchanged; everything else is rejected. A run that crashes leaves `finished_at` NULL. Alternative: one insert at exit (build 01's behaviour), which loses crashed runs.
2. **`state_adapters`.** The spec's prose says a `status` column with the value `semi`; the prompt says `mode` with `assisted`. Default: the prompt's (`mode`, `assisted`), primary key `(state, kind)`.
3. **New command `efp db migrate`** (not in spec section 10), so a database can be created and checked before ingest exists. Default: added. Remove it if you prefer ingest to be the only creator.
4. **`captures.body_path`** is stored in the spec's relative form `store/<aa>/<sha256>` and resolved against `paths.store`. The prompt says `store_path(sha256) -> Path`; the pure function takes the store root as a second argument (`store_path(sha256, store_root)`), with `config_store_path(sha256, config)` as the one-argument convenience.
5. **Row-hash check.** The spec lists "row hashes" among the data tests, but the hashing recipe belongs to build 03. `selftest --db` checks only that `row_sha256` is 64 hex characters; recomputation is for build 03.
6. **`ref_*` tables** are not created (the prompt reserves the names for build 11; plan S0 lists them). Default: the prompt's. The names are in `migrations/README.md`.
7. **`0003_triggers.sql` was edited in place** after the first draft, to add four `BEFORE INSERT` triggers. It had not been applied to any real database (none exists), so no recorded hash was invalidated. From now on migrations are append-only: a change is a new numbered file. The reason: `REPLACE` only fires DELETE triggers when `PRAGMA recursive_triggers` is on, and it is off by default in any non-efp connection, so a plain `sqlite3` shell could overwrite a protected row (the reviewer reproduced it). The insert triggers close that without depending on a pragma.
8. **Migration runner owns the transaction.** My first version wrapped each file in `executescript`, which is only atomic if the file contains no transaction control; the reviewer showed a `COMMIT;` inside a file left tables created but unrecorded. Now statements run one by one inside my own transaction, and a migration containing `BEGIN`/`COMMIT`/`END`/`ROLLBACK`/`SAVEPOINT` is rejected before anything runs.
9. **Columns I made `NOT NULL` that the prompt does not say are**: `links.url_raw`, `links.status`, `court_records.source_kind`/`found_at`, `articles.source_kind`, `social_accounts.platform`/`handle`, `gold_set.field`/`split`/`set_version`/`set_sha256`, `state_adapters.kind`/`mode`, `audit_log.command`/`args_json`/`invoked_at`. Also `documents.ocr_applied` is `NOT NULL DEFAULT 0 CHECK (ocr_applied IN (0, 1))`. `links.expansion_method` is nullable (not yet expanded). Every other supporting column is nullable.
10. **What a raw connection can still do.** UPDATE, DELETE and REPLACE on `rows`, `captures`, `decisions`, `audit_log` are refused by triggers stored in the database file, whatever connects. Schema statements are not: `DROP TRIGGER`, `ALTER TABLE ... DROP COLUMN`, `DROP TABLE`, `PRAGMA writable_schema` from a raw SQLite client remove the protection, and SQLite cannot forbid that. Mitigations: database outside the repository under the data directory (mode 600, directory 700), `selftest --db` verifies each trigger exists and fires, a static test keeps such statements out of `efp/` and of later migrations, and the weekly manifest (build 20) is what makes silent tampering with `captures` detectable. `migrations/README.md` says this plainly.
11. **DuckDB's `sqlite` extension is never auto-installed.** `LOAD sqlite` would otherwise download it (a network fetch outside the capture layer), so auto-install and auto-load are off. If the extension is missing, `open_readonly` raises with the one-line manual `INSTALL sqlite` instruction. It loads on this machine already.
12. **Audit gaps.** `--version`, `--help` and a bare `efp` write no row (they exit before the hook). A row inserted by someone with write access can already be complete; the trigger cannot distinguish that from a hook-written row. The invocation that creates the database is recorded (its row is written after the database exists). `git_sha` is now the full 40-character SHA (`--version` still prints the short one).
13. **`selftest --db` sampling is deterministic.** Above `selftest_capture_sample` (200) captures it hashes a sample drawn with `scope.sample_seed`, so the same captures are checked each run. Raise the config value to check more. It reports "sample of N of M" so the scope is visible.
14. **The repository is iCloud-synced, which has two consequences.** (a) `.venv` files get macOS's `hidden` flag, within seconds of being cleared, even with no `uv` running; Python then skips the editable-install `.pth`, `uv run efp` fails with `ModuleNotFoundError`, and tests still pass (pytest has `pythonpath = ["."]`). It looks like the sync client flagging dot-directory contents; I have not proven that. Likely cure: keep the virtual environment outside synced folders (`UV_PROJECT_ENVIRONMENT=<path outside Documents>`) or move the checkout. The `README 2.md`, `config 2.toml`, `config.example 2.toml` files in the working tree look like iCloud conflict copies; I left them alone and they are untracked. (b) A `.env` in the repository root is synced too (see the top of this report). The database itself is not at risk: it lives under the data directory, outside this folder, as long as `paths.data_dir` stays outside iCloud-synced folders. **Resolved after build 03:** the repository moved to `~/dev/efp`, outside iCloud, and the virtual environment no longer sits in a synced folder; see the README "Developing" section.
15. **CI.** The repository now has `.circleci/config.yml` (yours). I did not change it and have not seen a CI result for this branch; the PR page will show it. The same steps pass locally.
16. **Migrations are found from the repository checkout** (editable install). A wheel install would not include `migrations/`; fine for this single-operator tool, noted for build 20.
17. **Reviewer items not changed:** the pragma-level checks that merely read back what `connect()` set were replaced by the behavioural probes (done); the deterministic sample (decision 13) is by design.

## Done-when check

1. *"`uv run efp selftest --db` passes on a fresh database and reports every check it ran."* **Holds.** From a fresh clone of the branch with a scratch config: `efp db migrate` printed `applied: 0001_core, 0002_supporting, 0003_triggers`; `efp selftest --db` listed 12 checks (database file, journal mode WAL, migrations match files, tables present, indexes present, triggers present, append-only triggers fire (15 of 15 write attempts behaved as required, rolled back), capture bodies exist and hash, no orphan captures, foreign keys hold, `row_sha256` well-formed, SQLite quick_check) and ended `12 check(s) run, 0 failed`. With no database it is one failing check and nothing is created (`tests/test_dbcheck.py::test_cli_selftest_db_exits_1_when_the_database_is_missing`). Each check has a test that makes it fail: tampered, missing and mis-named bodies, an orphan capture, a foreign-key violation, a malformed hash, a changed migration file, an unapplied migration, a dropped trigger, a neutered trigger, a corrupt file.
2. *"The DDL in `migrations/0001_core.sql` matches spec section 3 column for column; any deliberate difference is listed in the report with the reason."* **Holds, with no difference in any table.** `tests/test_db_schema.py::test_core_ddl_matches_spec_section_3[…]` (eight cases) extracts the SQL block from `docs/spec.md` and compares normalised `CREATE TABLE` text, `PRAGMA table_info` and `PRAGMA foreign_key_list` with the migration; `test_core_migration_adds_only_indexes_to_the_spec` asserts the only additions are indexes. The independent reviewer diffed the two by hand and found them identical, comments included.
3. *"No code path can write to `rows` after ingest or update `captures`, `decisions` or `audit_log`."* **Holds for code and for any connection, with the schema-change caveat in decision 10.** Triggers reject UPDATE, DELETE and REPLACE on all four tables, shown on a raw `sqlite3` connection with `recursive_triggers = 0` (`tests/test_db_triggers.py::test_a_raw_connection_cannot_replace_protected_rows`, and directly from the fresh clone: `replace into rows`, `insert or replace into rows`, `update rows`, `delete from rows` all refused, the row unchanged). `tests/test_db_guard.py` fails if anything under `efp/` (code only, docstrings and comments ignored) deletes from or updates those tables, inserts into `rows` outside `efp/ingest/`, uses REPLACE semantics, or drops or alters schema; I confirmed it fails on a deliberately offending file. The one allowed UPDATE is `audit.COMPLETE_SQL`, filling the completion fields once.

Independent review: `done-when-reviewer` graded the build and returned "not ready to report" with 11 findings. Fixed: the raw-connection REPLACE bypass (insert triggers), DuckDB auto-install, non-atomic migrations with transaction control and the unterminated-comment case, concurrent `migrate` (lock plus re-read, clear error), redaction of URL credentials, query secrets, headers and `-p`, an unescaped database URI, a database outside `paths.data_dir` and mode 0644, `write_body` trusting a dangling symlink or corrupt entry, a guard that skipped migrations, vacuous pragma checks, the short git SHA, and `db migrate` showing a traceback on a SQLite error. I did not re-run the reviewer after the fixes; they are covered by new tests (274 pass) and the fresh-clone run above.

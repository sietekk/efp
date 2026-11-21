# Build 02: Database schema and migrations

Spec: section 3 (data model, in full), section 7 (content-addressed store path, pinned instruments), section 11 (data tests). Task list: D1. Needs: 01. Branch: `build/02-schema`. Report: `reports/02-schema.md`.

Read first: `CLAUDE.md`, `build/README.md`, `docs/spec.md` section 3 in full (the SQL is the contract), section 2 (the stage list and gates, which the `case_stage` CHECK constraints encode), `reports/01-scaffold.md`.

Stay in plan mode until I approve. Propose the migration mechanism, the trigger set, the supporting-table DDL (the spec gives those as prose), and the data-test list. Wait.

## What to build

1. **Migrations.** `migrations/0001_core.sql`, `0002_supporting.sql`, `0003_triggers.sql`, numbered and append-only; `efp/db.py` applies unapplied migrations in order inside a transaction and records each in `schema_migrations (id, name, sha256, applied_at)`. `PRAGMA journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout` set. The database path comes from `config.toml` (`paths.database`, default `~/efp-data/pipeline.db`); `db.py` creates the parent directory with mode 700 if needed and never creates a database inside the repository.
2. **Core tables, exactly as spec section 3 writes them:** `rows`, `case_stage`, `captures`, `identifiers`, `identity_candidates`, `decisions`, `extractions`, `codes`. Keep the column names, types, CHECK lists and comments; add indexes on every foreign key and on `case_stage(stage, status)`, `captures(content_sha256)`, `captures(row_id, stage)`, `identifiers(row_id, kind)`, `codes(column_name)`.
3. **Supporting tables** from the spec's table "Supporting tables, same conventions", with columns that follow from the description and from the components that write them:
   - `links(link_id, row_id, url_raw, url_expanded, expansion_method CHECK IN ('live','wayback_short','wayback_target','unresolved'), status CHECK IN ('live','archived','dead','unresolved'), capture_id, resolved_at)`
   - `court_records(record_id, row_id, source_kind CHECK IN ('doj_release','state_ag_release','da_release','federal_docket','state_docket','pin_report','other'), jurisdiction, court, case_number, document_kind, strategy, capture_id, found_at)` — one row per document; `strategy` names the court sub-strategy that found it (spec section 4) so coverage is reported per strategy.
   - `documents(document_id, row_id, record_id, capture_id, pre_ocr_sha256, post_ocr_sha256, ocr_applied INTEGER, ocr_engine, docling_path, page_count, text_sha256, quality_min REAL, quality_json, extracted_at)`
   - `articles(article_id, row_id, capture_id, outlet, published_date, date_method, extractor, extractor_version, source_kind CHECK IN ('automated','manual'), query_json, retrieved_at)`
   - `social_accounts(account_id, row_id, platform, handle, attribution_basis, allowlist_version, allowlist_sha256, capture_id, found_at)`
   - `gold_set(gold_id, row_id, field, coder_a, coder_b, consensus, span, split CHECK IN ('dev','heldout'), set_version, set_sha256)`
   - `state_adapters(state, kind CHECK IN ('ag','docket'), portal, adapter_module, mode CHECK IN ('automated','assisted','manual','none'), reason, last_verified)`
   - `audit_log(audit_id, invoked_at, finished_at, command, args_json, git_sha, lockfile_sha256, config_sha256, exit_code)`
   - `ref_*` tables are created by build 11's loaders, not here, but reserve the naming: `ref_dime`, `ref_fec_candidates`, `ref_ftm`, `ref_local`, `ref_congress`, `ref_state_leg`, `ref_surname_freq`, `ref_given_freq`, `ref_population`.
4. **Append-only enforcement by trigger.** `rows`: triggers reject UPDATE and DELETE. `captures`, `decisions`, `audit_log`: triggers reject UPDATE and DELETE (a reversal is a new `decisions` row). `codes` is rewritable only by the `code` stage; document that it is not append-only and why. `case_stage` is the one mutable work table.
5. **Store path convention.** A pure function `store_path(sha256) -> Path` returning `<paths.store>/<sha256[:2]>/<sha256>`, used by every writer; bodies never live in the database.
6. **Audit log hook.** The CLI callback from build 01 now writes an `audit_log` row at start (with `args_json`, the git SHA, SHA-256 of `uv.lock` and of `config.toml`) and updates `finished_at` and `exit_code` at exit. Arguments are redacted of anything that looks like a secret before they are stored.
7. **Data tests as a command:** `efp selftest --db` checks: every migration's recorded hash matches the file; triggers fire (attempted UPDATE on `rows` fails); every `captures.body_path` exists on disk and hashes to `content_sha256` (sampled when large); no orphan `captures` without a `rows` parent except those with `row_id IS NULL` and a `stage` of `reference` or `selftest`; foreign keys hold (`PRAGMA foreign_key_check`).
8. **Analysis access.** A helper `efp/analysis.py` that opens the database read-only through DuckDB (`INSTALL sqlite; ATTACH ... (TYPE sqlite, READ_ONLY)`), so notebooks never touch the write path. Document the Postgres type substitutions from the spec in `migrations/README.md`; do not implement them.

## Tests

- Fresh database: migrations apply; a second run is a no-op; a modified migration file is detected.
- Trigger tests for each append-only table; CHECK constraints for every enum (stage, status, identifier kind, decision kind, alignment status, evidence tier, expansion method, adapter mode).
- `store_path` and a round trip write/read/hash of a synthetic body.
- `audit_log` row written for a CLI invocation, with redaction verified.

## Done when

- `uv run efp selftest --db` passes on a fresh database and reports every check it ran.
- The DDL in `migrations/0001_core.sql` matches spec section 3 column for column; any deliberate difference is listed in the report with the reason.
- No code path can write to `rows` after ingest or update `captures`, `decisions` or `audit_log`.

## Guardrails that bite here

`rows` is immutable; captures, decisions and the audit log are append-only; the database lives only under `paths.data_dir`. No real subject data in fixtures.

Finish with `reports/02-schema.md` and a pull request. Do not merge. This is a review checkpoint: I read the schema. Build 03 stacks on this branch if I have not merged yet.

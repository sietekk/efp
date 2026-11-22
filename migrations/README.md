# migrations/

Numbered SQL files, applied in order by `efp db migrate` (and by `efp ingest` once it exists) through
`efp/db.py`. Each file runs in one transaction together with its row in `schema_migrations
(id, name, sha256, applied_at)`, so a failure leaves the database at the previous migration.

**Migrations are append-only.** The SHA-256 of every applied file is recorded, and a later edit is
refused (`MigrationError`). To change the schema add the next numbered file; never edit an applied file.

| File | Holds |
| --- | --- |
| `0001_core.sql` | The eight core tables, copied from `docs/spec.md` section 3, plus indexes (the only addition) |
| `0002_supporting.sql` | `links`, `court_records`, `documents`, `articles`, `social_accounts`, `gold_set`, `state_adapters`, `audit_log` |
| `0003_triggers.sql` | Append-only enforcement |
| `0004_source_workbook.sql` | `source_workbook`: the ingested workbook's hash, immutable |
| `0005_case_stage_summary.sql` | `case_stage.summary_json`: the runner's per-row outcome summary (module chain outcomes) |
| `0006_capture_support.sql` | Capture layer: `captures` may set `wayback_url` and `tsa_token_path` once from NULL (nothing else); `spn_queue`, `manifests`, `manifest_tokens`, `manifest_captures` |
| `0007_links_resolution.sql` | `links` rebuilt: `bitly_info` and `commoncrawl` methods, a `note` column, unique per row and URL |
| `0008_court_records_weak.sql` | `court_records.weak` and `basis_json`: a candidate match is marked, with its evidence |
| `0009_court_records_identity.sql` | `court_records.docket_id` and `current`: a record is identified by its document, and one a later run no longer finds is marked not current |
| `0010_court_records_views.sql` | views `court_records_current` (current = 1) and `court_records_confirmed` (current = 1 and weak = 0): later builds read these, not the table |
| `0011_court_identifiers_current.sql` | `court_records.text_capture_id` and the view `court_identifiers_current`: identifiers read from a record that is still current (person-level kinds only from one that is not weak) |

`schema_migrations` itself is created by `efp/db.py` before the first migration runs.

## Append-only enforcement

| Table | Rule |
| --- | --- |
| `rows` | UPDATE and DELETE rejected. Written once by ingest. |
| `captures` | DELETE rejected. UPDATE rejected except two one-shot writes (migration 0006): `wayback_url` and `tsa_token_path` may go from NULL to a value, once, with every other column unchanged. Every retrieval, forever. |
| `decisions` | UPDATE and DELETE rejected. A reversal is a new row. |
| `audit_log` | DELETE rejected. UPDATE rejected except one completion write per row: `finished_at` and `exit_code` go from NULL to a value, every other column unchanged. A run that crashed leaves `finished_at` NULL. |
| `case_stage` | The one mutable work table. |
| `codes` | **Not** append-only (see below). |

Each protected table also has a `BEFORE INSERT` trigger that refuses an insert whose key already
exists. That is what `REPLACE` / `INSERT OR REPLACE` / `ON CONFLICT DO UPDATE` do after removing the old
row, and SQLite fires the DELETE trigger for that only when `PRAGMA recursive_triggers` is ON, which is
**off by default** in a plain `sqlite3` shell or any non-efp connection. The insert triggers do not
depend on that setting. `efp.db.connect()` also turns `recursive_triggers` ON as a second layer.

### What is and is not protected (read this)

| Actor | Can it change or delete a protected row? |
| --- | --- |
| Code using `efp.db.connect()` | No: UPDATE, DELETE and REPLACE are rejected by triggers. |
| A raw connection (`sqlite3` shell, a notebook, another library) | No for UPDATE, DELETE and REPLACE: the triggers live in the database file, not in the connection. |
| A raw connection issuing **schema** statements | **Yes.** `DROP TRIGGER`, `ALTER TABLE ... DROP COLUMN`, `DROP TABLE` or `PRAGMA writable_schema` can remove the protection, and SQLite has no way to forbid that. |

So the protection is strong against mistakes and against code, and weak against a person who opens
the file with a SQLite client and drops a trigger. The mitigations are: the database lives outside the
repository under the data directory (mode 0600, directory 0700); `efp selftest --db` verifies every
trigger exists *and fires*; `tests/test_db_guard.py` fails if any code under `efp/`, or any later
migration, drops a trigger or alters a protected table; and the weekly hash manifest (build 20)
records `captures`, which is what makes silent tampering detectable. Analysis goes through
`efp/analysis.py`, which attaches the file read-only.

### Why `codes` is not append-only

`codes` holds the final coded value per (row, column). It is derived state: the `code` stage
rewrites it when a codebook version changes (`efp run code --reset`), from `extractions` (append-only
in practice, new rows only) and `decisions`. Anything a human touched is recorded as a new `decisions`
row, which is append-only, so no history is lost. Only the `code` stage writes to it.

## Reserved names

Created by build 11's loaders, not here: `ref_dime`, `ref_fec_candidates`, `ref_ftm`, `ref_local`,
`ref_congress`, `ref_state_leg`, `ref_surname_freq`, `ref_given_freq`, `ref_population`.

## Postgres path (documented, not implemented)

The schema is ANSI enough to move with type substitutions only (spec section 3): `TEXT` timestamps
become `TIMESTAMPTZ`, `*_json` columns become `JSONB`, `INTEGER PRIMARY KEY` becomes `BIGSERIAL`. The
triggers would be rewritten as PL/pgSQL, and `INSERT OR REPLACE` has no equivalent. Move when a second
operator joins or concurrent stage runners are wanted; not before. The content store stays on disk.
DuckDB attaches the SQLite file read-only for analysis (`efp/analysis.py`).

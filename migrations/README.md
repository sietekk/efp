# migrations/

Numbered SQL files, applied in order by `efp db migrate` (and by `efp ingest` once it exists) through
`efp/db.py`. Each file runs in one transaction together with its row in `schema_migrations
(id, name, sha256, applied_at)`, so a failure leaves the database at the previous migration.

**Migrations are append-only.** The SHA-256 of every applied file is recorded, and a later edit is
refused (`MigrationError`). To change the schema add `0004_*.sql`; never edit an applied file.

| File | Holds |
| --- | --- |
| `0001_core.sql` | The eight core tables, copied from `docs/spec.md` section 3, plus indexes (the only addition) |
| `0002_supporting.sql` | `links`, `court_records`, `documents`, `articles`, `social_accounts`, `gold_set`, `state_adapters`, `audit_log` |
| `0003_triggers.sql` | Append-only enforcement |

`schema_migrations` itself is created by `efp/db.py` before the first migration runs.

## Append-only enforcement

| Table | Rule |
| --- | --- |
| `rows` | UPDATE and DELETE rejected. Written once by ingest. |
| `captures` | UPDATE and DELETE rejected. Every retrieval, forever. |
| `decisions` | UPDATE and DELETE rejected. A reversal is a new row. |
| `audit_log` | DELETE rejected. UPDATE rejected except one completion write per row: `finished_at` and `exit_code` go from NULL to a value, every other column unchanged. A run that crashed leaves `finished_at` NULL. |
| `case_stage` | The one mutable work table. |
| `codes` | **Not** append-only (see below). |

`INSERT OR REPLACE` deletes the conflicting row, and SQLite fires DELETE triggers for that only when
`PRAGMA recursive_triggers` is ON. `efp.db.connect()` always turns it on, and `efp selftest --db`
checks that a REPLACE against `rows` is refused. SQLite cannot stop `DROP TRIGGER` or `DROP TABLE`
from a raw connection; `tests/test_db_guard.py` fails if code under `efp/` could issue one, and the
database lives outside the repository under the data directory.

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

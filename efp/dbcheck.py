"""``efp selftest --db``: data checks on the pipeline database. Reads only.

The one write-shaped check (do the append-only triggers fire?) runs inside a transaction that is
always rolled back, so nothing is left behind. The database is never created here.
"""

from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from efp.config import Config
from efp.db import (
    DatabaseError,
    MigrationError,
    connect,
    discover_migrations,
    migration_status,
)
from efp.store import StoreError, resolve_body_path, sha256_hex

EXPECTED_TABLES: tuple[str, ...] = (
    "schema_migrations",
    # core (spec section 3)
    "rows",
    "case_stage",
    "captures",
    "identifiers",
    "identity_candidates",
    "decisions",
    "extractions",
    "codes",
    # supporting
    "links",
    "court_records",
    "documents",
    "articles",
    "social_accounts",
    "gold_set",
    "state_adapters",
    "audit_log",
    "source_workbook",
    # capture layer (build 05)
    "spn_queue",
    "manifests",
    "manifest_tokens",
    "manifest_captures",
)

EXPECTED_INDEXES: tuple[str, ...] = (
    "idx_case_stage_stage_status",
    "idx_captures_content_sha256",
    "idx_captures_row_stage",
    "idx_identifiers_row_kind",
    "idx_identifiers_capture",
    "idx_identity_candidates_row",
    "idx_decisions_row",
    "idx_extractions_row",
    "idx_extractions_capture",
    "idx_codes_column_name",
    "idx_codes_extraction",
    "idx_codes_decision",
    "idx_links_row",
    "idx_links_capture",
    "idx_court_records_row",
    "idx_court_records_capture",
    "idx_documents_row",
    "idx_documents_record",
    "idx_documents_capture",
    "idx_articles_row",
    "idx_articles_capture",
    "idx_social_accounts_row",
    "idx_social_accounts_capture",
    "idx_gold_set_row",
    "idx_spn_queue_status",
    "idx_manifest_captures_capture",
)

EXPECTED_TRIGGERS: tuple[str, ...] = (
    "rows_no_update",
    "rows_no_delete",
    "captures_no_update",
    "captures_no_delete",
    "decisions_no_update",
    "decisions_no_delete",
    "audit_log_no_delete",
    "audit_log_complete_once",
    "rows_no_replace",
    "captures_no_replace",
    "decisions_no_replace",
    "audit_log_no_replace",
    "source_workbook_no_update",
    "source_workbook_no_delete",
    "source_workbook_no_replace",
)

# Orphan captures (no rows parent) are allowed only for these stages.
ORPHAN_OK_STAGES: tuple[str, ...] = ("reference", "selftest")


@dataclass(frozen=True)
class DbCheck:
    name: str
    ok: bool
    detail: str


def _names(conn: sqlite3.Connection, kind: str) -> set[str]:
    return {
        str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,))
    }


def _check_migrations(conn: sqlite3.Connection) -> list[DbCheck]:
    try:
        files = discover_migrations()
    except MigrationError as exc:
        return [DbCheck("migrations match files", False, str(exc))]
    status = migration_status(conn, files)
    problems: list[str] = []
    if status.changed:
        problems.append(f"changed after apply: {', '.join(status.changed)}")
    if status.missing:
        problems.append(f"recorded but no file: {', '.join(status.missing)}")
    if status.pending:
        problems.append(
            f"unapplied: {', '.join(m.path.name for m in status.pending)} (run `efp db migrate`)"
        )
    if problems:
        return [DbCheck("migrations match files", False, "; ".join(problems))]
    return [DbCheck("migrations match files", True, f"{len(status.applied)} applied, hashes match")]


def _try_write(conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> bool:
    """True if the statement was refused by an append-only trigger."""
    try:
        conn.execute(sql, params)
    except sqlite3.Error as exc:
        return "append-only" in str(exc)
    return False


def _check_triggers_fire(conn: sqlite3.Connection) -> DbCheck:
    stamp = datetime.now(UTC).isoformat()
    zero = "0" * 64
    attempts: list[tuple[str, str, tuple[object, ...]]] = [
        ("rows UPDATE", "UPDATE rows SET name = 'x' WHERE row_id = -1", ()),
        ("rows DELETE", "DELETE FROM rows WHERE row_id = -1", ()),
        (
            "rows INSERT OR REPLACE",
            "INSERT OR REPLACE INTO rows (row_id, year, state, name, in_scope, row_sha256, "
            "ingested_at) VALUES (-1, 2001, 'XX', 'replaced', 1, ?, ?)",
            (zero, stamp),
        ),
        (
            "rows REPLACE INTO",
            "REPLACE INTO rows (row_id, year, state, name, in_scope, row_sha256, ingested_at) "
            "VALUES (-1, 2001, 'XX', 'replaced', 1, ?, ?)",
            (zero, stamp),
        ),
        ("captures UPDATE", "UPDATE captures SET stage = 'x' WHERE capture_id = -1", ()),
        (
            "captures INSERT OR REPLACE",
            "INSERT OR REPLACE INTO captures (capture_id, row_id, stage, url_requested, "
            "retrieved_at, content_sha256, body_path, tool, tool_version) "
            "VALUES (-1, -1, 'x', 'u', ?, ?, 'store/00/x', 't', '0')",
            (stamp, zero),
        ),
        ("captures DELETE", "DELETE FROM captures WHERE capture_id = -1", ()),
        ("decisions UPDATE", "UPDATE decisions SET outcome = 'x' WHERE decision_id = -1", ()),
        ("decisions DELETE", "DELETE FROM decisions WHERE decision_id = -1", ()),
        (
            "decisions INSERT OR REPLACE",
            "INSERT OR REPLACE INTO decisions (decision_id, row_id, kind, outcome, decided_by, "
            "decided_at) VALUES (-1, -1, 'review', 'flipped', 'XX', ?)",
            (stamp,),
        ),
        ("audit_log DELETE", "DELETE FROM audit_log WHERE audit_id = -1", ()),
        (
            "audit_log INSERT OR REPLACE",
            "INSERT OR REPLACE INTO audit_log (audit_id, invoked_at, command, args_json) "
            "VALUES (-1, ?, 'x', '[]')",
            (stamp,),
        ),
        (
            "audit_log UPDATE of args",
            "UPDATE audit_log SET args_json = '[]' WHERE audit_id = -1",
            (),
        ),
    ]
    refused: list[str] = []
    failed: list[str] = []
    conn.execute("BEGIN")
    try:
        conn.execute(
            "INSERT INTO rows (row_id, year, state, name, in_scope, row_sha256, ingested_at) "
            "VALUES (-1, 2000, 'XX', 'selftest', 1, ?, ?)",
            (zero, stamp),
        )
        conn.execute(
            "INSERT INTO captures (capture_id, row_id, stage, url_requested, retrieved_at, "
            "content_sha256, body_path, tool, tool_version) "
            "VALUES (-1, -1, 'selftest', 'selftest:', ?, ?, 'store/00/x', 'selftest', '0')",
            (stamp, zero),
        )
        conn.execute(
            "INSERT INTO decisions (decision_id, row_id, kind, outcome, decided_by, decided_at) "
            "VALUES (-1, -1, 'review', 'selftest', 'selftest', ?)",
            (stamp,),
        )
        conn.execute(
            "INSERT INTO audit_log (audit_id, invoked_at, command, args_json) "
            "VALUES (-1, ?, 'selftest', '[]')",
            (stamp,),
        )
        if conn.execute("SELECT 1 FROM source_workbook").fetchone() is None:
            conn.execute(
                "INSERT INTO source_workbook (workbook_id, sha256, filename, sheet_name, "
                "header_row, data_rows, year_from, year_to, ingested_at) "
                "VALUES (1, ?, 'selftest.xlsx', 'Sheet', 1, 0, 2000, 2025, ?)",
                (zero, stamp),
            )
        attempts += [
            ("source_workbook UPDATE", "UPDATE source_workbook SET sha256 = 'x'", ()),
            ("source_workbook DELETE", "DELETE FROM source_workbook", ()),
            (
                "source_workbook INSERT OR REPLACE",
                "INSERT OR REPLACE INTO source_workbook (workbook_id, sha256, filename, "
                "sheet_name, header_row, data_rows, year_from, year_to, ingested_at) "
                "VALUES (1, 'x', 'x', 'x', 1, 0, 2000, 2025, ?)",
                (stamp,),
            ),
        ]
        for label, sql, params in attempts:
            (refused if _try_write(conn, sql, params) else failed).append(label)
        # The completion write is allowed exactly once.
        completed = not _try_write(
            conn,
            "UPDATE audit_log SET finished_at = ?, exit_code = 0 WHERE audit_id = -1",
            (stamp,),
        )
        again = _try_write(
            conn,
            "UPDATE audit_log SET finished_at = ?, exit_code = 1 WHERE audit_id = -1",
            (stamp,),
        )
        if completed:
            refused.append("audit_log completion allowed once")
        else:
            failed.append("audit_log completion (should be allowed once)")
        if again:
            refused.append("audit_log second completion")
        else:
            failed.append("audit_log second completion (should be refused)")
    except sqlite3.Error as exc:
        failed.append(f"probe could not run ({exc}); is another efp process holding the database?")
    finally:
        conn.rollback()
    left = conn.execute("SELECT count(*) FROM rows WHERE row_id = -1").fetchone()[0]
    if left:
        failed.append("synthetic row survived the rollback")
    if failed:
        return DbCheck("append-only triggers fire", False, "NOT enforced: " + ", ".join(failed))
    return DbCheck(
        "append-only triggers fire",
        True,
        f"{len(refused)} of {len(attempts) + 2} write attempts behaved as required (rolled back)",
    )


def _check_bodies(conn: sqlite3.Connection, config: Config) -> DbCheck:
    ids = [int(r[0]) for r in conn.execute("SELECT capture_id FROM captures ORDER BY capture_id")]
    total = len(ids)
    limit = config.database.selftest_capture_sample
    sampled = total > limit
    rng = random.Random(config.scope.sample_seed)  # noqa: S311  (seeded sampling, not crypto)
    chosen = rng.sample(ids, limit) if sampled else ids
    bad: list[str] = []
    for capture_id in chosen:
        body_path, expected = conn.execute(
            "SELECT body_path, content_sha256 FROM captures WHERE capture_id = ?", (capture_id,)
        ).fetchone()
        try:
            path = resolve_body_path(config.paths.store, body_path)
            if not body_path.endswith(expected):
                bad.append(f"{capture_id}: body_path does not name content_sha256")
            elif not path.is_file():
                bad.append(f"{capture_id}: body file missing")
            elif sha256_hex(path.read_bytes()) != expected:
                bad.append(f"{capture_id}: body does not hash to content_sha256")
        except StoreError as exc:
            bad.append(f"{capture_id}: {exc}")
    if total == 0:
        return DbCheck("capture bodies exist and hash", True, "no captures yet (nothing to verify)")
    scope = f"sample of {len(chosen)} of {total}" if sampled else f"all {total}"
    if bad:
        return DbCheck(
            "capture bodies exist and hash",
            False,
            f"{len(bad)} bad ({scope}): " + "; ".join(bad[:5]),
        )
    return DbCheck("capture bodies exist and hash", True, f"{scope} verified")


def _check_workbook_hashes(conn: sqlite3.Connection, config: Config) -> DbCheck:
    """Recompute every row hash from the source workbook and compare with ``rows``."""
    from efp.ingest.loader import sha256_file
    from efp.ingest.reader import WorkbookError, read_workbook

    name = "rows.row_sha256 matches the source workbook"
    record = conn.execute("SELECT sha256 FROM source_workbook").fetchone()
    if record is None:
        return DbCheck(name, True, "no workbook ingested yet (nothing to verify)")
    path = config.paths.source_workbook
    if not path.is_file():
        return DbCheck(name, True, f"skipped: the workbook is not at {path} (not an error)")
    if sha256_file(path) != record[0]:
        return DbCheck(
            name, False, "the file at paths.source_workbook is not the workbook that was ingested"
        )
    try:
        fresh = {r.row_id: r.sha256 for r in read_workbook(path).rows}
    except WorkbookError as exc:
        return DbCheck(name, False, f"workbook no longer reads cleanly: {exc}")
    stored = dict(conn.execute("SELECT row_id, row_sha256 FROM rows"))
    bad = sorted(i for i in set(fresh) | set(stored) if fresh.get(i) != stored.get(i))
    if bad:
        return DbCheck(name, False, f"{len(bad)} row(s) differ, first row_id(s): {bad[:5]}")
    return DbCheck(name, True, f"all {len(stored):,} rows recomputed from the workbook and match")


def run_db_checks(config: Config) -> list[DbCheck]:
    """Run every database check. A missing database is one failing check; nothing is created."""
    path = config.paths.database
    try:
        conn = connect(config, migrate=False, create=False)
    except DatabaseError as exc:
        return [DbCheck("database file", False, f"{exc}; run `efp db migrate` to create it")]
    except sqlite3.Error as exc:
        return [DbCheck("database file", False, f"cannot open {path}: {exc}")]
    checks: list[DbCheck] = [DbCheck("database file", True, str(path))]
    try:
        # journal_mode is a property of the file (it persists); foreign_keys and
        # recursive_triggers are per connection and are set by db.connect(), so checking them
        # here would only read back what connect() just wrote. The probes below test behaviour.
        journal = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        checks.append(DbCheck("journal mode is WAL", journal == "wal", f"journal_mode={journal}"))
        checks += _check_migrations(conn)
        tables, indexes, triggers = (
            _names(conn, "table"),
            _names(conn, "index"),
            _names(conn, "trigger"),
        )
        for label, expected, present in (
            ("tables present", EXPECTED_TABLES, tables),
            ("indexes present", EXPECTED_INDEXES, indexes),
            ("triggers present", EXPECTED_TRIGGERS, triggers),
        ):
            missing = [n for n in expected if n not in present]
            checks.append(
                DbCheck(
                    label,
                    not missing,
                    f"missing: {', '.join(missing)}" if missing else f"{len(expected)} found",
                )
            )
        if all(
            t in tables for t in ("rows", "captures", "decisions", "audit_log", "source_workbook")
        ) and all(t in triggers for t in EXPECTED_TRIGGERS):
            checks.append(_check_triggers_fire(conn))
            checks.append(_check_bodies(conn, config))
            orphans = [
                int(r[0])
                for r in conn.execute(
                    "SELECT capture_id FROM captures WHERE row_id IS NULL AND stage NOT IN (?, ?)",
                    ORPHAN_OK_STAGES,
                )
            ]
            checks.append(
                DbCheck(
                    "no orphan captures",
                    not orphans,
                    f"orphans: {orphans[:10]}"
                    if orphans
                    else "every capture has a row (or stage reference/selftest)",
                )
            )
            fk_problems = conn.execute("PRAGMA foreign_key_check").fetchall()
            checks.append(
                DbCheck(
                    "foreign keys hold",
                    not fk_problems,
                    f"{len(fk_problems)} violation(s), first in table {fk_problems[0][0]}"
                    if fk_problems
                    else "foreign_key_check is empty",
                )
            )
            bad_hash = [
                int(r[0])
                for r in conn.execute(
                    "SELECT row_id FROM rows WHERE length(row_sha256) != 64 "
                    "OR row_sha256 GLOB '*[^0-9a-f]*'"
                )
            ]
            has_rows = conn.execute("SELECT 1 FROM rows LIMIT 1").fetchone() is not None
            if bad_hash:
                hash_detail = f"malformed for row_id {bad_hash[:10]}"
            elif has_rows:
                hash_detail = "format only; the next check recomputes them from the workbook"
            else:
                hash_detail = "no rows yet (nothing to verify)"
            checks.append(DbCheck("rows.row_sha256 well-formed", not bad_hash, hash_detail))
            checks.append(_check_workbook_hashes(conn, config))
        else:
            checks.append(
                DbCheck(
                    "data checks",
                    False,
                    "skipped: schema incomplete (run `efp db migrate`)",
                )
            )
        quick = [str(r[0]) for r in conn.execute("PRAGMA quick_check")]
        checks.append(
            DbCheck(
                "sqlite quick_check",
                quick == ["ok"],
                "ok" if quick == ["ok"] else "; ".join(quick[:3]),
            )
        )
    except sqlite3.Error as exc:
        checks.append(DbCheck("database readable", False, f"sqlite error: {exc}"))
    finally:
        conn.close()
    return checks

"""Static guard: no code path under efp/ can alter the protected tables or their triggers.

The triggers are the runtime defence; this test is the review-time one. Statements that appear
in the allow-list are the only ones permitted, each for a stated reason.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from efp.audit import COMPLETE_SQL
from efp.manifest import TSA_SQL
from efp.spn import WAYBACK_SQL
from tests.conftest import REPO

PROTECTED = r"(rows|captures|decisions|audit_log|source_workbook)"

# file (relative to efp/) -> why it may mention the statement
PROBE_FILE = "dbcheck.py"  # selftest --db attempts these inside a transaction it always rolls back
INGEST_DIR = "ingest/"  # the only writer of rows (build 03)


def _code_only(text: str) -> str:
    """The module's code with docstrings and comments removed, so prose that explains a
    limitation (for example "DROP TRIGGER is not blocked") is not mistaken for code that does it."""
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def _sources() -> list[tuple[str, str]]:
    root = REPO / "efp"
    return [
        (str(p.relative_to(root)), _code_only(p.read_text(encoding="utf-8")))
        for p in sorted(root.rglob("*.py"))
    ]


def _hits(pattern: str, allowed_files: tuple[str, ...] = ()) -> list[str]:
    regex = re.compile(pattern, re.I | re.S)
    return [
        name
        for name, text in _sources()
        if regex.search(text) and not name.startswith(allowed_files)
    ]


def test_nothing_deletes_from_protected_tables() -> None:
    assert _hits(rf"DELETE\s+FROM\s+{PROTECTED}\b", (PROBE_FILE,)) == []


CAPTURE_UPDATES = (WAYBACK_SQL, TSA_SQL)


def test_only_the_audit_completion_updates_a_protected_table() -> None:
    sources = _sources()
    pattern = re.compile(rf"UPDATE\s+{PROTECTED}\b[^\"']*", re.I)
    offenders = []
    for name, text in sources:
        if name == PROBE_FILE:
            continue
        for match in pattern.finditer(text):
            found = match.group(0).strip()
            allowed = COMPLETE_SQL.startswith(found) or any(
                found.startswith(sql) for sql in CAPTURE_UPDATES
            )
            if found != "" and not allowed:
                offenders.append(f"{name}: {found[:60]}")
    assert offenders == []
    assert COMPLETE_SQL == "UPDATE audit_log SET finished_at = ?, exit_code = ? WHERE audit_id = ?"
    # The two NULL-to-value writes migration 0006 permits on captures, and nothing else.
    assert WAYBACK_SQL == (
        "UPDATE captures SET wayback_url = ? WHERE capture_id = ? AND wayback_url IS NULL"
    )
    assert TSA_SQL.startswith("UPDATE captures SET tsa_token_path = ? WHERE tsa_token_path IS NULL")


def test_only_ingest_inserts_into_rows() -> None:
    assert _hits(r"INSERT\s+(OR\s+\w+\s+)?INTO\s+rows\b", (PROBE_FILE, INGEST_DIR)) == []
    assert _hits(r"INSERT\s+(OR\s+\w+\s+)?INTO\s+source_workbook\b", (PROBE_FILE, INGEST_DIR)) == []


def test_nothing_uses_replace_semantics() -> None:
    assert _hits(r"\bREPLACE\s+INTO\b|\bINSERT\s+OR\s+REPLACE\b", (PROBE_FILE,)) == []
    assert _hits(r"ON\s+CONFLICT\b[^;]*\bDO\s+UPDATE\b", (PROBE_FILE,)) == []


def test_nothing_drops_or_rewrites_the_schema() -> None:
    assert _hits(r"\bDROP\s+(TABLE|TRIGGER|INDEX|VIEW)\b") == []
    assert _hits(r"\bALTER\s+TABLE\b") == []
    assert _hits(r"writable_schema") == []
    assert _hits(r"PRAGMA\s+recursive_triggers\s*=\s*(OFF|0)", ()) == []


def test_the_guard_can_see_the_probe_file() -> None:
    """If these statements ever disappear from dbcheck.py the allow-list above is stale."""
    probe = (REPO / "efp" / PROBE_FILE).read_text(encoding="utf-8")
    assert "INSERT OR REPLACE INTO rows" in probe
    assert Path(REPO / "efp" / "db.py").exists()


# Migration 0006 relaxes captures_no_update so wayback_url and tsa_token_path can be set once from
# NULL (build 05; reviewed by the operator). Nothing else may drop a trigger.
ALLOWED_TRIGGER_REPLACEMENTS = {"0006_capture_support.sql": "captures_no_update"}


def test_no_migration_drops_or_alters_the_protected_schema() -> None:
    """A later migration must not undo the guarantees: no DROP TRIGGER, no DROP/ALTER of the
    protected tables. (Adding indexes, new tables and new triggers is fine.)"""
    offenders = []
    for path in sorted((REPO / "migrations").glob("*.sql")):
        text = re.sub(r"--[^\n]*", "", path.read_text(encoding="utf-8"))
        drops = re.findall(r"\bDROP\s+TRIGGER\s+(\w+)", text, re.I)
        if path.name in ALLOWED_TRIGGER_REPLACEMENTS:
            # The one reviewed exception: the trigger is dropped and recreated in the same file.
            allowed = ALLOWED_TRIGGER_REPLACEMENTS[path.name]
            recreated = re.findall(r"\bCREATE\s+TRIGGER\s+(\w+)", text, re.I)
            if drops != [allowed] or allowed not in recreated:
                offenders.append(f"{path.name}: DROP TRIGGER other than a reviewed replacement")
        elif drops:
            offenders.append(f"{path.name}: DROP TRIGGER")
        if re.search(rf"\b(DROP|ALTER)\s+TABLE\s+(IF\s+EXISTS\s+)?{PROTECTED}\b", text, re.I):
            offenders.append(f"{path.name}: DROP/ALTER protected table")
    assert offenders == []

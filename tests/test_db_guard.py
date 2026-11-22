"""Static guard: no code path under efp/ can alter the protected tables or their triggers.

The triggers are the runtime defence; this test is the review-time one. Statements that appear
in the allow-list are the only ones permitted, each for a stated reason.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from efp.audit import COMPLETE_SQL
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


def test_only_the_audit_completion_updates_a_protected_table() -> None:
    sources = _sources()
    pattern = re.compile(rf"UPDATE\s+{PROTECTED}\b[^\"']*", re.I)
    offenders = []
    for name, text in sources:
        if name == PROBE_FILE:
            continue
        for match in pattern.finditer(text):
            if not COMPLETE_SQL.startswith(match.group(0).strip()) and match.group(0).strip() != "":
                offenders.append(f"{name}: {match.group(0)[:60]}")
    assert offenders == []
    assert COMPLETE_SQL == "UPDATE audit_log SET finished_at = ?, exit_code = ? WHERE audit_id = ?"


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


def test_no_migration_drops_or_alters_the_protected_schema() -> None:
    """A later migration must not undo the guarantees: no DROP TRIGGER, no DROP/ALTER of the
    protected tables. (Adding indexes, new tables and new triggers is fine.)"""
    offenders = []
    for path in sorted((REPO / "migrations").glob("*.sql")):
        text = re.sub(r"--[^\n]*", "", path.read_text(encoding="utf-8"))
        if re.search(r"\bDROP\s+TRIGGER\b", text, re.I):
            offenders.append(f"{path.name}: DROP TRIGGER")
        if re.search(rf"\b(DROP|ALTER)\s+TABLE\s+(IF\s+EXISTS\s+)?{PROTECTED}\b", text, re.I):
            offenders.append(f"{path.name}: DROP/ALTER protected table")
    assert offenders == []

"""Fixtures for the documents tests: PDFs stored as captures under a row, and a module context.

Everything is synthetic (see tests/pdfgen.py)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import efp.documents as component
from efp.capture import Capture
from efp.config import Config, load_config
from efp.documents.ladder import Candidate, PageTask
from efp.documents.modules import register_modules
from efp.documents.pdfio import PageSize, Word
from efp.documents.quality import score_lines
from efp.modules import Availability, ModuleResult, Outcome, Registry
from efp.runner import load_row, make_context
from efp.store import body_relpath, write_body
from efp.types import Context, Row
from tests.capturehelpers import make_capture
from tests.dbhelpers import STAMP

FULL_CHAIN = (
    'modules = ["documents.pdfplumber", "documents.docling", "documents.ocrmypdf", '
    '"documents.paddleocr", "documents.claude_transcribe"]'
)


def doc_config(
    write_config: Callable[..., Path], chain: str = FULL_CHAIN, **replace: str
) -> Config:
    swaps = {'modules = ["documents.pdfplumber"]': chain, **replace}
    return load_config(write_config(swaps))


def add_row(conn: sqlite3.Connection, row_id: int = 1, state: str = "CT") -> None:
    conn.execute(
        "INSERT INTO rows (row_id, year, state, name, in_scope, row_sha256, ingested_at) "
        "VALUES (?, 2014, ?, 'Alex Example', 1, ?, ?)",
        (row_id, state, f"{row_id:064d}", STAMP),
    )
    conn.execute(
        "INSERT INTO case_stage (row_id, stage, status) VALUES (?, 'documents', 'pending')",
        (row_id,),
    )
    conn.commit()


def store_capture(
    conn: sqlite3.Connection,
    config: Config,
    data: bytes,
    row_id: int = 1,
    *,
    content_type: str = "application/pdf",
    manual_source: str | None = None,
) -> int:
    """The bytes as a stored capture of the row (as the court stage leaves a downloaded PDF)."""
    digest = write_body(config.paths.store, data)
    cursor = conn.execute(
        "INSERT INTO captures (row_id, stage, url_requested, retrieved_at, http_status, "
        "content_type, content_sha256, body_path, tool, tool_version, manual_source) "
        "VALUES (?, 'court', 'https://example.test/filing.pdf', ?, 200, ?, ?, ?, 'fetch', '0', ?)",
        (row_id, STAMP, content_type, digest, body_relpath(digest), manual_source),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def add_filing(
    conn: sqlite3.Connection,
    config: Config,
    data: bytes,
    row_id: int = 1,
    *,
    via: str = "record",
    docket_id: int = 77,
    content_type: str = "application/pdf",
) -> int:
    """A PDF a court record points to. ``via``: record (pin_report capture), basis (a state docket
    lists it in basis_json.document_captures), filing (RECAP: a filing under a current docket),
    manual (an import with manual_source)."""
    capture_id = store_capture(
        conn,
        config,
        data,
        row_id,
        content_type=content_type,
        manual_source="westlaw" if via == "manual" else None,
    )
    if via == "record":
        conn.execute(
            "INSERT INTO court_records (row_id, source_kind, document_kind, capture_id, found_at) "
            "VALUES (?, 'pin_report', 'report', ?, ?)",
            (row_id, capture_id, STAMP),
        )
    elif via == "basis":
        conn.execute(
            "INSERT INTO court_records (row_id, source_kind, document_kind, found_at, basis_json) "
            "VALUES (?, 'state_docket', 'docket', ?, ?)",
            (row_id, STAMP, json.dumps({"document_captures": [capture_id]})),
        )
    elif via == "filing":
        conn.execute(
            "INSERT INTO court_records (row_id, source_kind, document_kind, docket_id, found_at) "
            "VALUES (?, 'federal_docket', 'docket', ?, ?)",
            (row_id, docket_id, STAMP),
        )
        conn.execute(
            "INSERT INTO court_records (row_id, source_kind, document_kind, docket_id, "
            "capture_id, found_at) VALUES (?, 'federal_docket', 'filing', ?, ?, ?)",
            (row_id, docket_id, capture_id, STAMP),
        )
    conn.commit()
    return capture_id


def make_ctx(
    config: Config, conn: sqlite3.Connection, registry: Registry | None = None
) -> tuple[Context, Capture, Registry]:
    cap = make_capture(config, conn, hosts=("api.anthropic.com", "example.test"))
    ctx = make_context(config, conn).model_copy(update={"capture": cap})
    if registry is None:
        registry = Registry()
        register_modules(registry)
    return ctx, cap, registry


def run_row(
    config: Config,
    conn: sqlite3.Connection,
    row_id: int = 1,
    registry: Registry | None = None,
) -> Any:
    ctx, cap, reg = make_ctx(config, conn, registry)
    try:
        row: Row = load_row(conn, row_id)
        return component.process_row(row, ctx, reg)
    finally:
        cap.close()


# --- scripted rungs: a deterministic ladder for tests that are not about a tool --------------------


def words(text: str, y: float = 100.0) -> list[list[Word]]:
    lines = []
    for index, line in enumerate(text.split("\n")):
        x = 54.0
        row: list[Word] = []
        for token in line.split():
            row.append(Word(token, x, y + index * 14, x + 6 * len(token), y + index * 14 + 10))
            x += 6 * len(token) + 4
        if row:
            lines.append(row)
    return lines


CLEAN = (
    "The defendant pleaded guilty to one count of making a false statement to a federal agency.\n"
    "The court accepted the plea after reviewing the written agreement of the parties and ordered\n"
    "a presentence report. Sentencing was set for the spring term of the court."
)
GARBLED = (
    "Tbe defcndant plea ded guihy to onc coun of rnaking a falsc staternent to a fedcral agcncy.\n"
    "Thc court acccpted thc plca aftcr rcvicwing thc writtcn agrccmcnt of thc partics and ordcrcd\n"
    "a prcscntcncc rcport. Scntcncing was sct for thc spring tcrm of thc court."
)


def scripted(name: str, texts: dict[int, str], *, ok: bool = True, why: str = "") -> type:
    """A rung that returns fixed text for the pages it knows, scored like any other."""

    class Rung:
        stage = "documents"

        def available(self, ctx: Context) -> Availability:
            return Availability.available() if ok else Availability.unavailable(why)

        def applies(self, row: Row, ctx: Context) -> bool | str:
            return True

        def run(self, row: Row, ctx: Context) -> ModuleResult:
            assert isinstance(row, PageTask)
            ran = {"ocrmypdf": "tesseract", "paddleocr": "paddleocr"}.get(name.split(".")[-1])
            if ran:
                row.p.ran.add(ran)  # as the real OCR rungs do
            text = texts.get(row.page_no)
            if text is None:
                return ModuleResult(Outcome.miss)
            lines = words(text)
            score = score_lines([" ".join(w.text for w in ln) for ln in lines])
            method = name.removeprefix("documents.")
            method = {
                "pdfplumber": "native",
                "ocrmypdf": "tesseract",
                "claude_transcribe": "model",
            }.get(method, method)
            row.p.add(Candidate(method, lines, score, PageSize(612, 792)))
            return ModuleResult(Outcome.hit, quality=score.score)

    Rung.name = name  # type: ignore[attr-defined]
    return Rung


def scripted_registry(**rungs: type) -> Registry:
    registry = Registry()
    for rung in rungs.values():
        registry.register(rung)
    return registry


def model_registry() -> Registry:
    """The real first rung and the real model rung, with an OCR rung that reads nothing: the page
    has been through OCR and failed, which is when the model is allowed to read it."""
    from efp.documents.modules import ClaudeTranscribe, Pdfplumber

    registry = Registry()
    registry.register(Pdfplumber)
    registry.register(scripted("documents.ocrmypdf", {}))
    registry.register(ClaudeTranscribe)
    return registry

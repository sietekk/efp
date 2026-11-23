"""Documents stage: the guards found in review (recheck through the runner, the model only after
OCR, the evidentiary bytes, the database rules, a failed write, a faint scan)."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import efp.documents as component
from efp import runner as rn
from efp import status as status_view
from efp.builtin_modules import register_builtin
from efp.capture import Capture
from efp.config import Config
from efp.documents import llm
from efp.documents.modules import ClaudeTranscribe, Pdfplumber
from efp.modules import Registry
from efp.store import store_path
from tests import pdfgen
from tests.documents_helpers import (
    add_filing,
    add_row,
    doc_config,
    make_ctx,
    model_registry,
    run_row,
)
from tests.test_documents_model import SONNET, Stub

ALL_STAGES = (
    'stages = ["resolve_links", "court", "identity", "documents", "news", "social", '
    '"extract", "code"]'
)
DIGITS = [["1234 5678 9012 3456 7890 1234 5678"] * 6]


def config_with_stages(write_config: Callable[..., Path], **more: str) -> Config:
    return doc_config(write_config, **{'stages = ["resolve_links", "court"]': ALL_STAGES, **more})


def test_recheck_through_the_runner_is_a_noop_until_a_deferred_row_comes_due(
    write_config: Callable[..., Path], conn: sqlite3.Connection
) -> None:
    register_builtin()
    config = config_with_stages(write_config)
    add_row(conn)
    add_filing(conn, config, pdfgen.born_digital([pdfgen.LINES_A]))
    conn.execute("INSERT INTO case_stage (row_id, stage, status) VALUES (1, 'court', 'done')")
    conn.commit()
    ctx, cap, _ = make_ctx(config, conn)
    try:
        first = rn.run_stage(conn, config, rn.RunOptions("documents"), ctx, component=component)
        assert (first.claimed, first.done, first.failed) == (1, 1, 0)
        again = rn.run_stage(
            conn, config, rn.RunOptions("documents", recheck=True), ctx, component=component
        )
        assert again.noop == 1 and again.claimed == 0 and again.failed == 0
        conn.execute(
            "UPDATE case_stage SET input_hash = input_hash || ':budget' "
            "WHERE row_id = 1 AND stage = 'documents'"
        )
        conn.commit()
        due = rn.run_stage(
            conn, config, rn.RunOptions("documents", recheck=True), ctx, component=component
        )
        assert due.claimed == 1 and due.done == 1  # the deferred row is revisited
    finally:
        cap.close()


# --- the model reads only what OCR failed on -----------------------------------------------------------


def _stubbed(monkeypatch: pytest.MonkeyPatch) -> Stub:
    s = Stub()
    monkeypatch.setattr(Capture, "llm_message", lambda self, **kw: s(self, **kw))
    return s


def test_the_model_is_not_called_when_no_ocr_engine_ran(
    write_config: Callable[..., Path], conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = _stubbed(monkeypatch)
    config = doc_config(
        write_config,
        'modules = ["documents.pdfplumber", "documents.claude_transcribe"]',
        **SONNET,
    )
    add_row(conn)
    add_filing(conn, config, pdfgen.image_only([pdfgen.LINES_A]))
    registry = Registry()
    registry.register(Pdfplumber)
    registry.register(ClaudeTranscribe)
    run_row(config, conn, registry=registry)
    assert s.calls == []
    assert conn.execute("SELECT method, flagged FROM document_pages_current").fetchall() == [
        ("failed", 1)
    ]
    modules = json.loads(conn.execute("SELECT methods_json FROM documents").fetchone()[0])
    skipped = [m for m in modules["pages"][0]["modules"] if m["name"].endswith("transcribe")]
    assert "no OCR engine ran" in skipped[0]["reason"]


def test_a_digital_page_with_a_low_score_is_never_sent_to_the_model(
    write_config: Callable[..., Path], conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = _stubbed(monkeypatch)
    config = doc_config(
        write_config,
        'modules = ["documents.pdfplumber", "documents.ocrmypdf", "documents.claude_transcribe"]',
        **SONNET,
    )
    add_row(conn)
    add_filing(
        conn, config, pdfgen.born_digital(DIGITS)
    )  # a ledger of digits: real text, low score
    run_row(config, conn, registry=model_registry())
    assert s.calls == []  # OCR ran (scripted) and found nothing, but the page has a text layer
    ((method, quality, flagged),) = conn.execute(
        "SELECT method, quality, flagged FROM document_pages_current"
    ).fetchall()
    assert (method, flagged) == ("native", 1) and quality < 0.85  # native text kept, flagged


def test_the_model_waits_for_every_installed_ocr_engine(
    write_config: Callable[..., Path], conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from efp.documents import modules as m

    s = _stubbed(monkeypatch)
    monkeypatch.setattr(m.Paddleocr, "available", lambda self, ctx: m.Availability.available())
    chain = (
        'modules = ["documents.pdfplumber", "documents.ocrmypdf", "documents.claude_transcribe"]'
    )
    config = doc_config(write_config, chain, **SONNET)
    add_row(conn)
    add_filing(conn, config, pdfgen.image_only([pdfgen.LINES_A]))
    run_row(config, conn, registry=model_registry())  # paddle is "installed" but not in the chain
    assert len(s.calls) == 1  # a rung that is not in the chain is not waited for


# --- evidentiary bytes -----------------------------------------------------------------------------------


def test_a_tampered_store_body_is_not_read_and_not_touched(
    write_config: Callable[..., Path], conn: sqlite3.Connection
) -> None:
    config = doc_config(write_config)
    add_row(conn)
    capture_id = add_filing(conn, config, pdfgen.born_digital([pdfgen.LINES_A]))
    sha = conn.execute(
        "SELECT content_sha256 FROM captures WHERE capture_id = ?", (capture_id,)
    ).fetchone()[0]
    path = store_path(sha, config.paths.store)
    os.chmod(path, 0o600)
    path.write_bytes(path.read_bytes() + b"\n% tampered\n")
    tampered = path.read_bytes()
    result = run_row(config, conn)
    assert result.summary_json["documents_status"] == "unreadable"
    assert "not verified" in result.summary_json["documents"][0]["reason"]
    assert path.read_bytes() == tampered  # never "repaired", never overwritten


def test_the_tools_work_on_a_copy_not_on_the_stored_file(
    write_config: Callable[..., Path], conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from efp.documents import pdfio

    config = doc_config(write_config)
    add_row(conn)
    capture_id = add_filing(conn, config, pdfgen.born_digital([pdfgen.LINES_A]))
    sha = conn.execute(
        "SELECT content_sha256 FROM captures WHERE capture_id = ?", (capture_id,)
    ).fetchone()[0]
    stored = store_path(sha, config.paths.store)
    opened: list[Path] = []
    real = pdfio.native_page

    def spy(path: Path, index: int) -> Any:
        opened.append(path)
        return real(path, index)

    monkeypatch.setattr(pdfio, "native_page", spy)
    run_row(config, conn)
    assert opened and all(p != stored for p in opened)


# --- the database rules -----------------------------------------------------------------------------------


def test_the_database_refuses_an_unflagged_page_below_the_threshold(
    write_config: Callable[..., Path], conn: sqlite3.Connection
) -> None:
    config = doc_config(write_config)
    add_row(conn)
    add_filing(conn, config, pdfgen.born_digital([pdfgen.LINES_A]))
    run_row(config, conn)
    with pytest.raises(sqlite3.DatabaseError, match="below the quality threshold"):
        conn.execute("INSERT INTO document_pages VALUES (1, 9, 'native', 0.5, 0, '[]', 0, 0)")
    conn.execute("INSERT INTO document_pages VALUES (1, 9, 'native', 0.5, 1, '[\"x\"]', 0, 0)")
    conn.execute("INSERT INTO document_pages VALUES (1, 10, 'native', 0.9, 0, '[]', 0, 0)")


def test_the_extraction_view_applies_the_documents_own_threshold(
    write_config: Callable[..., Path], conn: sqlite3.Connection
) -> None:
    config = doc_config(write_config)
    add_row(conn)
    add_filing(conn, config, pdfgen.born_digital([pdfgen.LINES_A]))
    run_row(config, conn)
    assert conn.execute("SELECT count(*) FROM document_pages_extractable").fetchone()[0] == 1
    # a page stored flagged=0 with a quality at the threshold is still held to it by the view
    assert (
        conn.execute(
            "SELECT count(*) FROM document_pages_extractable e JOIN documents d "
            "ON d.document_id = e.document_id "
            "WHERE e.quality < json_extract(d.quality_json, '$.threshold')"
        ).fetchone()[0]
        == 0
    )


def test_a_capture_has_only_one_current_document(
    write_config: Callable[..., Path], conn: sqlite3.Connection
) -> None:
    config = doc_config(write_config)
    add_row(conn)
    capture_id = add_filing(conn, config, pdfgen.born_digital([pdfgen.LINES_A]))
    run_row(config, conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO documents (row_id, capture_id, ocr_applied, current) VALUES (1, ?, 0, 1)",
            (capture_id,),
        )


def test_a_failed_write_leaves_no_partial_document_and_keeps_the_old_one(
    write_config: Callable[..., Path], conn: sqlite3.Connection
) -> None:
    config = doc_config(write_config)
    add_row(conn)
    add_filing(conn, config, pdfgen.born_digital([pdfgen.LINES_A]))
    run_row(config, conn)
    stricter = doc_config(
        write_config, **{"ocr_quality_threshold = 0.85": "ocr_quality_threshold = 0.99"}
    )
    conn.execute(
        "CREATE TEMP TRIGGER boom BEFORE INSERT ON document_pages BEGIN "
        "SELECT RAISE(ABORT, 'boom'); END"
    )
    ctx, cap, _ = make_ctx(stricter, conn)
    try:
        from efp.runner import load_row

        (result,) = component.run([load_row(conn, 1)], ctx)
    finally:
        cap.close()
    assert result.status == "failed" and "boom" in (result.error or "")
    assert conn.execute("SELECT document_id, current FROM documents").fetchall() == [(1, 1)]
    assert (
        conn.execute("SELECT count(*) FROM document_pages WHERE document_id = 1").fetchone()[0] == 1
    )


# --- docling_always, a faint scan, the spend line ---------------------------------------------------------


def test_docling_always_gives_a_born_digital_document_its_structure(
    write_config: Callable[..., Path], conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_documents_model import _fake_docling

    _fake_docling(monkeypatch)
    chain = 'modules = ["documents.pdfplumber", "documents.docling"]'
    config = doc_config(write_config, chain, **{"docling_always = false": "docling_always = true"})
    add_row(conn)
    add_filing(conn, config, pdfgen.born_digital([pdfgen.LINES_A]))
    run_row(config, conn)
    assert conn.execute("SELECT method FROM document_pages_current").fetchall() == [("native",)]
    assert conn.execute("SELECT docling_path FROM documents").fetchone()[0].startswith("store/")
    off = doc_config(write_config, chain)
    add_row(conn, 2)
    add_filing(conn, off, pdfgen.born_digital([pdfgen.LINES_B]), 2)
    run_row(off, conn, 2)
    assert conn.execute("SELECT docling_path FROM documents WHERE row_id = 2").fetchone()[0] is None


def test_a_faint_scan_is_not_recorded_as_a_blank_page(
    write_config: Callable[..., Path], conn: sqlite3.Connection
) -> None:
    config = doc_config(write_config, 'modules = ["documents.pdfplumber"]')
    add_row(conn)
    add_filing(conn, config, pdfgen.faint([pdfgen.LINES_A]))
    run_row(config, conn)
    assert conn.execute("SELECT method, flagged FROM document_pages_current").fetchall() == [
        ("failed", 1)
    ]  # not "blank": nothing read it, so it is flagged for review


def test_the_status_spend_line_counts_only_transcription(
    write_config: Callable[..., Path], conn: sqlite3.Connection
) -> None:
    config = doc_config(write_config)
    for purpose, cost in (("transcribe_page", 0.25), ("extract", 5.0)):
        llm.log_call(
            conn, row_id=None, stage="x", purpose=purpose, capture_id=None, model_id="m",
            prompt_sha256=None, temperature=None, thinking=None, input_tokens=1, output_tokens=1,
            cost=cost, request_id=None, ok=True,
        )  # fmt: skip
    assert status_view.build_report(conn, config).documents["transcription_spend_usd"] == 0.25
    assert llm.spent_this_month(conn) == pytest.approx(5.25)  # the cap counts every purpose


# --- OCRmyPDF must not change how the rest of the process reads PDFs ------------------------------------


def test_unmapped_glyph_text_cannot_pass_as_a_good_page() -> None:
    from efp.documents.quality import score_lines

    garbage = ["".join(f"(cid:{n})" for n in range(40, 80))] * 4
    assert score_lines(garbage).score < 0.85


@pytest.mark.skipif(
    __import__("shutil").which("tesseract") is None, reason="the tesseract binary is not installed"
)
def test_running_ocr_leaves_pdfplumber_reading_standard_fonts_correctly(
    write_config: Callable[..., Path], conn: sqlite3.Connection
) -> None:
    """Importing ocrmypdf patches pdfminer for the whole process (a standard-font PDF then reads as
    "(cid:72)(cid:101)..."), so OCR runs as a subprocess and the process never imports it."""
    import sys

    config = doc_config(write_config)
    add_row(conn)
    add_filing(conn, config, pdfgen.image_only([pdfgen.LINES_A]))
    run_row(config, conn)
    assert conn.execute("SELECT method FROM document_pages_current").fetchall() == [("tesseract",)]
    assert "ocrmypdf" not in sys.modules
    add_row(conn, 2)
    add_filing(conn, config, pdfgen.born_digital([pdfgen.LINES_B]), 2)
    run_row(config, conn, 2)
    got = conn.execute(
        "SELECT method, quality FROM document_pages_current p JOIN documents d USING (document_id) "
        "WHERE d.row_id = 2"
    ).fetchall()
    assert got[0][0] == "native" and got[0][1] >= 0.85  # not "(cid:..)" garbage

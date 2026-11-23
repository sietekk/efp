"""The documents stage (build 10): the ladder, the page score, provenance and the flags.

Synthetic PDFs only (tests/pdfgen.py). The OCR test needs the tesseract binary and skips without it.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from efp.config import Config
from efp.documents import canonical, pdfio
from efp.documents.pdfio import Word, group_lines
from efp.documents.quality import score_lines
from efp.store import read_verified
from tests import pdfgen
from tests.documents_helpers import (
    CLEAN,
    GARBLED,
    add_filing,
    add_row,
    doc_config,
    run_row,
    scripted,
    scripted_registry,
)

THRESHOLD = 0.85
needs_tesseract = pytest.mark.skipif(
    shutil.which("tesseract") is None, reason="the tesseract binary is not installed"
)
THREE_PAGES = pdfgen.born_digital([pdfgen.LINES_A, pdfgen.LINES_B, pdfgen.LINES_A])
SCRIPTED_CHAIN = (
    'modules = ["documents.pdfplumber", "documents.ocrmypdf", "documents.claude_transcribe"]'
)


def pages(conn: sqlite3.Connection) -> list[tuple[int, str, float | None, int, str]]:
    return conn.execute(
        "SELECT page_no, method, quality, flagged, flag_reasons FROM document_pages_current "
        "ORDER BY document_id, page_no"
    ).fetchall()


def current_doc(conn: sqlite3.Connection) -> sqlite3.Row:
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM documents WHERE current = 1").fetchone()
    finally:
        conn.row_factory = None


# --- the score ------------------------------------------------------------------------------------


def test_the_score_ranks_clean_above_degraded_above_junk() -> None:
    clean = score_lines(CLEAN.split("\n")).score
    degraded = score_lines(GARBLED.split("\n")).score
    junk = score_lines(
        ["~~~~ |||| ¬¬ ,, .. ^^ lI1 | ] [ ' '", "; ; ; l l | | = = - - ~ ~ ~", "í ï ì | ¬ ¬ 1 1"]
    )
    assert clean >= THRESHOLD > degraded > junk.score
    assert 0.0 <= junk.score <= clean <= 1.0
    assert set(junk.parts) <= {"dictionary", "charset", "lines", "confidence"}


def test_short_and_empty_pages_are_scored_on_what_they_have() -> None:
    short = score_lines(["Page 3"])
    assert short.short and set(short.parts) == {"charset"}
    empty = score_lines(["", "  "])
    assert empty.empty and empty.score == 0.0


def test_engine_confidence_is_part_of_the_score_only_when_given() -> None:
    lines = CLEAN.split("\n")
    assert "confidence" not in score_lines(lines).parts
    assert "confidence" in score_lines(lines, [0.99, 0.97, 0.98]).parts
    assert score_lines(lines, [0.2, 0.3]).score < score_lines(lines, [0.99]).score


# --- canonical text and provenance ------------------------------------------------------------------


def test_canonical_text_rules_and_offsets() -> None:
    def w(text: str, x: float = 0.0) -> Word:
        return Word(text, x, 10, x + 20, 20)

    page1 = canonical.PageText(1, "native", [[w("Café", 0), w("two", 30)], [w("​"), w("ok", 0)]])
    page2 = canonical.PageText(2, "blank")
    page3 = canonical.PageText(3, "native", [[w("end")]])
    canon = canonical.build([page1, page2, page3])
    assert (
        canon.text == "Café two\nok\f\fend"
    )  # NFC; zero-width word dropped; empty page; \f between
    assert canon.spans == {1: (0, 11), 2: (12, 12), 3: (13, 16)}
    for unit in canon.units:
        assert canon.text[unit["char_start"] : unit["char_end"]] in {"Café", "two", "ok", "end"}
    assert canon.sha256 == hashlib.sha256(canon.text.encode()).hexdigest()


def test_group_lines_orders_words_top_to_bottom_left_to_right() -> None:
    def w(t: str, x: float, top: float) -> Word:
        return Word(t, x, top, x + 10, top + 10)

    lines = group_lines([w("b", 30, 51), w("a", 10, 50), w("c", 10, 80)])
    assert [[x.text for x in line] for line in lines] == [["a", "b"], ["c"]]


def test_a_words_char_offsets_resolve_to_its_page_and_box(
    write_config: Callable[..., Path], conn: sqlite3.Connection
) -> None:
    config = doc_config(write_config)
    add_row(conn)
    capture_id = add_filing(conn, config, THREE_PAGES)
    result = run_row(config, conn)
    assert result.status == "done" and result.summary_json["documents_status"] == "read"
    doc = current_doc(conn)
    row = conn.execute(
        "SELECT sha256 FROM (SELECT content_sha256 AS sha256 FROM captures WHERE capture_id = ?)",
        (doc["text_capture_id"],),
    ).fetchone()
    text = read_verified(config.paths.store, row[0]).decode()
    layout_sha = conn.execute(
        "SELECT content_sha256 FROM captures WHERE capture_id = ?", (doc["layout_capture_id"],)
    ).fetchone()[0]
    layout = json.loads(read_verified(config.paths.store, layout_sha))
    assert hashlib.sha256(text.encode()).hexdigest() == doc["text_sha256"] == layout["text_sha256"]
    assert doc["canonical_version"] == canonical.CANONICAL_VERSION == layout["canonical_version"]
    assert text.count("\f") == 2
    # the second page's first line, found by its offsets
    second = text.split("\f")[1]
    start = len(text.split("\f")[0]) + 1 + second.index("Sam")
    found = canonical.resolve(layout, start, start + len("Sam"))
    assert [f["page"] for f in found] == [2]
    # pdfplumber's own box for that word
    store_path = config.paths.store
    pdf_sha = conn.execute(
        "SELECT content_sha256 FROM captures WHERE capture_id = ?", (capture_id,)
    ).fetchone()[0]
    pdf_path = store_path / pdf_sha[:2] / pdf_sha
    import pdfplumber

    with pdfplumber.open(pdf_path) as pdf:
        want = next(w for w in pdf.pages[1].extract_words() if w["text"] == "Sam")
    x0, top, x1, bottom = found[0]["bbox"]
    assert (x0, top, x1, bottom) == pytest.approx(
        (want["x0"], want["top"], want["x1"], want["bottom"]), abs=0.02
    )
    # a span over two words gives one union box
    both = canonical.resolve(layout, start, start + len("Sam Sample,"))
    assert both[0]["bbox"][0] == pytest.approx(x0, abs=0.02) and both[0]["bbox"][2] > x1


def test_the_canonical_hash_is_stable_across_runs_and_a_rerun_adds_nothing(
    write_config: Callable[..., Path], conn: sqlite3.Connection
) -> None:
    config = doc_config(write_config)
    add_row(conn)
    add_filing(conn, config, THREE_PAGES)
    first = run_row(config, conn)
    again = run_row(config, conn)
    docs = conn.execute("SELECT document_id, text_sha256, current FROM documents").fetchall()
    assert len(docs) == 1 and docs[0][2] == 1  # nothing duplicated
    assert first.input_hash == again.input_hash
    parts = [
        canonical.PageText(i + 1, "native", pdfio.native_page(_stored(conn, config), i)[0])
        for i in range(3)
    ]
    assert canonical.build(parts).sha256 == docs[0][1]


def _stored(conn: sqlite3.Connection, config: Config) -> Path:
    sha = conn.execute("SELECT pre_ocr_sha256 FROM documents").fetchone()[0]
    return config.paths.store / sha[:2] / sha


# --- the ladder -------------------------------------------------------------------------------------


def test_a_born_digital_pdf_is_read_by_pdfplumber_alone(
    write_config: Callable[..., Path], conn: sqlite3.Connection
) -> None:
    config = doc_config(write_config)
    add_row(conn)
    add_filing(conn, config, THREE_PAGES)
    run_row(config, conn)
    assert [p[1] for p in pages(conn)] == ["native"] * 3
    assert all(p[2] >= THRESHOLD and p[3] == 0 for p in pages(conn))
    doc = current_doc(conn)
    assert doc["ocr_applied"] == 0 and doc["post_ocr_sha256"] is None
    assert conn.execute("SELECT count(*) FROM captures WHERE tool = 'ocrmypdf'").fetchone()[0] == 0
    methods = json.loads(doc["methods_json"])
    assert [m["name"] for m in methods["pages"][0]["modules"]] == ["documents.pdfplumber"]


@needs_tesseract
def test_an_image_only_pdf_is_ocrd_and_the_original_bytes_survive(
    write_config: Callable[..., Path], conn: sqlite3.Connection
) -> None:
    config = doc_config(write_config)
    add_row(conn)
    original = pdfgen.image_only([pdfgen.LINES_A])
    capture_id = add_filing(conn, config, original)
    run_row(config, conn)
    (page,) = pages(conn)
    assert page[1] == "tesseract" and page[2] >= THRESHOLD and page[3] == 0
    doc = current_doc(conn)
    assert doc["ocr_applied"] == 1 and doc["ocr_engine"] == "tesseract"
    assert doc["pre_ocr_sha256"] == hashlib.sha256(original).hexdigest()  # the evidentiary hash
    assert doc["post_ocr_sha256"] and doc["post_ocr_sha256"] != doc["pre_ocr_sha256"]
    assert read_verified(config.paths.store, doc["pre_ocr_sha256"]) == original  # never overwritten
    assert read_verified(config.paths.store, doc["post_ocr_sha256"]).startswith(b"%PDF")
    assert (
        conn.execute(
            "SELECT count(*) FROM captures WHERE tool = 'ocrmypdf' AND content_sha256 = ?",
            (doc["post_ocr_sha256"],),
        ).fetchone()[0]
        == 1
    )
    assert doc["capture_id"] == capture_id


def test_a_blank_page_is_neither_text_nor_a_failure(
    write_config: Callable[..., Path], conn: sqlite3.Connection
) -> None:
    config = doc_config(write_config)
    add_row(conn)
    add_filing(
        conn, config, pdfgen.merge(pdfgen.born_digital([pdfgen.LINES_A]), pdfgen.blank_page())
    )
    run_row(config, conn)
    got = pages(conn)
    assert [(p[1], p[3]) for p in got] == [("native", 0), ("blank", 0)]
    assert current_doc(conn)["quality_min"] == got[0][2]  # a blank page is not the lowest page
    extractable = conn.execute("SELECT page_no FROM document_pages_extractable").fetchall()
    assert extractable == [(1,)]


def test_the_ladder_falls_through_rungs_and_flags_what_stays_below_the_bar(
    write_config: Callable[..., Path], conn: sqlite3.Connection
) -> None:
    config = doc_config(write_config, SCRIPTED_CHAIN)
    add_row(conn)
    add_filing(conn, config, THREE_PAGES)
    registry = scripted_registry(
        native=scripted("documents.pdfplumber", {1: CLEAN, 2: GARBLED}),
        ocr=scripted("documents.ocrmypdf", {2: GARBLED, 3: CLEAN}),
        model=scripted("documents.claude_transcribe", {}, ok=False, why="no key"),
    )
    result = run_row(config, conn, registry=registry)
    got = pages(conn)
    assert [(p[0], p[1], p[3]) for p in got] == [
        (1, "native", 0),
        (2, "native", 1),
        (3, "tesseract", 0),
    ]
    assert json.loads(got[1][4]) == ["below_threshold"]
    assert current_doc(conn)["quality_min"] == pytest.approx(got[1][2])
    # the flagged page's text is kept in the canonical text, but it is not extraction-eligible
    spans = conn.execute(
        "SELECT page_no, char_start, char_end FROM document_pages ORDER BY page_no"
    ).fetchall()
    assert spans[1][2] > spans[1][1]
    assert [r[0] for r in conn.execute("SELECT page_no FROM document_pages_extractable")] == [1, 3]
    modules = json.loads(current_doc(conn)["methods_json"])["pages"][1]["modules"]
    assert [(m["name"], m["status"]) for m in modules] == [
        ("documents.pdfplumber", "hit"),
        ("documents.ocrmypdf", "hit"),
        ("documents.claude_transcribe", "skipped"),
    ]
    assert result.summary_json["pages_by_method"] == {"native": 2, "tesseract": 1}


def test_no_page_below_the_threshold_reaches_extraction_unflagged(
    write_config: Callable[..., Path], conn: sqlite3.Connection
) -> None:
    """The done-when: over a mixed corpus every page that is below the bar, transcribed by a model
    or without text is flagged, and the extraction view holds none of them."""
    config = doc_config(write_config, SCRIPTED_CHAIN)
    pdfs = [
        pdfgen.born_digital([pdfgen.LINES_A, pdfgen.LINES_B, pdfgen.LINES_A, pdfgen.LINES_B]),
        pdfgen.born_digital([pdfgen.LINES_B, pdfgen.LINES_A]),
        pdfgen.image_only([pdfgen.LINES_A]),
    ]
    for row_id, data in enumerate(pdfs, start=1):
        add_row(conn, row_id)
        add_filing(conn, config, data, row_id)
    registry = scripted_registry(
        native=scripted("documents.pdfplumber", {1: CLEAN, 2: GARBLED}),
        ocr=scripted("documents.ocrmypdf", {2: GARBLED}),
        model=scripted("documents.claude_transcribe", {3: CLEAN}),
    )
    for row_id in (1, 2, 3):
        run_row(config, conn, row_id, registry)
    got = conn.execute(
        "SELECT page_no, method, quality, flagged FROM document_pages_current"
    ).fetchall()
    assert {p[1] for p in got} >= {"native", "model", "failed"}
    for _no, method, quality, flagged in got:
        if quality is None or quality < THRESHOLD or method in ("model", "failed"):
            assert flagged == 1, (method, quality)
    bad = conn.execute(
        "SELECT count(*) FROM document_pages_extractable WHERE flagged = 1 OR quality < ? "
        "OR method IN ('model', 'failed', 'blank')",
        (THRESHOLD,),
    ).fetchone()[0]
    assert bad == 0
    assert conn.execute("SELECT count(*) FROM document_pages_extractable").fetchone()[0] > 0


def test_a_missing_tool_is_skipped_with_its_reason_and_the_row_is_revisited(
    write_config: Callable[..., Path], conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = doc_config(write_config)
    add_row(conn)
    add_filing(conn, config, pdfgen.image_only([pdfgen.LINES_A]))
    monkeypatch.setattr(shutil, "which", lambda _name: None)  # no tesseract
    monkeypatch.setattr(pdfio, "have", lambda m: m == "pdfplumber")  # nothing else installed
    result = run_row(config, conn)
    (page,) = pages(conn)
    assert (page[1], page[3]) == ("failed", 1)
    reasons = {
        m["name"]: m["reason"]
        for m in json.loads(current_doc(conn)["methods_json"])["pages"][0]["modules"]
    }
    assert "not installed" in reasons["documents.docling"]
    assert "not installed" in reasons["documents.ocrmypdf"]
    assert "not installed" in reasons["documents.paddleocr"]
    assert result.status == "done" and result.input_hash.endswith(":tools")


def test_an_unreadable_pdf_is_recorded_and_the_row_still_finishes(
    write_config: Callable[..., Path], conn: sqlite3.Connection
) -> None:
    config = doc_config(write_config)
    add_row(conn)
    add_filing(conn, config, b"%PDF-1.4\nthis is not a pdf\n")
    result = run_row(config, conn)
    assert result.status == "done" and result.summary_json["documents_status"] == "unreadable"
    doc = current_doc(conn)
    assert doc["page_count"] is None and "unreadable" in json.loads(doc["quality_json"])
    assert conn.execute("SELECT count(*) FROM document_pages").fetchone()[0] == 0


def test_pages_past_the_limit_are_recorded_as_not_read(
    write_config: Callable[..., Path], conn: sqlite3.Connection
) -> None:
    config = doc_config(write_config, **{"max_pages = 500": "max_pages = 2"})
    add_row(conn)
    add_filing(conn, config, THREE_PAGES)
    run_row(config, conn)
    got = pages(conn)
    assert [(p[1], p[3]) for p in got] == [("native", 0), ("native", 0), ("failed", 1)]
    assert json.loads(got[2][4]) == ["not_read"]


# --- which PDFs a row has ---------------------------------------------------------------------------


@pytest.mark.parametrize("via", ["record", "basis", "filing", "manual"])
def test_every_way_a_pdf_reaches_a_row_is_read(
    via: str, write_config: Callable[..., Path], conn: sqlite3.Connection
) -> None:
    config = doc_config(write_config)
    add_row(conn)
    add_filing(conn, config, pdfgen.born_digital([pdfgen.LINES_A]), via=via)
    result = run_row(config, conn)
    assert result.summary_json["documents_status"] == "read"
    assert len(pages(conn)) == 1


def test_a_filing_under_a_docket_that_is_no_longer_current_is_not_read(
    write_config: Callable[..., Path], conn: sqlite3.Connection
) -> None:
    config = doc_config(write_config)
    add_row(conn)
    add_filing(conn, config, pdfgen.born_digital([pdfgen.LINES_A]), via="filing")
    conn.execute("UPDATE court_records SET current = 0 WHERE document_kind = 'docket'")
    conn.commit()
    assert run_row(config, conn).summary_json["documents_status"] == "no_documents"


def test_a_row_with_no_pdf_is_done_and_a_non_pdf_capture_is_ignored(
    write_config: Callable[..., Path], conn: sqlite3.Connection
) -> None:
    from tests.documents_helpers import store_capture

    config = doc_config(write_config)
    add_row(conn)
    html = store_capture(conn, config, b"<html>press release</html>", content_type="text/html")
    conn.execute(
        "INSERT INTO court_records (row_id, source_kind, document_kind, capture_id, found_at) "
        "VALUES (1, 'doj_release', 'release', ?, '2026-01-01T00:00:00+00:00')",
        (html,),
    )
    conn.commit()
    result = run_row(config, conn)
    assert result.status == "done" and result.summary_json["documents_status"] == "no_documents"
    assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 0


def test_a_rerun_with_a_new_threshold_replaces_the_current_row_and_keeps_the_old(
    write_config: Callable[..., Path], conn: sqlite3.Connection
) -> None:
    config = doc_config(write_config)
    add_row(conn)
    add_filing(conn, config, THREE_PAGES)
    run_row(config, conn)
    stricter = doc_config(
        write_config, **{"ocr_quality_threshold = 0.85": "ocr_quality_threshold = 0.99"}
    )
    run_row(stricter, conn)
    rows = conn.execute(
        "SELECT document_id, current FROM documents ORDER BY document_id"
    ).fetchall()
    assert rows == [(1, 0), (2, 1)]
    assert (
        conn.execute("SELECT count(*) FROM document_pages WHERE document_id = 1").fetchone()[0] == 3
    )


# --- the tables -------------------------------------------------------------------------------------


def test_documents_and_pages_are_append_only(
    write_config: Callable[..., Path], conn: sqlite3.Connection
) -> None:
    config = doc_config(write_config)
    add_row(conn)
    add_filing(conn, config, THREE_PAGES)
    run_row(config, conn)
    for sql in (
        "UPDATE documents SET page_count = 99",
        "UPDATE documents SET quality_min = 0.0",
        "DELETE FROM documents",
        "UPDATE document_pages SET flagged = 1",
        "DELETE FROM document_pages",
        "INSERT INTO document_pages VALUES (1, 9, 'model', 0.9, 0, '[]', 0, 0)",
    ):
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(sql)
    conn.execute("UPDATE documents SET current = 0")  # the one change allowed
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE documents SET current = 1")


# --- no PyMuPDF ---------------------------------------------------------------------------------------


def test_pymupdf_is_nowhere_in_the_code_or_the_lockfile() -> None:
    import ast

    repo = Path(__file__).resolve().parents[1]
    for path in sorted((repo / "efp").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            assert not [n for n in names if n.split(".")[0].lower() in ("fitz", "pymupdf")], path
    lock = (repo / "uv.lock").read_text(encoding="utf-8").lower()
    assert 'name = "pymupdf"' not in lock and 'name = "pymupdf4llm"' not in lock

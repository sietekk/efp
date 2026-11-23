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
from efp import llm
from efp import runner as rn
from efp import status as status_view
from efp.builtin_modules import register_builtin
from efp.capture import Capture
from efp.config import Config
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
    real = pdfio.PdfReader.native_page

    def spy(self: pdfio.PdfReader, index: int) -> Any:
        opened.append(self.path)
        return real(self, index)

    monkeypatch.setattr(pdfio.PdfReader, "native_page", spy)
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
            prompt_sha256=None, thinking=None, input_tokens=1, output_tokens=1,
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


# --- review round 3 ---------------------------------------------------------------------------------------


def test_the_checked_in_config_does_not_send_court_pages_off_the_machine() -> None:
    from efp.config import load_config

    repo = Path(__file__).resolve().parents[1]
    for name in ("config.toml", "config.example.toml"):
        assert load_config(repo / name).documents.llm_transcription_fallback is False, name


def test_a_pdf_is_opened_once_for_all_its_pages(
    write_config: Callable[..., Path], conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from efp.documents import pdfio

    config = doc_config(write_config)
    add_row(conn)
    add_filing(conn, config, pdfgen.born_digital([pdfgen.LINES_A] * 6))
    opens: list[Path] = []
    real = pdfio._open

    def counting(path: Path) -> Any:
        opens.append(path)
        return real(path)

    monkeypatch.setattr(pdfio, "_open", counting)
    run_row(config, conn)
    assert len(pages(conn)) == 6
    assert len(opens) <= 2  # the page-count probe, then one handle shared by every page


def pages(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT page_no, method, quality, flagged FROM document_pages_current"
    ).fetchall()


def test_a_short_ocr_page_is_flagged_because_its_score_says_little(
    write_config: Callable[..., Path], conn: sqlite3.Connection
) -> None:
    from tests.documents_helpers import scripted, scripted_registry

    config = doc_config(write_config, 'modules = ["documents.pdfplumber", "documents.ocrmypdf"]')
    add_row(conn)
    add_filing(conn, config, pdfgen.born_digital([pdfgen.LINES_A, pdfgen.LINES_B]))
    registry = scripted_registry(
        native=scripted("documents.pdfplumber", {2: "Page 2"}),  # a short text-layer page
        ocr=scripted("documents.ocrmypdf", {1: "xqzv kjwp lmnb"}),  # short garbled OCR
    )
    run_row(config, conn, registry=registry)
    got = conn.execute(
        "SELECT page_no, method, flagged, flag_reasons FROM document_pages_current ORDER BY page_no"
    ).fetchall()
    assert got[0][1:3] == ("tesseract", 1) and "short_unverified" in json.loads(got[0][3])
    assert got[1][1:3] == ("native", 0)  # a short page read from the text layer is exact


def test_a_model_reply_cut_off_at_max_tokens_is_flagged_truncated(
    write_config: Callable[..., Path], conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = Stub(stop="max_tokens")
    monkeypatch.setattr(Capture, "llm_message", lambda self, **kw: s(self, **kw))
    config = doc_config(
        write_config,
        'modules = ["documents.pdfplumber", "documents.ocrmypdf", "documents.claude_transcribe"]',
        **SONNET,
        **{"llm_transcription_fallback = true": "llm_transcription_fallback = true"},
    )
    add_row(conn)
    add_filing(conn, config, pdfgen.image_only([pdfgen.LINES_A]))
    run_row(config, conn, registry=model_registry())
    reasons = conn.execute("SELECT flag_reasons FROM document_pages_current").fetchone()[0]
    assert json.loads(reasons) == ["model_transcribed", "truncated"]


# --- review round 4 ---------------------------------------------------------------------------------------


def test_turning_docling_always_on_changes_the_rows_hash(
    write_config: Callable[..., Path], conn: sqlite3.Connection
) -> None:
    from efp.runner import load_row

    off = doc_config(write_config)
    add_row(conn)
    add_filing(conn, off, pdfgen.born_digital([pdfgen.LINES_A]))
    first = run_row(off, conn)
    on = doc_config(write_config, **{"docling_always = false": "docling_always = true"})
    ctx, cap, registry = make_ctx(on, conn)
    try:
        assert component.input_hash(load_row(conn, 1), ctx, registry) != first.input_hash
    finally:
        cap.close()


def test_the_live_model_check_sends_the_request_shape_transcription_uses(
    write_config: Callable[..., Path], conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from efp.livecheck import _model_live

    s = Stub(text="The court accepted the plea.")
    monkeypatch.setattr(Capture, "llm_message", lambda self, **kw: s(self, **kw))
    config = doc_config(write_config, **SONNET)
    _ctx, cap, _ = make_ctx(config, conn)
    try:
        (check,) = _model_live(cap, config, conn)
        assert check.ok and "accepted the plea" in check.detail
        call = s.calls[0]
        assert call["thinking"] == {"type": "between_tools"} and call["effort"] == "low"
        assert "temperature" not in call and call["model"] == "claude-sonnet-5-5"
        assert conn.execute("SELECT purpose, ok FROM llm_calls").fetchall() == [("selftest", 1)]
        off = doc_config(
            write_config,
            **SONNET,
            **{"llm_transcription_fallback = true": "llm_transcription_fallback = false"},
        )
        (skipped,) = _model_live(cap, off, conn)
        assert skipped.ok and "skipped" in skipped.detail and len(s.calls) == 1
    finally:
        cap.close()


def test_the_sdk_accepts_the_thinking_setting_the_transcription_sends() -> None:
    """The one part of the request that can be checked offline: the installed SDK's own types name
    ``between_tools`` as a thinking type and ``low`` as an effort level."""
    import typing

    from anthropic.types.output_config_param import OutputConfigParam
    from anthropic.types.thinking_config_between_tools_param import ThinkingConfigBetweenToolsParam

    assert typing.get_args(typing.get_type_hints(ThinkingConfigBetweenToolsParam)["type"]) == (
        "between_tools",
    )
    effort = typing.get_type_hints(OutputConfigParam)["effort"]
    assert "low" in str(effort)


# --- review round 5: the cap never undercounts; one current document per row and capture ----------------


class _Raises:
    """Replaces Capture.llm_message with a failure of a chosen kind."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def __call__(self, cap: Capture, **kw: Any) -> Any:
        raise self.exc


def _fail_with(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    raiser = _Raises(exc)
    monkeypatch.setattr(Capture, "llm_message", lambda self, **kw: raiser(self, **kw))


def _model_run(write_config: Callable[..., Path], conn: sqlite3.Connection) -> Config:
    chain = (
        'modules = ["documents.pdfplumber", "documents.ocrmypdf", "documents.claude_transcribe"]'
    )
    config = doc_config(write_config, chain, **SONNET)
    add_row(conn)
    add_filing(conn, config, pdfgen.image_only([pdfgen.LINES_A]))
    run_row(config, conn, registry=model_registry())
    return config


def test_a_timeout_after_the_request_went_out_is_logged_at_the_worst_case_cost(
    write_config: Callable[..., Path], conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from efp.capture import LlmCallError

    _fail_with(monkeypatch, LlmCallError("claude-sonnet-5-5: APITimeoutError"))
    config = _model_run(write_config, conn)
    ok, cost, err = conn.execute("SELECT ok, cost_usd, error FROM llm_calls").fetchone()
    assert ok == 0 and "possibly billed" in err
    assert cost == pytest.approx(llm.worst_case_usd(config, config.documents.transcribe_max_tokens))
    assert llm.spent_this_month(conn) == pytest.approx(cost)  # the cap sees it


def test_an_answer_that_could_not_be_recorded_is_logged_at_its_real_tokens(
    write_config: Callable[..., Path], conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from efp.capture import LlmCallError

    _fail_with(
        monkeypatch,
        LlmCallError("answer received but not recorded", input_tokens=2000, output_tokens=300),
    )
    _model_run(write_config, conn)
    cost = conn.execute("SELECT cost_usd FROM llm_calls").fetchone()[0]
    assert cost == pytest.approx((2000 * 2 + 300 * 10) / 1e6)


def test_an_http_error_answer_is_not_billed(
    write_config: Callable[..., Path], conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from efp.capture import CaptureError

    _fail_with(monkeypatch, CaptureError("claude-sonnet-5-5: HTTP 529 (overloaded)"))
    _model_run(write_config, conn)
    assert conn.execute("SELECT cost_usd FROM llm_calls").fetchone()[0] == 0


def test_llm_message_raises_a_billed_error_for_a_timeout_and_for_a_failed_store(
    config: Config, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import anthropic
    import httpx2 as httpx

    from efp.capture import LlmCallError

    add_row(conn)
    _ctx, cap, _ = make_ctx(config, conn)
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    kwargs: dict[str, Any] = {
        "api_key": "k", "row_id": 1, "stage": "documents", "purpose": "p", "model": "m",
        "system": "s", "content": [{"type": "text", "text": "x"}], "max_tokens": 10,
    }  # fmt: skip

    class Timeout:
        def __init__(self, **kw: Any) -> None:
            self.messages = self

        def create(self, **kw: Any) -> None:
            raise anthropic.APITimeoutError(request=request)

        def close(self) -> None:
            pass

    monkeypatch.setattr(anthropic, "Anthropic", Timeout)
    try:
        with pytest.raises(LlmCallError) as timed_out:
            cap.llm_message(**kwargs)
        assert timed_out.value.input_tokens is None  # unknown: the caller logs the worst case

        from tests.test_documents_model import _Message

        class Answers:
            def __init__(self, **kw: Any) -> None:
                self.messages = self

            def create(self, **kw: Any) -> Any:
                return _Message()

            def close(self) -> None:
                pass

        monkeypatch.setattr(anthropic, "Anthropic", Answers)

        def broken(self: Capture, *a: Any, **k: Any) -> Any:
            raise OSError("disk full")

        monkeypatch.setattr(Capture, "store_bytes", broken)
        with pytest.raises(LlmCallError) as unrecorded:
            cap.llm_message(**kwargs)
        assert (unrecorded.value.input_tokens, unrecorded.value.output_tokens) == (321, 45)
    finally:
        cap.close()


def test_two_rows_that_point_at_one_capture_each_keep_their_own_current_document(
    write_config: Callable[..., Path], conn: sqlite3.Connection
) -> None:
    config = doc_config(write_config)
    add_row(conn, 1)
    capture_id = add_filing(conn, config, pdfgen.born_digital([pdfgen.LINES_A]))
    add_row(conn, 2)
    conn.execute(
        "INSERT INTO court_records (row_id, source_kind, document_kind, found_at, basis_json) "
        "VALUES (2, 'state_docket', 'docket', '2026-01-01T00:00:00+00:00', ?)",
        (json.dumps({"document_captures": [capture_id]}),),
    )
    conn.commit()
    run_row(config, conn, 1)
    run_row(config, conn, 2)
    run_row(config, conn, 1)  # and again: they must not take turns
    got = conn.execute(
        "SELECT row_id, current FROM documents WHERE current = 1 ORDER BY row_id"
    ).fetchall()
    assert got == [(1, 1), (2, 1)]
    assert (
        conn.execute("SELECT count(DISTINCT row_id) FROM document_pages_current").fetchone()[0] == 2
    )


def test_the_open_pdfs_are_closed_even_when_a_page_raises(
    write_config: Callable[..., Path], conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from efp.documents import pdfio
    from efp.documents.ladder import DocState

    closed: list[bool] = []
    real = DocState.close

    def spy(self: DocState) -> None:
        closed.append(True)
        real(self)

    monkeypatch.setattr(DocState, "close", spy)
    monkeypatch.setattr(
        pdfio.PdfReader, "native_page", lambda self, i: (_ for _ in ()).throw(KeyError("boom"))
    )
    config = doc_config(write_config, 'modules = ["documents.pdfplumber"]')
    add_row(conn)
    add_filing(conn, config, pdfgen.born_digital([pdfgen.LINES_A]))
    run_row(config, conn)  # the rung's error is recorded; nothing escapes
    assert closed  # and the handles were released


# --- review round 6: hash inputs, the audit link of an error call, long filings ---------------------------


def test_changing_a_setting_that_changes_output_changes_the_hash(
    write_config: Callable[..., Path], conn: sqlite3.Connection
) -> None:
    from efp.runner import load_row

    base = doc_config(write_config)
    add_row(conn)
    add_filing(conn, base, pdfgen.born_digital([pdfgen.LINES_A]))
    ctx, cap, registry = make_ctx(base, conn)
    row = load_row(conn, 1)

    def hash_for(**swap: str) -> str:
        cfg = doc_config(write_config, **swap)
        c2, k2, r2 = make_ctx(cfg, conn)
        try:
            return component.input_hash(row, c2, r2)
        finally:
            k2.close()

    try:
        same = component.input_hash(row, ctx, registry)
    finally:
        cap.close()
    assert hash_for() == same
    assert hash_for(**{"render_dpi = 200": "render_dpi = 300"}) != same
    assert hash_for(**{'ocr_language = "eng"': 'ocr_language = "fra"'}) != same
    # the fixture has the fallback on: the model that would read a page is part of the hash ...
    assert hash_for(**SONNET) != same
    # ... and with the fallback off it is not
    off = {"llm_transcription_fallback = true": "llm_transcription_fallback = false"}
    assert hash_for(**off) == hash_for(**off, **SONNET)
    assert hash_for(**off) != same


def test_an_http_error_call_is_logged_with_its_stored_request(
    write_config: Callable[..., Path], conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import anthropic
    import httpx2 as httpx

    class Refuses:
        def __init__(self, **kw: Any) -> None:
            self.messages = self

        def create(self, **kw: Any) -> None:
            request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
            raise anthropic.APIStatusError(
                "overloaded", response=httpx.Response(529, request=request), body=None
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr(anthropic, "Anthropic", Refuses)
    _model_run(write_config, conn)
    capture_id = conn.execute("SELECT capture_id FROM llm_calls").fetchone()[0]
    assert capture_id is not None
    assert conn.execute(
        "SELECT tool, http_status FROM captures WHERE capture_id = ?", (capture_id,)
    ).fetchone() == ("llm", 529)
    assert conn.execute("SELECT cost_usd FROM llm_calls").fetchone()[0] == 0  # not billed


def test_docling_and_ocr_stop_at_max_pages_and_docling_has_a_time_limit(
    write_config: Callable[..., Path], conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_documents_model import _fake_docling

    _fake_docling(monkeypatch)
    seen: dict[str, Any] = {}

    import docling.document_converter as dc

    from tests.test_documents_model import DOCLING

    class Doc:
        def export_to_dict(self) -> dict[str, Any]:
            return DOCLING

    class Conv2:
        def __init__(self, **kw: Any) -> None:
            seen["options"] = kw["format_options"]["pdf"]["pipeline_options"]

        def convert(self, path: str, **kw: Any) -> Any:
            seen["kwargs"] = kw
            import types

            return types.SimpleNamespace(document=Doc())

    monkeypatch.setattr(dc, "DocumentConverter", Conv2)
    config = doc_config(
        write_config,
        'modules = ["documents.pdfplumber", "documents.docling", "documents.ocrmypdf"]',
        **{
            "max_pages = 500": "max_pages = 2",
            "docling_timeout_seconds = 900": "docling_timeout_seconds = 77",
        },
    )
    add_row(conn)
    add_filing(conn, config, pdfgen.image_only([pdfgen.LINES_A, pdfgen.LINES_B, pdfgen.LINES_A]))
    commands: list[list[str]] = []
    from efp.documents import modules as doc_modules

    real_group = doc_modules.run_group

    def spy(command: list[str], timeout: float) -> Any:
        if "ocrmypdf" in command:
            commands.append(list(command))
        return real_group(command, timeout)

    monkeypatch.setattr(doc_modules, "run_group", spy)
    run_row(config, conn)
    assert seen["kwargs"]["page_range"] == (1, 2)
    assert seen["options"]["document_timeout"] == 77.0
    if commands:  # the OCR rung runs when tesseract is installed
        cmd = commands[0]
        assert cmd[cmd.index("--pages") + 1] == "1-2"
        assert cmd[-2].endswith("original.pdf")  # the scratch copy, never the stored file


# --- review round 7: temperature can never be sent; one path for every model call -----------------------


def test_no_code_path_can_send_a_temperature() -> None:
    """The API (version 2026-10-21) rejects it, confirmed live. The structural guarantee: the call
    function has no parameter for it, the log function has none, nothing smuggles it in the body,
    and no config key turns it on. Re-evaluate when the API version this SDK speaks changes."""
    import inspect

    import anthropic._constants as sdk

    assert "temperature" not in inspect.signature(Capture.llm_message).parameters
    assert "temperature" not in inspect.signature(llm.log_call).parameters
    assert "temperature" not in inspect.signature(llm.call_model).parameters
    repo = Path(__file__).resolve().parents[1]
    for path in sorted((repo / "efp").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "extra_body" not in text, path
        assert "send_temperature" not in text, path
    for name in ("config.toml", "config.example.toml"):
        assert "send_temperature" not in (repo / name).read_text(), name
    assert sdk.ANTHROPIC_API_VERSION == "2026-10-21", (
        "the SDK speaks a different API version: re-check whether `temperature` is accepted "
        "again (efp/llm.py), then update this pin"
    )


def test_transcribe_max_tokens_and_the_request_shape_are_in_the_hash_only_while_the_fallback_is_on(
    write_config: Callable[..., Path], conn: sqlite3.Connection
) -> None:
    from efp.runner import load_row

    add_row(conn)
    base = doc_config(write_config)
    add_filing(conn, base, pdfgen.born_digital([pdfgen.LINES_A]))
    row = load_row(conn, 1)

    def hash_for(**swap: str) -> str:
        cfg = doc_config(write_config, **swap)
        ctx, cap, registry = make_ctx(cfg, conn)
        try:
            return component.input_hash(row, ctx, registry)
        finally:
            cap.close()

    tokens = {"transcribe_max_tokens = 4096": "transcribe_max_tokens = 2048"}
    off = {"llm_transcription_fallback = true": "llm_transcription_fallback = false"}
    assert hash_for(**tokens) != hash_for()
    assert hash_for(**off, **tokens) == hash_for(**off)


def test_a_sdk_that_refuses_the_arguments_is_a_failed_call_not_a_crash(
    write_config: Callable[..., Path], conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import anthropic

    from efp.livecheck import _model_live

    class Old:
        def __init__(self, **kw: Any) -> None:
            self.messages = self

        def create(self, **kw: Any) -> None:
            raise TypeError("create() got an unexpected keyword argument 'output_config'")

        def close(self) -> None:
            pass

    monkeypatch.setattr(anthropic, "Anthropic", Old)
    config = doc_config(write_config, **SONNET)
    _ctx, cap, _ = make_ctx(config, conn)
    try:
        (check,) = _model_live(cap, config, conn)  # reported, not raised
    finally:
        cap.close()
    assert not check.ok and "request not sent" in check.detail
    assert conn.execute("SELECT ok, cost_usd FROM llm_calls").fetchall() == [(0, 0)]  # not billed


def test_any_exception_in_the_live_model_check_is_a_failed_check(
    write_config: Callable[..., Path], conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from efp.livecheck import _model_live

    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("anything at all")

    monkeypatch.setattr(llm, "call_model", boom)
    config = doc_config(write_config, **SONNET)
    _ctx, cap, _ = make_ctx(config, conn)
    try:
        (check,) = _model_live(cap, config, conn)
    finally:
        cap.close()
    assert not check.ok and "RuntimeError" in check.detail


def test_a_failure_to_store_the_api_error_answer_still_reports_the_api_error(
    config: Config, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import anthropic
    import httpx2 as httpx

    from efp.capture import LlmHttpError

    class Refuses:
        def __init__(self, **kw: Any) -> None:
            self.messages = self

        def create(self, **kw: Any) -> None:
            request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
            raise anthropic.APIStatusError(
                "overloaded", response=httpx.Response(529, request=request), body=None
            )

        def close(self) -> None:
            pass

    def broken(self: Capture, *a: Any, **k: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr(anthropic, "Anthropic", Refuses)
    monkeypatch.setattr(Capture, "store_bytes", broken)
    add_row(conn)
    _ctx, cap, _ = make_ctx(config, conn)
    try:
        with pytest.raises(LlmHttpError, match="HTTP 529") as caught:
            cap.llm_message(
                api_key="k", row_id=1, stage="documents", purpose="p", model="m", system="s",
                content=[{"type": "text", "text": "x"}], max_tokens=10,
            )  # fmt: skip
    finally:
        cap.close()
    assert caught.value.capture_id is None  # nothing stored, and the error still gets out


def test_a_timed_out_command_leaves_no_child_processes_behind(tmp_path: Path) -> None:
    import os
    import signal
    import subprocess
    import time

    from efp.documents.modules import run_group

    pidfile = tmp_path / "child.pid"
    with pytest.raises(subprocess.TimeoutExpired):
        run_group(["sh", "-c", f"sleep 60 & echo $! > {pidfile}; wait"], timeout=1)
    child = int(pidfile.read_text())
    for _ in range(20):  # the group was killed: the grandchild must be gone
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(child, signal.SIGKILL)
        raise AssertionError("the child of the timed-out command is still running")


def test_run_group_returns_the_exit_code_and_stderr(tmp_path: Path) -> None:
    from efp.documents.modules import run_group

    assert run_group(["sh", "-c", "echo oops >&2; exit 3"], timeout=10) == (3, "oops\n")


def test_a_docling_box_without_a_page_size_is_never_negative() -> None:
    from efp.documents import docling_parse

    doc = {
        "body": {"children": [{"$ref": "#/texts/0"}]},
        "texts": [
            {
                "text": "Sized nowhere",
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {
                            "l": 54,
                            "t": 700,
                            "r": 300,
                            "b": 688,
                            "coord_origin": "BOTTOMLEFT",
                        },
                    }
                ],
            }
        ],
    }  # no "pages" entry: the page size is unknown
    ((lines, size),) = docling_parse.parse(doc).values()
    word = lines[0][0]
    assert (size.width, size.height) == (0, 0)
    assert min(word.x0, word.top, word.x1, word.bottom) >= 0


# --- Docling runs in a subprocess that is killed if a page hangs ---------------------------------------------


def test_docling_runs_as_a_killable_subprocess_command(
    write_config: Callable[..., Path], conn: sqlite3.Connection
) -> None:
    from efp.documents import modules as doc_modules
    from efp.documents.ladder import DocState

    config = doc_config(write_config, **{"max_pages = 500": "max_pages = 3"})
    doc = DocState(Path("/scratch/original.pdf"), Path("/scratch"), 1, 1, page_count=9)
    cmd = doc_modules.docling_command(doc, config.documents, Path("/scratch/docling.json"))
    assert cmd[1:3] == ["-m", "efp.documents.docling_worker"]
    assert cmd[3:] == ["/scratch/original.pdf", "/scratch/docling.json", "3", "900.0"]


def test_a_docling_page_that_hangs_is_killed_and_the_ladder_carries_on(
    write_config: Callable[..., Path], conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    from efp.documents import modules as doc_modules

    monkeypatch.setattr(doc_modules, "DOCLING_KILL_GRACE_SECONDS", 0)
    monkeypatch.setattr(
        doc_modules, "docling_command", lambda doc, cfg, out: ["sh", "-c", "sleep 60"]
    )
    monkeypatch.setattr(
        doc_modules.Docling, "available", lambda self, ctx: doc_modules.Availability.available()
    )
    config = doc_config(
        write_config,
        'modules = ["documents.pdfplumber", "documents.docling"]',
        **{"docling_timeout_seconds = 900": "docling_timeout_seconds = 1"},
    )
    add_row(conn)
    add_filing(conn, config, pdfgen.image_only([pdfgen.LINES_A]))  # pdfplumber misses: docling runs
    started = time.monotonic()
    result = run_row(config, conn)
    assert time.monotonic() - started < 30  # not 60: the hung worker was killed
    assert result.status == "done"
    modules = json.loads(conn.execute("SELECT methods_json FROM documents").fetchone()[0])
    assert "hung and was killed" in modules["document"]["docling_error"]
    assert conn.execute("SELECT method, flagged FROM document_pages_current").fetchall() == [
        ("failed", 1)
    ]  # nothing else could read the scan, and it is flagged for review


def test_a_failing_docling_worker_is_an_error_with_its_reason(tmp_path: Path) -> None:
    from efp.documents import docling_worker

    assert (
        docling_worker.main([str(tmp_path / "missing.pdf"), str(tmp_path / "o.json"), "1", "5"])
        == 1
    )
    assert docling_worker.main(["only-one-argument"]) == 1


# --- review round 8: OCR time scales with pages; dropped PDFs retire; the cap lives in call_model -----------


def test_the_ocr_limit_is_a_base_plus_an_allowance_per_page_read(
    write_config: Callable[..., Path], conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from efp.documents import modules as doc_modules

    seen: list[float] = []

    def fake(command: list[str], timeout: float) -> tuple[int, str]:
        seen.append(timeout)
        return 1, "stopped for the test"

    monkeypatch.setattr(doc_modules, "run_group", fake)
    monkeypatch.setattr(
        doc_modules.Ocrmypdf, "available", lambda self, ctx: doc_modules.Availability.available()
    )
    config = doc_config(
        write_config,
        'modules = ["documents.pdfplumber", "documents.ocrmypdf"]',
        **{
            "ocr_timeout_seconds = 300": "ocr_timeout_seconds = 100",
            "ocr_seconds_per_page = 30": "ocr_seconds_per_page = 7",
            "max_pages = 500": "max_pages = 2",
        },
    )
    add_row(conn)
    add_filing(conn, config, pdfgen.image_only([pdfgen.LINES_A, pdfgen.LINES_B, pdfgen.LINES_A]))
    run_row(config, conn)
    assert seen == [100 + 7 * 2]  # two pages are read (max_pages), not the three the PDF has


def test_raising_an_ocr_limit_makes_recheck_revisit_the_rows(
    write_config: Callable[..., Path], conn: sqlite3.Connection
) -> None:
    from efp.runner import load_row

    add_row(conn)
    base = doc_config(write_config)
    add_filing(conn, base, pdfgen.born_digital([pdfgen.LINES_A]))
    row = load_row(conn, 1)

    def hash_for(**swap: str) -> str:
        ctx, cap, registry = make_ctx(doc_config(write_config, **swap), conn)
        try:
            return component.input_hash(row, ctx, registry)
        finally:
            cap.close()

    same = hash_for()
    assert hash_for(**{"ocr_timeout_seconds = 300": "ocr_timeout_seconds = 600"}) != same
    assert hash_for(**{"ocr_seconds_per_page = 30": "ocr_seconds_per_page = 60"}) != same


def test_a_pdf_the_row_no_longer_points_to_stops_being_current(
    write_config: Callable[..., Path], conn: sqlite3.Connection
) -> None:
    config = doc_config(write_config)
    add_row(conn)
    add_filing(conn, config, pdfgen.born_digital([pdfgen.LINES_A]))
    add_filing(conn, config, pdfgen.born_digital([pdfgen.LINES_B]))
    first = run_row(config, conn)
    assert len(first.summary_json["documents"]) == 2
    assert conn.execute("SELECT count(*) FROM document_pages_extractable").fetchone()[0] == 2
    # the court stage supersedes the record that pointed at the second PDF
    conn.execute("UPDATE court_records SET current = 0 WHERE record_id = 2")
    conn.commit()
    again = run_row(config, conn)
    assert again.summary_json["retired_documents"] == 1
    assert conn.execute(
        "SELECT capture_id, current FROM documents ORDER BY document_id"
    ).fetchall() == [
        (1, 1),
        (2, 0),
    ]
    assert (
        conn.execute(
            "SELECT count(DISTINCT capture_id) FROM document_pages_extractable"
        ).fetchone()[0]
        == 1
    )
    # and when no PDF is left at all, none stays current
    conn.execute("UPDATE court_records SET current = 0")
    conn.commit()
    last = run_row(config, conn)
    assert last.summary_json["documents_status"] == "no_documents"
    assert conn.execute("SELECT count(*) FROM documents WHERE current = 1").fetchone()[0] == 0


def test_call_model_refuses_a_call_the_cap_cannot_afford_without_calling_the_api(
    write_config: Callable[..., Path], conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = Stub()
    monkeypatch.setattr(Capture, "llm_message", lambda self, **kw: s(self, **kw))
    config = doc_config(
        write_config, **SONNET, **{"max_monthly_usd = 1.0": "max_monthly_usd = 0.01"}
    )
    _ctx, cap, _ = make_ctx(config, conn)
    try:
        done = llm.call_model(
            cap, conn, config, api_key="k", row_id=None, stage="x", purpose="p", system="s",
            content=[{"type": "text", "text": "x"}], max_tokens=4096, prompt_sha256=None,
        )  # fmt: skip
    finally:
        cap.close()
    assert done.reply is None and "max_monthly_usd" in (done.error or "") and done.cost == 0.0
    assert s.calls == [] and conn.execute("SELECT count(*) FROM llm_calls").fetchone()[0] == 0

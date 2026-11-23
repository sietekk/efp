"""Model transcription (stubbed: no live call), Docling and PaddleOCR parsing, status, capture."""

from __future__ import annotations

import hashlib
import importlib.machinery
import json
import sqlite3
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from efp import status as status_view
from efp.capture import Capture, CaptureError, LlmResult
from efp.config import Config
from efp.documents import docling_parse, llm
from efp.documents.modules import PROMPT, parse_paddle, thinking_for
from tests import pdfgen
from tests.conftest import SENTINELS
from tests.documents_helpers import (
    CLEAN,
    FULL_CHAIN,
    add_filing,
    add_row,
    doc_config,
    make_ctx,
    model_registry,
    run_row,
)

CHAIN = 'modules = ["documents.pdfplumber", "documents.ocrmypdf", "documents.claude_transcribe"]'
SONNET = {'transcription_model = "test-model"': 'transcription_model = "claude-sonnet-5-5"'}
IMAGE_PDF = pdfgen.image_only([pdfgen.LINES_A])


class Stub:
    """Replaces Capture.llm_message: records the call, stores a capture like the real one does."""

    def __init__(self, text: str = CLEAN, *, fail: bool = False, stop: str = "end_turn") -> None:
        self.calls: list[dict[str, Any]] = []
        self.text, self.fail, self.stop = text, fail, stop

    def __call__(self, cap: Capture, **kw: Any) -> LlmResult:
        self.calls.append(kw)
        if self.fail:
            raise CaptureError("claude-sonnet-5-5: HTTP 529 (overloaded)")
        record = cap.store_bytes(
            b'{"stub": true}', row_id=kw["row_id"], stage="documents",
            url_requested="https://api.anthropic.com/v1/messages", tool="llm", tool_version="stub",
        )  # fmt: skip
        return LlmResult(
            self.text, kw["model"], 1500, 400, "req_stub", self.stop, record.capture_id
        )


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> Stub:
    s = Stub()
    monkeypatch.setattr(Capture, "llm_message", lambda self, **kw: s(self, **kw))
    return s


def setup(write_config: Callable[..., Path], conn: sqlite3.Connection, **extra: str) -> Config:
    config = doc_config(write_config, CHAIN, **{**SONNET, **extra})
    add_row(conn)
    add_filing(conn, config, IMAGE_PDF)
    return config


def test_a_page_both_ocr_paths_failed_is_transcribed_scored_and_flagged_for_review(
    write_config: Callable[..., Path], conn: sqlite3.Connection, stub: Stub
) -> None:
    config = setup(write_config, conn)
    result = run_row(config, conn, registry=model_registry())
    (page,) = conn.execute(
        "SELECT page_no, method, quality, flagged, flag_reasons FROM document_pages_current"
    ).fetchall()
    assert page[1] == "model" and page[2] >= 0.85 and page[3] == 1  # good text, still flagged
    assert json.loads(page[4]) == ["model_transcribed"]
    assert conn.execute("SELECT count(*) FROM document_pages_extractable").fetchone()[0] == 0
    doc = conn.execute("SELECT ocr_applied, ocr_engine FROM documents").fetchone()
    assert doc == (1, "model:claude-sonnet-5-5")
    assert result.summary_json["pages_by_method"] == {"model": 1}
    # the call: model id, prompt hash, no temperature, thinking off, the key from the env file
    (call,) = stub.calls
    assert call["model"] == "claude-sonnet-5-5" and call["temperature"] is None
    assert call["thinking"] == {"type": "between_tools"} and call["effort"] == "low"
    assert call["api_key"] == SENTINELS["ANTHROPIC_API_KEY"]
    assert call["content"][0]["type"] == "image" and call["system"] == PROMPT.read_text()
    row = conn.execute(
        "SELECT model_id, prompt_sha256, temperature, temperature_note, thinking, input_tokens, "
        "output_tokens, cost_usd, ok, purpose, stage FROM llm_calls"
    ).fetchone()
    assert row[0] == "claude-sonnet-5-5"
    assert row[1] == hashlib.sha256(PROMPT.read_bytes()).hexdigest()
    assert row[2] is None and "reject temperature" in row[3] and row[4] == "between_tools"
    assert (row[5], row[6]) == (1500, 400) and row[7] == pytest.approx((1500 * 2 + 400 * 10) / 1e6)
    assert row[8] == 1 and row[9] == "transcribe_page" and row[10] == "documents"


def test_a_model_that_takes_temperature_gets_the_configured_one_when_enabled(
    write_config: Callable[..., Path], conn: sqlite3.Connection, stub: Stub
) -> None:
    config = setup(write_config, conn, **{"send_temperature = false": "send_temperature = true"})
    run_row(config, conn, registry=model_registry())
    assert stub.calls[0]["temperature"] == 0.0
    assert conn.execute("SELECT temperature, temperature_note FROM llm_calls").fetchone() == (
        0.0,
        None,
    )


def test_the_monthly_cap_stops_transcription_and_the_row_is_revisited(
    write_config: Callable[..., Path], conn: sqlite3.Connection, stub: Stub
) -> None:
    config = setup(write_config, conn, **{"max_monthly_usd = 1.0": "max_monthly_usd = 0.01"})
    result = run_row(config, conn, registry=model_registry())
    assert stub.calls == []  # not even one call: the worst case would pass the cap
    assert conn.execute("SELECT count(*) FROM llm_calls").fetchone()[0] == 0
    (page,) = conn.execute("SELECT method, flagged FROM document_pages_current").fetchall()
    assert page == ("failed", 1)
    assert result.input_hash.endswith(":budget")
    modules = json.loads(conn.execute("SELECT methods_json FROM documents").fetchone()[0])
    skipped = [m for m in modules["pages"][0]["modules"] if m["name"].endswith("claude_transcribe")]
    assert skipped[0]["status"] == "skipped" and "max_monthly_usd" in skipped[0]["reason"]


def test_spend_already_logged_counts_against_the_cap(
    write_config: Callable[..., Path], conn: sqlite3.Connection, stub: Stub
) -> None:
    config = setup(write_config, conn)  # cap 1.0
    llm.log_call(
        conn, row_id=None, stage="documents", purpose="transcribe_page", capture_id=None,
        model_id="m", prompt_sha256=None, temperature=None, thinking=None, input_tokens=1,
        output_tokens=1, cost=0.99, request_id=None, ok=True,
    )  # fmt: skip
    run_row(config, conn, registry=model_registry())
    assert stub.calls == []
    assert llm.spent_this_month(conn) == pytest.approx(0.99)


def test_a_failed_call_is_logged_costs_nothing_and_leaves_the_page_failed(
    write_config: Callable[..., Path], conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = Stub(fail=True)
    monkeypatch.setattr(Capture, "llm_message", lambda self, **kw: s(self, **kw))
    config = setup(write_config, conn)
    run_row(config, conn, registry=model_registry())
    assert conn.execute("SELECT method, flagged FROM document_pages_current").fetchall() == [
        ("failed", 1)
    ]
    row = conn.execute("SELECT ok, cost_usd, error FROM llm_calls").fetchone()
    assert row[0] == 0 and row[1] == 0 and "529" in row[2]


def test_a_refusal_is_logged_and_gives_no_text(
    write_config: Callable[..., Path], conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = Stub(text="", stop="refusal")
    monkeypatch.setattr(Capture, "llm_message", lambda self, **kw: s(self, **kw))
    config = setup(write_config, conn)
    run_row(config, conn, registry=model_registry())
    assert conn.execute("SELECT method FROM document_pages_current").fetchall() == [("failed",)]
    assert conn.execute("SELECT ok, error FROM llm_calls").fetchone() == (0, "refusal")


def test_no_key_or_no_fallback_flag_means_no_transcription(
    write_config: Callable[..., Path], conn: sqlite3.Connection, stub: Stub, env_file: Path
) -> None:
    config = setup(
        write_config,
        conn,
        **{"llm_transcription_fallback = true": "llm_transcription_fallback = false"},
    )
    run_row(config, conn, registry=model_registry())
    assert stub.calls == []
    reasons = json.loads(conn.execute("SELECT methods_json FROM documents").fetchone()[0])
    why = [m["reason"] for m in reasons["pages"][0]["modules"] if m["name"].endswith("transcribe")]
    assert why == ["documents.llm_transcription_fallback is false"]


def test_the_cost_is_computed_from_config_prices(config: Config) -> None:
    assert llm.cost_usd(config, 1_000_000, 1_000_000) == pytest.approx(12.0)
    assert llm.worst_case_usd(config, 4096) == pytest.approx(
        (llm.PROMPT_TOKENS_ESTIMATE * 2 + 4096 * 10) / 1e6
    )


def test_thinking_is_turned_off_only_for_the_model_that_needs_it() -> None:
    assert thinking_for("claude-sonnet-5-5") == ({"type": "between_tools"}, "low")
    assert thinking_for("claude-haiku-5-5") == (None, None)


# --- Capture.llm_message --------------------------------------------------------------------------------


class _Block:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _Message:
    content = (_Block("hello\nworld"),)
    model = "claude-sonnet-5-5"
    stop_reason = "end_turn"
    _request_id = "req_123"

    class usage:
        input_tokens = 321
        output_tokens = 45

    def to_json(self) -> str:
        return json.dumps({"id": "msg_1", "content": [{"type": "text", "text": "hello\nworld"}]})


def test_llm_message_goes_through_the_capture_layer_and_never_stores_the_key_or_the_image(
    config: Config, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import anthropic

    seen: dict[str, Any] = {}

    class FakeClient:
        def __init__(self, **kw: Any) -> None:
            seen["init"] = kw
            self.messages = self

        def create(self, **kw: Any) -> _Message:
            seen["args"] = kw
            return _Message()

        def close(self) -> None:
            seen["closed"] = True

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    add_row(conn)
    _ctx, cap, _ = make_ctx(config, conn)
    image = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
    }
    try:
        got = cap.llm_message(
            api_key="sk-test-not-real", row_id=1, stage="documents", purpose="transcribe_page",  # pragma: allowlist secret
            model="claude-sonnet-5-5", system="sys", content=[image, {"type": "text", "text": "go"}],
            max_tokens=100, thinking={"type": "between_tools"}, effort="low",
        )  # fmt: skip
    finally:
        cap.close()
    assert got.text == "hello\nworld" and (got.input_tokens, got.output_tokens) == (321, 45)
    assert got.request_id == "req_123" and seen["closed"]
    assert "temperature" not in seen["args"] and seen["args"]["output_config"] == {"effort": "low"}
    assert seen["init"]["api_key"] == "sk-test-not-real"  # pragma: allowlist secret
    body = conn.execute("SELECT content_sha256, tool, url_requested FROM captures").fetchone()
    assert body[1] == "llm" and body[2] == "https://api.anthropic.com/v1/messages"
    stored = (cap.config.paths.store / body[0][:2] / body[0]).read_text()
    assert "sk-test-not-real" not in stored and "QUJD" not in stored  # no key, no image bytes
    assert hashlib.sha256(b"QUJD").hexdigest() in stored  # the image is named by its hash


def test_an_api_error_is_a_capture_error_and_is_stored(
    config: Config, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import anthropic
    import httpx2 as httpx

    class Boom:
        def __init__(self, **kw: Any) -> None:
            self.messages = self

        def create(self, **kw: Any) -> None:
            request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
            raise anthropic.APIStatusError(
                "overloaded", response=httpx.Response(529, request=request), body=None
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr(anthropic, "Anthropic", Boom)
    add_row(conn)
    _ctx, cap, _ = make_ctx(config, conn)
    try:
        with pytest.raises(CaptureError, match="HTTP 529"):
            cap.llm_message(
                api_key="k", row_id=1, stage="documents", purpose="p", model="m", system="s",
                content=[{"type": "text", "text": "x"}], max_tokens=10,
            )  # fmt: skip
    finally:
        cap.close()
    assert conn.execute("SELECT http_status FROM captures WHERE tool = 'llm'").fetchone() == (529,)


# --- parsers for tools that are not installed here ----------------------------------------------------------


DOCLING = {
    "pages": {"1": {"page_no": 1, "size": {"width": 612, "height": 792}}},
    "body": {"children": [{"$ref": "#/texts/1"}, {"$ref": "#/tables/0"}, {"$ref": "#/texts/0"}]},
    "texts": [
        {"label": "text", "text": "Closing paragraph.", "prov": [
            {"page_no": 1, "bbox": {"l": 54, "t": 200, "r": 300, "b": 188, "coord_origin": "BOTTOMLEFT"}}]},
        {"label": "title", "text": "IN THE COURT OF EXAMPLE", "prov": [
            {"page_no": 1, "bbox": {"l": 54, "t": 740, "r": 400, "b": 725, "coord_origin": "BOTTOMLEFT"}}]},
    ],
    "tables": [
        {"prov": [{"page_no": 1, "bbox": {"l": 54, "t": 600, "r": 400, "b": 560, "coord_origin": "BOTTOMLEFT"}}],
         "data": {"table_cells": [
            {"text": "Count", "start_row_offset_idx": 0, "start_col_offset_idx": 0},
            {"text": "Plea", "start_row_offset_idx": 0, "start_col_offset_idx": 1},
            {"text": "One", "start_row_offset_idx": 1, "start_col_offset_idx": 0},
            {"text": "Guilty", "start_row_offset_idx": 1, "start_col_offset_idx": 1}]}},
    ],
}  # fmt: skip


def test_docling_json_is_read_in_reading_order_with_boxes_from_the_top_left() -> None:
    parsed = docling_parse.parse(DOCLING)
    lines, size = parsed[1]
    assert (size.width, size.height) == (612, 792)
    assert [" ".join(w.text for w in ln) for ln in lines] == [
        "IN THE COURT OF EXAMPLE", "Count Plea", "One Guilty", "Closing paragraph.",
    ]  # fmt: skip
    title = lines[0][0]
    assert (title.top, title.bottom) == (792 - 740, 792 - 725)  # BOTTOMLEFT -> top-left origin


def test_paddle_output_becomes_lines_of_words_in_points() -> None:
    result = [{
        "rec_texts": ["second line here", "First line"],
        "rec_scores": [0.9, 0.99],
        "rec_boxes": [[100, 300, 400, 340], [100, 100, 300, 140]],
    }]  # fmt: skip
    lines, confidences = parse_paddle(result, 0.5)  # 144 dpi -> 72
    assert [[w.text for w in ln] for ln in lines] == [["First", "line"], ["second", "line", "here"]]
    assert confidences == [0.99, 0.9]
    first = lines[0]
    assert first[0].x0 == 50 and first[0].top == 50 and first[-1].x1 == pytest.approx(150)
    assert first[0].conf == 0.99


def _fake_docling(monkeypatch: pytest.MonkeyPatch) -> None:
    def module(name: str, **attrs: Any) -> types.ModuleType:
        mod = types.ModuleType(name)
        mod.__spec__ = importlib.machinery.ModuleSpec(name, None)
        mod.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, mod)
        return mod

    class Doc:
        def export_to_dict(self) -> dict[str, Any]:
            return DOCLING

    class Converter:
        def __init__(self, **kw: Any) -> None:
            pass

        def convert(self, path: str, **kw: Any) -> Any:
            return types.SimpleNamespace(document=Doc())

    module("docling")
    module("docling.datamodel")
    module("docling.datamodel.base_models", InputFormat=types.SimpleNamespace(PDF="pdf"))
    module("docling.datamodel.pipeline_options", PdfPipelineOptions=lambda **kw: kw)
    module(
        "docling.document_converter", DocumentConverter=Converter, PdfFormatOption=lambda **kw: kw
    )


def test_docling_reads_a_page_pdfplumber_could_not_and_its_json_is_stored(
    write_config: Callable[..., Path], conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_docling(monkeypatch)
    config = doc_config(write_config, 'modules = ["documents.pdfplumber", "documents.docling"]')
    add_row(conn)
    add_filing(conn, config, IMAGE_PDF)
    run_row(config, conn)
    (page,) = conn.execute("SELECT method, quality FROM document_pages_current").fetchall()
    assert page[0] == "docling"
    doc = conn.execute("SELECT docling_path FROM documents").fetchone()
    assert doc[0].startswith("store/")
    sha = doc[0].rsplit("/", 1)[1]
    assert json.loads((config.paths.store / sha[:2] / sha).read_text()) == DOCLING
    assert conn.execute("SELECT count(*) FROM captures WHERE tool = 'docling'").fetchone()[0] == 1
    layout_sha = conn.execute(
        "SELECT c.content_sha256 FROM documents d JOIN captures c ON c.capture_id = d.layout_capture_id"
    ).fetchone()[0]
    units = json.loads((config.paths.store / layout_sha[:2] / layout_sha).read_text())["units"]
    assert {u["bbox_source"] for u in units} == {"item"}


# --- status ---------------------------------------------------------------------------------------------


def test_status_reports_methods_quality_bands_flags_and_spend(
    write_config: Callable[..., Path], conn: sqlite3.Connection, stub: Stub
) -> None:
    config = setup(write_config, conn)
    add_filing(conn, config, pdfgen.born_digital([pdfgen.LINES_A, pdfgen.LINES_B]))
    run_row(config, conn, registry=model_registry())
    report = status_view.build_report(conn, config)
    d = report.documents
    assert d["pages_by_method"] == {"native": 2, "model": 1} and d["flagged_pages"] == 1
    assert d["documents"] == 2 and d["documents_by_quality_min"]["0.95 to 1.00"] == 2
    assert d["transcription_spend_usd"] > 0
    text = status_view.render_md(report)
    assert (
        "### Documents" in text and "| model | 1 |" in text and "Model transcription spend" in text
    )
    assert json.loads(status_view.render_json(report))["documents"]["flagged_pages"] == 1


def test_status_has_no_documents_section_before_the_stage_has_run(
    config: Config, conn: sqlite3.Connection
) -> None:
    assert status_view.build_report(conn, config).documents == {}
    assert "### Documents" not in status_view.render_md(status_view.build_report(conn, config))


def test_the_full_default_chain_is_what_the_helpers_default_to() -> None:
    assert "documents.claude_transcribe" in FULL_CHAIN

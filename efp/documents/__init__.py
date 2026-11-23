"""documents component (build 10): every court PDF as text with page, box and character provenance.

``run`` finds the PDFs a row's court records point to (RECAP filings, docket filings, PIN reports,
manual imports), and for each one walks every page down the ladder in ``[stages.documents]``:
born-digital text, Docling layout, OCRmyPDF, PaddleOCR, then model transcription. Each page ends
with a method, a quality score in [0, 1] and a flag; the canonical text, the word map (page, box,
character span) and the page rows are written, and the original PDF is never changed.

A page below ``documents.ocr_quality_threshold``, or one a model transcribed, is flagged: its text
is kept but it is not in ``document_pages_extractable``, so extraction never reads it unflagged.
See docs/documents.md for the score, the canonical text and the storage layout.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from efp import llm
from efp import modules as chain
from efp.documents import canonical, pdfio
from efp.documents.ladder import DocState, PageState, PageTask
from efp.documents.modules import PROMPT, STAGE, docling_first, transcribe_configured
from efp.documents.quality import SCORE_VERSION
from efp.modules import REGISTRY, ModuleResult, Registry
from efp.runner import input_hash as hash_values
from efp.store import StoreError, body_relpath, config_store_path, read_verified, sha256_hex
from efp.types import Context, Row, StageResult

BUILD = "10"
IMPLEMENTED = True
TOOL_VERSION = "1"

log = logging.getLogger(__name__)


def gate(row: Row) -> bool | str:
    """Every row enters: a row with no PDF is recorded as ``no_documents``, not skipped."""
    return True


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class PdfInput:
    capture_id: int
    record_id: int | None
    sha256: str
    path: Path


@dataclass
class PageOut:
    page_no: int
    method: str
    quality: float | None
    flagged: bool
    reasons: list[str]
    parts: dict[str, float]
    chars: int
    short: bool
    modules: list[dict[str, str]]
    text: canonical.PageText


@dataclass
class DocOut:
    pdf: PdfInput
    pages: list[PageOut] = field(default_factory=list)
    unreadable: str | None = None
    state: DocState | None = None
    not_read: int = 0
    ocr_bytes: bytes | None = None


# --- inputs -------------------------------------------------------------------------------------


def pdf_inputs(conn: sqlite3.Connection, ctx: Context, row_id: int) -> list[PdfInput]:
    """The PDFs a row has: captures its current court records name (the record's own capture, the
    filings a docket lists in ``basis.document_captures``) and manual imports, kept when the body
    starts like a PDF. A body that is not in the store is not skipped silently: it is reported by
    the caller as unreadable."""
    found: dict[int, int | None] = {}
    for record_id, capture_id, basis in conn.execute(
        "SELECT record_id, capture_id, basis_json FROM court_records_current WHERE row_id = ?",
        (row_id,),
    ):
        if capture_id:
            found.setdefault(int(capture_id), int(record_id))
        try:
            listed = (json.loads(basis) if basis else {}).get("document_captures") or []
        except (ValueError, AttributeError):
            listed = []
        for item in listed:
            if isinstance(item, int):
                found.setdefault(item, int(record_id))
    for (capture_id,) in conn.execute(
        "SELECT capture_id FROM captures WHERE row_id = ? AND manual_source IS NOT NULL", (row_id,)
    ):
        found.setdefault(int(capture_id), None)
    out: list[PdfInput] = []
    for capture_id, record_id in sorted(found.items()):
        got = conn.execute(
            "SELECT content_sha256, content_type FROM captures WHERE capture_id = ?", (capture_id,)
        ).fetchone()
        if got is None:
            continue
        sha, content_type = str(got[0]), str(got[1] or "")
        path = config_store_path(sha, ctx.config)
        try:
            with path.open("rb") as handle:
                head = handle.read(1024)
        except OSError:
            if "pdf" in content_type.lower():
                out.append(PdfInput(capture_id, record_id, sha, path))  # reported as unreadable
            continue
        if pdfio.is_pdf(head):
            out.append(PdfInput(capture_id, record_id, sha, path))
    return out


# --- one page, one document ---------------------------------------------------------------------


def settle(task: PageTask, records: list[dict[str, Any]], threshold: float) -> PageOut:
    """Turn what the ladder found for a page into its method, score, flag and canonical lines."""
    state: PageState = task.p
    modules = [{"name": r["name"], "status": r["status"], "reason": r["reason"]} for r in records]
    page = task.page_no
    if state.blank:
        return PageOut(
            page, "blank", None, False, [], {}, 0, False, modules,
            canonical.PageText(page, "blank"),
        )  # fmt: skip
    best = state.best()
    if best is None:
        return PageOut(
            page, "failed", None, True, ["no_text"], {}, 0, False, modules,
            canonical.PageText(page, "failed"),
        )  # fmt: skip
    reasons: list[str] = []
    if best.score.score < threshold:
        reasons.append("below_threshold")
    if best.method == "model":
        reasons.append("model_transcribed")  # model errors are silent: always for review
    reasons.extend(best.flags)
    if best.score.short and best.method in ("tesseract", "paddleocr"):
        # a score from a few characters says little: letters of garbled OCR look clean
        reasons.append("short_unverified")
    text = canonical.PageText(
        page,
        best.method,
        best.lines,
        best.size.width,
        best.size.height,
        best.bbox_source,
    )
    return PageOut(
        page, best.method, best.score.score, bool(reasons), reasons, best.score.parts,
        best.score.chars, best.score.short, modules, text,
    )  # fmt: skip


def process_pdf(
    ctx: Context, registry: Registry, row: Row, pdf: PdfInput, threshold: float
) -> DocOut:
    out = DocOut(pdf)
    cfg = ctx.config.documents
    try:
        # the evidentiary bytes: re-hashed on every read, and every tool works on a copy of them
        original = read_verified(ctx.config.paths.store, pdf.sha256)
    except (StoreError, OSError) as exc:
        out.unreadable = f"stored body not verified: {type(exc).__name__}"
        return out
    chain_cfg = chain.chains_for(ctx.config, STAGE)[0]

    def meets(result: ModuleResult) -> bool:
        return result.quality is not None and result.quality >= threshold

    with tempfile.TemporaryDirectory(prefix="efp-doc-", ignore_cleanup_errors=True) as scratch:
        copy = Path(scratch) / "original.pdf"
        copy.write_bytes(original)
        try:
            count = pdfio.page_count(copy)  # (a throwaway open: the pages below share one)
        except pdfio.PdfError as exc:
            out.unreadable = str(exc)
            return out
        doc = DocState(copy, Path(scratch), row.row_id, pdf.capture_id, count)
        out.state = doc
        if cfg.docling_always:
            docling_first(ctx, registry, doc)
        limit = min(count, cfg.max_pages)
        try:
            for index in range(count):
                page_no = index + 1
                if index >= limit:
                    out.pages.append(
                        PageOut(
                            page_no,
                            "failed",
                            None,
                            True,
                            ["not_read"],
                            {},
                            0,
                            False,
                            [],
                            canonical.PageText(page_no, "failed"),
                        )
                    )
                    out.not_read += 1
                    continue
                task = PageTask(
                    row_id=row.row_id,
                    year=row.year,
                    state=row.state,
                    source=row.source,
                    page_no=page_no,
                    doc=doc,
                    ladder=PageState(),
                )
                run = chain.run_chain(chain_cfg, task, ctx, registry, quality_bar=meets)
                settled = settle(task, run.records, threshold)
                if settled.method in ("tesseract", "paddleocr"):
                    doc.engines.add(settled.method)
                out.pages.append(settled)
        finally:
            doc.close()  # the open PDFs (original and OCR copy), whatever happened above
        # the OCR output lives in the scratch directory: read it before that is removed
        if doc.ocr_path is not None and any(p.method == "tesseract" for p in out.pages):
            out.ocr_bytes = doc.ocr_path.read_bytes()
    return out


# --- writing a document -------------------------------------------------------------------------

METHOD_NAMES = {"model": "model"}


def _store(
    ctx: Context,
    row_id: int,
    *,
    data: bytes,
    url: str,
    tool: str,
    content_type: str,
    parent: int,
    version: str = "1",
    extra: dict[str, Any] | None = None,
) -> int:
    """Keep a derived artifact as its own capture, once: a re-run that makes the same bytes finds
    the capture it made before."""
    if ctx.db is None or ctx.capture is None:
        raise RuntimeError("the documents stage needs the database and the capture client")
    digest = sha256_hex(data)
    found = ctx.db.execute(
        "SELECT capture_id FROM captures WHERE row_id = ? AND tool = ? AND content_sha256 = ? "
        "AND url_requested = ? ORDER BY capture_id LIMIT 1",
        (row_id, tool, digest, url),
    ).fetchone()
    if found is not None:
        return int(found[0])
    record = ctx.capture.store_bytes(
        data,
        row_id=row_id,
        stage=STAGE,
        url_requested=url,
        url_final=url,
        tool=tool,
        tool_version=version,
        content_type=content_type,
        headers={"parent_capture_id": parent, **(extra or {})},
    )
    return int(record.capture_id)


def _quality_json(out: DocOut, canon: canonical.Canonical, threshold: float) -> dict[str, Any]:
    pages = []
    for p in out.pages:
        start, end = canon.spans.get(p.page_no, (0, 0))
        pages.append(
            {
                "page": p.page_no,
                "method": p.method,
                "quality": p.quality,
                "parts": p.parts,
                "flags": p.reasons,
                "short": p.short,
                "chars": p.chars,
                "char_start": start,
                "char_end": end,
            }
        )
    return {
        "score_version": SCORE_VERSION,
        "canonical_version": canonical.CANONICAL_VERSION,
        "threshold": threshold,
        "not_read_pages": out.not_read,
        "pages": pages,
    }


def write_document(ctx: Context, row: Row, out: DocOut, threshold: float) -> int | None:
    """Write one document's rows. Returns the new ``document_id``, or None when the latest run
    already holds the same text and the same page results (nothing is duplicated by a re-run).

    The derived captures are stored first (each commits on its own and is content-addressed, so a
    failure after them leaves only unreferenced captures). The ``documents`` and ``document_pages``
    rows, and the flip of the previous row to ``current = 0``, are one transaction: either all of
    them or none."""
    if ctx.db is None:
        raise RuntimeError("the documents stage needs the database")
    try:
        return _write_document(ctx, row, out, threshold)
    except Exception:
        ctx.db.rollback()
        raise


def _write_document(ctx: Context, row: Row, out: DocOut, threshold: float) -> int | None:
    conn, pdf = ctx.db, out.pdf
    if conn is None:
        raise RuntimeError("the documents stage needs the database")
    old = conn.execute(
        "SELECT document_id, text_sha256, quality_json FROM documents "
        "WHERE row_id = ? AND capture_id = ? AND current = 1",
        (row.row_id, pdf.capture_id),
    ).fetchone()
    if out.unreadable is not None:
        quality = json.dumps({"unreadable": out.unreadable}, sort_keys=True)
        if old is not None and old[2] == quality:
            return None
        if old is not None:
            conn.execute("UPDATE documents SET current = 0 WHERE document_id = ?", (old[0],))
        cursor = conn.execute(
            "INSERT INTO documents (row_id, record_id, capture_id, pre_ocr_sha256, ocr_applied, "
            "quality_json, extracted_at, canonical_version, current) VALUES (?,?,?,?,0,?,?,?,1)",
            (row.row_id, pdf.record_id, pdf.capture_id, pdf.sha256, quality, _now(),
             canonical.CANONICAL_VERSION),
        )  # fmt: skip
        new_id = int(cursor.lastrowid or 0)
        conn.commit()
        return new_id
    pages = [p.text for p in out.pages]
    canon = canonical.build(pages)
    quality_doc = _quality_json(out, canon, threshold)
    quality_text = json.dumps(quality_doc, sort_keys=True)
    if old is not None and old[1] == canon.sha256 and old[2] == quality_text:
        return None
    text_capture = _store(
        ctx,
        row.row_id,
        data=canon.text.encode("utf-8"),
        url=f"efp:documents/{pdf.capture_id}/text",
        tool="documents",
        content_type="text/plain; charset=utf-8",
        parent=pdf.capture_id,
        extra={"canonical_version": canonical.CANONICAL_VERSION, "text_sha256": canon.sha256},
    )
    layout = canonical.layout_json(canon, pages)
    layout_capture = _store(
        ctx, row.row_id, data=json.dumps(layout, sort_keys=True).encode("utf-8"),
        url=f"efp:documents/{pdf.capture_id}/layout", tool="documents_layout",
        content_type="application/json", parent=pdf.capture_id,
        extra={"canonical_version": canonical.CANONICAL_VERSION},
    )  # fmt: skip
    post_sha = None
    if out.ocr_bytes is not None:
        _store(
            ctx, row.row_id, data=out.ocr_bytes, url=f"efp:documents/{pdf.capture_id}/ocr",
            tool="ocrmypdf", content_type="application/pdf", parent=pdf.capture_id,
        )  # fmt: skip
        post_sha = sha256_hex(out.ocr_bytes)
    docling_path = None
    doc = out.state
    if doc is not None and doc.docling_dict is not None:
        blob = json.dumps(doc.docling_dict, sort_keys=True).encode("utf-8")
        _store(
            ctx, row.row_id, data=blob, url=f"efp:documents/{pdf.capture_id}/docling",
            tool="docling", content_type="application/json", parent=pdf.capture_id,
        )  # fmt: skip
        docling_path = body_relpath(sha256_hex(blob))
    methods_used = {p.method for p in out.pages}
    engines = sorted(m for m in methods_used if m in ("tesseract", "paddleocr", "model"))
    if "model" in engines:
        engines[engines.index("model")] = f"model:{ctx.config.llm.transcription_model}"
    scored = [p.quality for p in out.pages if p.quality is not None]
    methods_doc = {
        "pages": [{"page": p.page_no, "modules": p.modules} for p in out.pages],
        "document": {
            "ocr_error": doc.ocr_error if doc else None,
            "docling_error": doc.docling_error if doc else None,
        },
    }
    if old is not None:
        conn.execute("UPDATE documents SET current = 0 WHERE document_id = ?", (old[0],))
    cursor = conn.execute(
        "INSERT INTO documents (row_id, record_id, capture_id, pre_ocr_sha256, post_ocr_sha256, "
        "ocr_applied, ocr_engine, docling_path, page_count, text_sha256, quality_min, "
        "quality_json, extracted_at, text_capture_id, layout_capture_id, canonical_version, "
        "methods_json, current) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
        (
            row.row_id, pdf.record_id, pdf.capture_id, pdf.sha256, post_sha,
            1 if engines else 0, ",".join(engines) or None, docling_path, len(out.pages),
            canon.sha256, min(scored) if scored else None, quality_text, _now(), text_capture,
            layout_capture, canonical.CANONICAL_VERSION, json.dumps(methods_doc, sort_keys=True),
        ),
    )  # fmt: skip
    new_id = int(cursor.lastrowid or 0)
    for p in out.pages:
        start, end = canon.spans.get(p.page_no, (0, 0))
        conn.execute(
            "INSERT INTO document_pages (document_id, page_no, method, quality, flagged, "
            "flag_reasons, char_start, char_end) VALUES (?,?,?,?,?,?,?,?)",
            (new_id, p.page_no, p.method, p.quality, 1 if p.flagged else 0,
             json.dumps(p.reasons), start, end),
        )  # fmt: skip
    conn.commit()
    return new_id


# --- the row ------------------------------------------------------------------------------------


def _signature(ctx: Context, registry: Registry) -> dict[str, bool]:
    """Which rungs can run now. Model transcription counts as present when it is configured and has
    a key, not when the month's budget has room, so spending does not change a row's hash."""
    names = chain.chains_for(ctx.config, STAGE)[0].modules
    out: dict[str, bool] = {}
    for name in names:
        module = registry.get(name)
        if module is None:
            out[name] = False
        elif name == "documents.claude_transcribe":
            out[name] = transcribe_configured(ctx)
        else:
            out[name] = module.available(ctx).ok
    return out


def input_hash(row: Row, ctx: Context, registry: Registry = REGISTRY) -> str:
    """What ``efp run documents --recheck`` compares with the stored hash: the PDFs the row has
    now, the rungs that can run, the canonical and score versions, and the settings. A stored hash
    with a ``:budget`` suffix never equals this one, so those rows are revisited."""
    if ctx.db is None:
        raise RuntimeError("the documents stage needs the database")
    return _hash(ctx, registry, pdf_inputs(ctx.db, ctx, row.row_id))


def _model_part(ctx: Context) -> list[Any]:
    """What would shape a model-read page, in the hash only while the fallback is on: the model,
    thinking and effort, the output limit, the temperature policy, and the prompt's hash. A change
    to any of them changes the output, and with the fallback off none of them matters."""
    if not ctx.config.documents.llm_transcription_fallback:
        return []
    return [
        llm.request_signature(
            ctx.config.llm.transcription_model, ctx.config.documents.transcribe_max_tokens
        ),
        hashlib.sha256(PROMPT.read_bytes()).hexdigest(),
    ]


def _hash(ctx: Context, registry: Registry, pdfs: list[PdfInput], suffix: str = "") -> str:
    cfg = ctx.config.documents
    if not pdfs:
        return hash_values("no_documents", canonical.CANONICAL_VERSION)
    return (
        hash_values(
            [p.sha256 for p in pdfs],
            _signature(ctx, registry),
            canonical.CANONICAL_VERSION,
            SCORE_VERSION,
            cfg.ocr_quality_threshold,
            cfg.max_pages,
            cfg.docling_always,  # turning it on revisits rows read without Docling, on purpose
            cfg.docling_timeout_seconds,
            cfg.render_dpi,
            cfg.ocr_language,
            _model_part(ctx),
        )
        + suffix
    )


def _summary(outs: list[DocOut]) -> dict[str, Any]:
    by_method: dict[str, int] = {}
    docs = []
    for o in outs:
        for p in o.pages:
            by_method[p.method] = by_method.get(p.method, 0) + 1
        scored = [p.quality for p in o.pages if p.quality is not None]
        docs.append(
            {
                "capture_id": o.pdf.capture_id,
                "status": "unreadable" if o.unreadable else "read",
                "reason": o.unreadable,
                "pages": len(o.pages),
                "quality_min": min(scored) if scored else None,
                "flagged_pages": sum(1 for p in o.pages if p.flagged),
            }
        )
    read = [d for d in docs if d["status"] == "read"]
    status = "read" if len(read) == len(docs) else ("partial" if read else "unreadable")
    return {"documents_status": status, "documents": docs, "pages_by_method": by_method}


def _gaps(outs: list[DocOut]) -> str:
    """The deferral suffix. A page left flagged because the month's budget was spent is revisited by
    ``--recheck`` once it is there: the budget is not in the hash (spending would change it on every
    call), so the suffix keeps the stored hash from matching. A missing tool needs no suffix: which
    rungs can run is part of the hash, so installing one changes it."""
    for o in outs:
        for p in o.pages:
            if p.flagged and any(
                m["name"] == "documents.claude_transcribe" and "max_monthly_usd" in m["reason"]
                for m in p.modules
            ):
                return ":budget"
    return ""


def process_row(row: Row, ctx: Context, registry: Registry = REGISTRY) -> StageResult:
    conn = ctx.db
    if conn is None or ctx.capture is None:
        raise RuntimeError("the documents stage needs the database and the capture client")
    pdfs = pdf_inputs(conn, ctx, row.row_id)
    if not pdfs:
        return StageResult(
            row_id=row.row_id, stage=STAGE, status="done",
            input_hash=_hash(ctx, registry, pdfs), tool_version=TOOL_VERSION,
            summary_json={"documents_status": "no_documents", "documents": []},
        )  # fmt: skip
    if not pdfio.have("pdfplumber"):
        return StageResult(
            row_id=row.row_id, stage=STAGE, status="done",
            input_hash=_hash(ctx, registry, pdfs), tool_version=TOOL_VERSION,
            summary_json={
                "documents_status": "no_tools",
                "reason": "pdfplumber is not installed (the documents extra)",
                "documents": [{"capture_id": p.capture_id} for p in pdfs],
            },
        )  # fmt: skip
    threshold = ctx.config.documents.ocr_quality_threshold
    outs = []
    for pdf in pdfs:
        out = process_pdf(ctx, registry, row, pdf, threshold)
        write_document(ctx, row, out, threshold)
        outs.append(out)
    return StageResult(
        row_id=row.row_id, stage=STAGE, status="done",
        input_hash=_hash(ctx, registry, pdfs, _gaps(outs)), tool_version=TOOL_VERSION,
        summary_json=_summary(outs),
    )  # fmt: skip


def run(batch: list[Row], ctx: Context) -> list[StageResult]:
    results: list[StageResult] = []
    for row in batch:
        try:
            results.append(process_row(row, ctx))
        except Exception as exc:  # one row's bug must not take the batch with it
            log.exception("documents failed for row %s", row.row_id)
            if ctx.db is not None:
                ctx.db.rollback()
            results.append(
                StageResult(
                    row_id=row.row_id,
                    stage=STAGE,
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}"[:300],
                    tool_version=TOOL_VERSION,
                )
            )
    return results

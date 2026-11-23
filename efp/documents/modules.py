"""The five rungs of the per-page ladder (``[stages.documents]``, policy ``ladder``).

    documents.pdfplumber        born-digital text, words with boxes
    documents.docling           layout and structure over the whole document (once per PDF)
    documents.ocrmypdf          Tesseract through OCRmyPDF ``--skip-text`` on a copy (once per PDF)
    documents.paddleocr         PaddleOCR on the page image, when the rungs above fell short
    documents.claude_transcribe the model reads the page image, when every rung above fell short

Each module adds a candidate to the page's state and returns its score as ``quality``; the
ladder stops at the first candidate that meets ``documents.ocr_quality_threshold``. A module that
cannot run says why (``available``) and the next one runs, so a machine without Docling, Tesseract
or an API key still produces text, marked with the methods that ran.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

from efp import modules as chain
from efp.capture import CaptureBudgetError, CaptureError
from efp.documents import docling_parse, llm, pdfio
from efp.documents.ladder import Candidate, DocState, PageTask
from efp.documents.pdfio import PageSize, Word
from efp.documents.quality import score_lines
from efp.logging import redact as scrub_text
from efp.modules import Availability, ModuleResult, Outcome, Registry
from efp.secrets import SecretsError, load_secrets
from efp.types import Context, Row

log = logging.getLogger(__name__)
STAGE = "documents"
PROMPT = Path(__file__).resolve().parents[2] / "prompts" / "transcribe_page.md"
MODEL_IMAGE_MAX_SIDE = 1568  # pixels; the API scales larger images down anyway


def _task(row: Row) -> PageTask:
    if not isinstance(row, PageTask):
        raise TypeError("the documents modules run on a PageTask")
    return row


def _texts(lines: list[list[Word]]) -> list[str]:
    return [" ".join(w.text for w in line) for line in lines]


def _needs(*pairs: tuple[str, str]) -> Availability:
    for module, why in pairs:
        if not pdfio.have(module):
            return Availability.unavailable(why)
    return Availability.available()


def _candidate(
    task: PageTask,
    method: str,
    lines: list[list[Word]],
    size: PageSize,
    *,
    bbox_source: str = "word",
    confidences: list[float] | None = None,
    flags: tuple[str, ...] = (),
) -> ModuleResult:
    score = score_lines(_texts(lines), confidences)
    if score.empty:
        return ModuleResult(Outcome.miss)
    task.p.add(Candidate(method, lines, score, size, bbox_source, flags))
    return ModuleResult(Outcome.hit, quality=score.score)


class Pdfplumber:
    name = "documents.pdfplumber"
    stage = STAGE

    def available(self, ctx: Context) -> Availability:
        return _needs(("pdfplumber", "pdfplumber is not installed (the documents extra)"))

    def applies(self, row: Row, ctx: Context) -> bool | str:
        return True

    def run(self, row: Row, ctx: Context) -> ModuleResult:
        task = _task(row)
        lines, size = task.d.reader(task.d.path).native_page(task.page_no - 1)
        if lines:
            return _candidate(task, "native", lines, size)
        try:  # no text layer: a page with no ink is blank, not a failure
            image = task.d.reader(task.d.path).render_page(task.page_no - 1, 100)
        except pdfio.PdfError:
            return ModuleResult(Outcome.miss)
        if pdfio.is_blank_image(image):
            task.p.blank = True
            return ModuleResult(Outcome.hit, quality=1.0)
        return ModuleResult(Outcome.miss)


def convert_docling(doc: DocState) -> None:
    """Run Docling over the whole PDF once; the outcome (or the error) stays on ``doc``."""
    if doc.docling_attempted:
        return
    doc.docling_attempted = True
    try:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption

        # layout and structure only: scanned pages are the OCR rungs' work
        options = PdfPipelineOptions(do_ocr=False)
        converter = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
        )
        result = converter.convert(str(doc.path))
        doc.docling_dict = result.document.export_to_dict()
        doc.docling_pages = docling_parse.parse(doc.docling_dict)
    except Exception as exc:
        doc.docling_error = f"{type(exc).__name__}: {str(exc)[:160]}"


def docling_first(ctx: Context, registry: Registry, doc: DocState) -> None:
    """``documents.docling_always``: structure for every document, not only those with a page the
    first rung could not read. Does nothing when Docling is not in the chain or not installed."""
    module = registry.get("documents.docling")
    if module is not None and module.available(ctx).ok:
        convert_docling(doc)


class Docling:
    name = "documents.docling"
    stage = STAGE

    def available(self, ctx: Context) -> Availability:
        return _needs(("docling", "docling is not installed (the layout extra)"))

    def applies(self, row: Row, ctx: Context) -> bool | str:
        return True

    def run(self, row: Row, ctx: Context) -> ModuleResult:
        task = _task(row)
        doc = task.d
        convert_docling(doc)
        if doc.docling_error:
            return ModuleResult(Outcome.error, error=doc.docling_error)
        got = doc.docling_pages.get(task.page_no)
        if not got or not got[0]:
            return ModuleResult(Outcome.miss)
        return _candidate(task, "docling", got[0], got[1], bbox_source="item")


class Ocrmypdf:
    name = "documents.ocrmypdf"
    stage = STAGE

    def available(self, ctx: Context) -> Availability:
        found = _needs(
            ("ocrmypdf", "ocrmypdf is not installed (the documents extra)"),
            ("pdfplumber", "pdfplumber is not installed (the documents extra)"),
        )
        if not found.ok:
            return found
        if shutil.which("tesseract") is None:
            return Availability.unavailable("the tesseract binary is not on PATH")
        return Availability.available()

    def applies(self, row: Row, ctx: Context) -> bool | str:
        return True

    def run(self, row: Row, ctx: Context) -> ModuleResult:
        task = _task(row)
        doc = task.d
        cfg = ctx.config.documents
        task.p.ran.add("tesseract")
        if not doc.ocr_attempted:
            doc.ocr_attempted = True
            target = doc.scratch / "ocr.pdf"
            # A subprocess, not an import: importing ocrmypdf monkeypatches pdfminer.six for the
            # whole process (PDFSimpleFont), after which pdfplumber reads a standard-font PDF as
            # "(cid:72)(cid:101)..." text. The copy in the scratch directory is the only input.
            command = [
                sys.executable, "-m", "ocrmypdf", "--skip-text", "-l", cfg.ocr_language,
                "--output-type", "pdf", "--optimize", "0", "--jobs", "2", "--quiet",
                "--invalidate-digital-signatures",
                "--tesseract-timeout", str(min(180, cfg.ocr_timeout_seconds)),
                str(doc.path), str(target),
            ]  # fmt: skip
            try:
                done = subprocess.run(  # noqa: S603
                    command, capture_output=True, text=True, check=False,
                    timeout=cfg.ocr_timeout_seconds,
                )  # fmt: skip
                if done.returncode == 0 and target.is_file():
                    doc.ocr_path = target
                else:
                    last = (done.stderr.strip().splitlines() or ["no output"])[-1]
                    doc.ocr_error = f"ocrmypdf exit {done.returncode}: {scrub_text(last)[:160]}"
            except subprocess.TimeoutExpired:
                doc.ocr_error = f"ocrmypdf timed out after {cfg.ocr_timeout_seconds} s"
            except OSError as exc:
                doc.ocr_error = f"ocrmypdf could not start: {type(exc).__name__}"
        if doc.ocr_error or doc.ocr_path is None:
            return ModuleResult(Outcome.error, error=doc.ocr_error or "no OCR output")
        lines, size = doc.reader(doc.ocr_path).native_page(task.page_no - 1)
        return _candidate(task, "tesseract", lines, size)


def _lang(code: str) -> str:
    return {"eng": "en", "fra": "fr", "deu": "german", "spa": "es"}.get(code, code)


@lru_cache(maxsize=2)
def _paddle(lang: str) -> Any:
    from paddleocr import PaddleOCR

    return PaddleOCR(
        lang=lang,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
    )


def parse_paddle(result: Any, scale: float) -> tuple[list[list[Word]], list[float]]:
    """PaddleOCR 3.x ``predict`` output (``rec_texts``, ``rec_scores``, ``rec_boxes`` as
    ``x0, y0, x1, y1`` in pixels) as lines of words in points. A recognised line is split on
    spaces and its box shared out by character count (``bbox_source = 'line_split'``)."""
    entries = result if isinstance(result, list) else [result]
    found: list[tuple[float, float, list[Word], float]] = []
    for entry in entries:
        texts = list(entry.get("rec_texts") or [])
        scores = list(entry.get("rec_scores") or [1.0] * len(texts))
        boxes = entry.get("rec_boxes")
        if boxes is None:
            boxes = [(min(p[0] for p in poly), min(p[1] for p in poly),
                      max(p[0] for p in poly), max(p[1] for p in poly))
                     for poly in (entry.get("rec_polys") or [])]  # fmt: skip
        for text, score, box in zip(texts, scores, boxes, strict=False):
            x0, y0, x1, y1 = (float(v) * scale for v in box)
            tokens = str(text).split()
            total = sum(len(t) for t in tokens) or 1
            cursor, words = x0, []
            for token in tokens:
                width = (x1 - x0) * len(token) / total
                words.append(Word(token, cursor, y0, cursor + width, y1, float(score)))
                cursor += width
            if words:
                found.append((y0, x0, words, float(score)))
    found.sort(key=lambda f: (round(f[0] / 4), f[1]))
    return [f[2] for f in found], [f[3] for f in found]


class Paddleocr:
    name = "documents.paddleocr"
    stage = STAGE

    def available(self, ctx: Context) -> Availability:
        return _needs(
            ("paddleocr", "paddleocr is not installed (the ocr-fallback extra)"),
            ("paddle", "paddlepaddle is not installed (the ocr-fallback extra)"),
            ("pdfplumber", "pdfplumber is not installed (the documents extra)"),
        )

    def applies(self, row: Row, ctx: Context) -> bool | str:
        return True  # reached only when the rungs above fell short of the bar

    def run(self, row: Row, ctx: Context) -> ModuleResult:
        import numpy

        task = _task(row)
        cfg = ctx.config.documents
        task.p.ran.add("paddleocr")
        image = task.d.reader(task.d.path).render_page(task.page_no - 1, cfg.render_dpi)
        result = _paddle(_lang(cfg.ocr_language)).predict(numpy.array(image))
        lines, confidences = parse_paddle(result, 72 / cfg.render_dpi)
        size = task.d.reader(task.d.path).page_size(task.page_no - 1)
        return _candidate(
            task, "paddleocr", lines, size, bbox_source="line_split", confidences=confidences
        )


def _api_key(ctx: Context) -> str | None:
    try:
        return load_secrets(ctx.config.paths.env_file).get("ANTHROPIC_API_KEY")
    except SecretsError:
        return None


def transcribe_configured(ctx: Context) -> bool:
    """Whether model transcription could run at all (flag, key, packages), ignoring the budget."""
    return (
        ctx.config.documents.llm_transcription_fallback
        and pdfio.have("anthropic")
        and _api_key(ctx) is not None
    )


def thinking_for(model: str) -> tuple[dict[str, str] | None, str | None]:
    """Sonnet 5.5 has thinking on by default and rejects ``disabled``; ``between_tools`` is how it
    is turned off, accepted at effort ``high`` or below. Any other model gets no thinking field."""
    if model.startswith("claude-sonnet-5-5"):
        return {"type": "between_tools"}, "low"
    return None, None


class ClaudeTranscribe:
    name = "documents.claude_transcribe"
    stage = STAGE

    def available(self, ctx: Context) -> Availability:
        cfg = ctx.config.documents
        if not cfg.llm_transcription_fallback:
            return Availability.unavailable("documents.llm_transcription_fallback is false")
        found = _needs(
            ("anthropic", "anthropic is not installed (the documents extra)"),
            ("pdfplumber", "pdfplumber is not installed (the documents extra)"),
        )
        if not found.ok:
            return found
        if _api_key(ctx) is None:
            return Availability.unavailable("ANTHROPIC_API_KEY is not set")
        if ctx.db is not None:
            fits, why = llm.room_for_call(ctx.db, ctx.config, cfg.transcribe_max_tokens)
            if not fits:
                return Availability.unavailable(why)
        return Availability.available()

    def applies(self, row: Row, ctx: Context) -> bool | str:
        """Only a page the OCR rungs failed on: at least one OCR engine ran on it, every
        installed one has, and the page has no text layer (a digital page is never sent out,
        whatever its score: a ledger of digits scores low and is not a scan)."""
        task = _task(row)
        ran = task.p.ran
        if not ran:
            return "no OCR engine ran on this page: the model reads only pages OCR failed on"
        wanted: dict[str, Any] = {"tesseract": Ocrmypdf, "paddleocr": Paddleocr}
        configured = set(chain.chains_for(ctx.config, STAGE)[0].modules)
        for method, cls in wanted.items():
            if cls.name in configured and method not in ran and cls().available(ctx).ok:
                return f"{method} has not yet run on this page"
        if task.p.has("native") is not None:
            return "the page has a text layer: it is not sent to the model"
        return True

    def run(self, row: Row, ctx: Context) -> ModuleResult:
        task = _task(row)
        cfg = ctx.config.documents
        conn, capture = ctx.db, ctx.capture
        key = _api_key(ctx)
        if conn is None or capture is None or key is None:
            return ModuleResult(Outcome.error, error="no database, capture client or API key")
        image = pdfio.shrink(
            task.d.reader(task.d.path).render_page(task.page_no - 1, cfg.render_dpi),
            MODEL_IMAGE_MAX_SIDE,
        )
        data = base64.standard_b64encode(pdfio.png_bytes(image)).decode("ascii")
        system = PROMPT.read_bytes()
        prompt_sha = hashlib.sha256(system).hexdigest()
        model = ctx.config.llm.transcription_model
        thinking, effort = thinking_for(model)
        send = ctx.config.llm.send_temperature
        temperature = ctx.config.llm.temperature if send else None
        content: list[dict[str, Any]] = [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": data},
            },
            {"type": "text", "text": "Transcribe this page."},
        ]
        common: dict[str, Any] = {
            "row_id": task.row_id,
            "stage": STAGE,
            "purpose": "transcribe_page",
            "model_id": model,
            "prompt_sha256": prompt_sha,
            "temperature": temperature,
            "thinking": thinking["type"] if thinking else None,
        }
        try:
            reply = capture.llm_message(
                api_key=key,
                row_id=task.row_id,
                stage=STAGE,
                purpose="transcribe_page",
                model=model,
                system=system.decode("utf-8"),
                content=content,
                max_tokens=cfg.transcribe_max_tokens,
                thinking=thinking,
                effort=effort,
                temperature=temperature,
            )
        except CaptureError as exc:
            llm.log_call(
                conn, capture_id=None, input_tokens=None, output_tokens=None, cost=0.0,
                request_id=None, ok=False, error=str(exc)[:300], **common,
            )  # fmt: skip
            kind = "budget" if isinstance(exc, CaptureBudgetError) else "call"
            return ModuleResult(Outcome.error, error=f"model transcription {kind}: {exc}"[:300])
        cost = llm.cost_usd(ctx.config, reply.input_tokens, reply.output_tokens)
        llm.log_call(
            conn, capture_id=reply.capture_id, input_tokens=reply.input_tokens,
            output_tokens=reply.output_tokens, cost=cost, request_id=reply.request_id,
            ok=reply.stop_reason != "refusal", error=None if reply.stop_reason != "refusal"
            else "refusal", **common,
        )  # fmt: skip
        if reply.stop_reason == "refusal" or not reply.text.strip():
            return ModuleResult(Outcome.miss)
        size = task.d.reader(task.d.path).page_size(task.page_no - 1)
        lines = [
            [Word(tok, 0.0, 0.0, size.width, size.height) for tok in ln.split()]
            for ln in reply.text.splitlines()
            if ln.split()
        ]
        task.d.transcribed_pages += 1
        # a reply cut off at max_tokens is part of a page: kept, and said so
        cut = ("truncated",) if reply.stop_reason == "max_tokens" else ()
        return _candidate(task, "model", lines, size, bbox_source="page", flags=cut)


MODULES: tuple[Any, ...] = (Pdfplumber, Docling, Ocrmypdf, Paddleocr, ClaudeTranscribe)


def register_modules(registry: Registry) -> None:
    for module in MODULES:
        if registry.get(module.name) is None:
            registry.register(module)

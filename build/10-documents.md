# Build 10: Documents — text, OCR and page provenance

Spec: section 4 (documents component), section 1 (F6: page, bounding-box and character-span provenance), section 7 (original bytes always survive OCR), section 10 (runtime: the OCR ladder and the model transcription fallback), section 12 (PyMuPDF is AGPL; use pdfplumber). Task list: D6. Needs: 07. Branch: `build/10-documents`. Report: `reports/10-documents.md`.

Read first: `CLAUDE.md`, `build/README.md`, `docs/spec.md` section 4 "documents" row, section 7 capture policy, section 10 "Runtime environment", the `documents` table from `reports/02-schema.md`, `docs/tool-register.md` on pdfplumber, pypdf, OCRmyPDF, Docling, PaddleOCR, `reports/07-court-federal.md` and `09-court-state-dockets.md` (which rows have filings).

Stay in plan mode until I approve. Propose the text ladder, the per-page quality score, the provenance structure (page, bbox, char span) and the storage layout for Docling output. Wait.

## What to build

1. **Inputs.** Every PDF the court stage captured (`court_records` → `captures` with `content_type` PDF, plus PDFs that arrive through the manual import path in build 14). The stage reads `court_records`, writes `documents`; predecessor is `court`.
2. **The ladder, per page** (modules `documents.pdfplumber`, `documents.docling`, `documents.ocrmypdf`, `documents.paddleocr`, `documents.claude_transcribe` under `[stages.documents]`, policy `ladder` with the quality threshold as the bar; a module whose tool is not installed, or whose key or budget is missing, is skipped with the reason and the ladder continues, so a machine without Docling or without an API key still produces text, marked with the methods that ran):
   1. Born-digital text with `pdfplumber` (words with bounding boxes); if the page yields text with a sane character distribution, done.
   2. Docling for layout and structure over the whole document (reading order, tables, headings), output stored under `paths.store` by hash and referenced by `documents.docling_path`.
   3. Scanned pages: OCRmyPDF (Tesseract) with `--skip-text`, producing a text layer while preserving the page image. The pre-OCR PDF's hash is the evidentiary one (`pre_ocr_sha256`); the OCR'd PDF is stored too (`post_ocr_sha256`). PaddleOCR is installed and used only if the Tesseract quality score falls below `documents.ocr_quality_threshold`.
   4. A page that fails both is transcribed by the Claude API from the page image (model id, prompt hash, temperature 0 recorded as for any extraction); that output is quality-scored and review-flagged like any other OCR, because model transcription errors are silent. This path counts against `llm.max_monthly_usd`.
3. **Quality score per page** in `[0, 1]`, defined and documented (dictionary-word ratio, character-class sanity, OCR engine confidence where available, line-length distribution), stored per page in `documents.quality_json` with the method used; `quality_min` is the document's lowest page. Pages under the threshold are flagged; a flagged page's text is stored but marked so build 16 never feeds it to extraction unflagged.
4. **Provenance that F6 requires.** For every text unit store page number, bounding box (from pdfplumber or OCR hOCR) and character offsets into the document's canonical text (`text_sha256` over that canonical text), so an extraction span (build 16) resolves to a page and a box. Define the canonical text once (page order, `\f` between pages, normalized whitespace rule written down) and never change it without a version bump recorded in `documents`.
5. **Text as captures.** The canonical text is stored as its own capture (`tool = 'documents'`, parent capture in `headers_json`), as build 06 did for HTML, so every reader of text reads a capture.
6. **Counts for the report.** Pages by method (native / Docling / Tesseract / PaddleOCR / model transcription / failed), documents by `quality_min` band, model transcription spend.

## Tests

- Synthetic PDFs generated in the test suite: born-digital, image-only (rendered from text, then rasterized), degraded (blurred or low-resolution) and a mixed document; no real filings in `tests/`.
- Ladder decisions on each; the quality score ranks them in the expected order; a page below threshold is flagged and excluded from the extraction-eligible view.
- Provenance: a known word's page and bbox resolve correctly from its char offsets; canonical text hash is stable across runs.
- The model fallback is tested with a stubbed client (no live calls in CI) and respects the budget cap.

## Done when

- `uv run efp run documents --sample` then the full run produce text and page-level quality flags for every captured filing; `efp status` reports the method and quality distributions.
- A test proves no page below the threshold can reach extraction unflagged.
- No PyMuPDF anywhere in the dependency tree (assert in a test over `uv.lock`).

## Guardrails that bite here

Original bytes always survive: never overwrite a captured PDF. Model transcription only on pages that failed both OCR paths, logged per call, under the budget cap. Nothing in `tests/` derived from real subjects.

Finish with `reports/10-documents.md` and a pull request. Do not merge.

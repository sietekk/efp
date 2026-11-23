"""Docling in its own process: ``python -m efp.documents.docling_worker PDF OUT MAX_PAGES SECONDS``.

Docling checks its ``document_timeout`` between pages, so one page that hangs inside its layout
model would stall the whole run if it ran in the stage's process. The stage starts this module as a
subprocess (``efp.documents.modules.run_group``) and kills its process group at a hard limit, as it
does for OCRmyPDF; the PDF is a scratch copy and the result is a JSON file the stage reads. Layout
and structure only (OCR is the OCR rungs' work); the first ``MAX_PAGES`` pages only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def convert(pdf: Path, out: Path, max_pages: int, seconds: float) -> None:
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    options = PdfPipelineOptions(do_ocr=False, document_timeout=seconds)
    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
    )
    result = converter.convert(str(pdf), page_range=(1, max(1, max_pages)))
    if getattr(getattr(result, "status", None), "name", "") == "FAILURE":
        raise RuntimeError("Docling could not convert the document")
    out.write_text(json.dumps(result.document.export_to_dict()), encoding="utf-8")


def main(argv: list[str]) -> int:
    try:
        pdf, out, pages, seconds = Path(argv[0]), Path(argv[1]), int(argv[2]), float(argv[3])
        convert(pdf, out, pages, seconds)
    except Exception as exc:  # the parent reads the last stderr line as the reason
        print(f"{type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

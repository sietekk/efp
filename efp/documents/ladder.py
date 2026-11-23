"""The shared state of the per-page ladder: what each module may read and add.

A ``DocState`` lives for one PDF (what is expensive to do once per document: the OCR pass, the
Docling pass). A ``PageState`` lives for one page and collects the candidates the modules produce.
``PageTask`` is the ``Row`` the module chain is given, as ``LinkTask`` is for resolve_links.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from efp.documents.pdfio import PageSize, Word
from efp.documents.quality import PageScore
from efp.types import Row

# ladder rung -> the method name stored in document_pages.method
METHODS = ("native", "docling", "tesseract", "paddleocr", "model")


@dataclass
class Candidate:
    method: str
    lines: list[list[Word]]
    score: PageScore
    size: PageSize
    bbox_source: str = "word"  # word | item | line_split | page


@dataclass
class PageState:
    candidates: list[Candidate] = field(default_factory=list)
    blank: bool = False

    def add(self, candidate: Candidate) -> None:
        self.candidates.append(candidate)

    def best(self) -> Candidate | None:
        if not self.candidates:
            return None
        # highest score; on a tie the earlier rung (cheaper, more faithful to the original bytes)
        return max(enumerate(self.candidates), key=lambda p: (p[1].score.score, -p[0]))[1]

    def best_score(self) -> float | None:
        best = self.best()
        return best.score.score if best else None

    def has(self, method: str) -> Candidate | None:
        return next((c for c in self.candidates if c.method == method), None)


@dataclass
class DocState:
    """Per-PDF state shared by the modules while one document is processed."""

    path: Path  # the captured original in the store: read, never written
    scratch: Path  # a temporary directory for OCR output
    row_id: int
    capture_id: int
    page_count: int
    ocr_attempted: bool = False
    ocr_error: str | None = None
    ocr_path: Path | None = None
    docling_attempted: bool = False
    docling_error: str | None = None
    docling_dict: dict[str, Any] | None = None
    docling_pages: dict[int, tuple[list[list[Word]], PageSize]] = field(default_factory=dict)
    engines: set[str] = field(default_factory=set)  # OCR engines that produced a used page
    transcribed_pages: int = 0


class PageTask(Row):
    """One page of one PDF, with the mutable ladder state."""

    page_no: int  # 1-based
    doc: Any  # the DocState
    ladder: Any  # the PageState

    @property
    def d(self) -> DocState:
        state: DocState = self.doc
        return state

    @property
    def p(self) -> PageState:
        state: PageState = self.ladder
        return state

"""Reading PDFs: words with boxes, page images, page count. pdfplumber and pypdfium2 only.

PyMuPDF is AGPL and is never used (spec section 12). The imports are lazy: the ``documents`` extra
may be absent, and the module chain reports that as a skipped module, not a crash.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Word:
    """One word of a page. Boxes are PDF points, origin top-left: ``(x0, top, x1, bottom)``."""

    text: str
    x0: float
    top: float
    x1: float
    bottom: float
    conf: float | None = None  # the engine's confidence in [0, 1], where it gives one


@dataclass(frozen=True)
class PageSize:
    width: float
    height: float


class PdfError(Exception):
    """The file is not a readable PDF (corrupt, encrypted, truncated)."""


def have(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def is_pdf(data: bytes) -> bool:
    return b"%PDF-" in data[:1024]


def _open(path: Path) -> Any:
    import pdfplumber

    try:
        return pdfplumber.open(path)
    except Exception as exc:  # pdfminer raises several types for a bad file
        raise PdfError(f"{type(exc).__name__}: {str(exc)[:120]}") from exc


def page_count(path: Path) -> int:
    with _open(path) as pdf:
        return len(pdf.pages)


def group_lines(words: list[Word]) -> list[list[Word]]:
    """Words into lines: top to bottom, left to right. A word joins the current line when its
    vertical centre is within 0.6 of the line's tallest word from the line's mean centre."""
    lines: list[list[Word]] = []
    for word in sorted(words, key=lambda w: (w.top, w.x0)):
        if lines:
            line = lines[-1]
            centre = sum((w.top + w.bottom) / 2 for w in line) / len(line)
            height = max(1.0, max(w.bottom - w.top for w in line))
            if abs((word.top + word.bottom) / 2 - centre) <= 0.6 * height:
                line.append(word)
                continue
        lines.append([word])
    return [sorted(line, key=lambda w: w.x0) for line in lines]


def native_page(path: Path, index: int) -> tuple[list[list[Word]], PageSize]:
    """The text layer of page ``index`` (0-based) as lines of words with boxes, and its size."""
    with _open(path) as pdf:
        try:
            page = pdf.pages[index]
            raw = page.extract_words(keep_blank_chars=False, use_text_flow=False)
            size = PageSize(float(page.width), float(page.height))
        except Exception as exc:
            raise PdfError(f"page {index + 1}: {type(exc).__name__}") from exc
    words = [
        Word(str(w["text"]), float(w["x0"]), float(w["top"]), float(w["x1"]), float(w["bottom"]))
        for w in raw
        if str(w.get("text", "")).strip()
    ]
    return group_lines(words), size


def render_page(path: Path, index: int, dpi: int) -> Any:
    """The page as a PIL image (pdfplumber renders through pypdfium2)."""
    with _open(path) as pdf:
        try:
            return pdf.pages[index].to_image(resolution=dpi).original.convert("RGB")
        except Exception as exc:
            raise PdfError(f"page {index + 1} render: {type(exc).__name__}") from exc


def is_blank_image(image: Any, *, ink_share: float = 0.0005) -> bool:
    """True when almost no pixel of the page is dark: nothing to read, nothing lost."""
    grey = image.convert("L").point(lambda v: 255 if v < 200 else 0)
    dark = grey.histogram()[255]
    return bool(dark / max(1, image.width * image.height) < ink_share)


def png_bytes(image: Any) -> bytes:
    import io

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def page_size(path: Path, index: int) -> PageSize:
    with _open(path) as pdf:
        try:
            page = pdf.pages[index]
            return PageSize(float(page.width), float(page.height))
        except Exception as exc:
            raise PdfError(f"page {index + 1}: {type(exc).__name__}") from exc


def shrink(image: Any, max_side: int) -> Any:
    """The image scaled down (never up) so its longer side is at most ``max_side`` pixels."""
    longest = max(image.width, image.height)
    if longest <= max_side:
        return image
    factor = max_side / longest
    return image.resize((max(1, round(image.width * factor)), max(1, round(image.height * factor))))

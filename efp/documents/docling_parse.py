"""Docling's JSON export read into lines of words, per page, in Docling's reading order.

Docling (``export_to_dict``) gives text items and tables with a page number and a box in the page's
coordinate system (``coord_origin`` BOTTOMLEFT or TOPLEFT) and a body tree that fixes reading order.
An item's words share the item's box (``bbox_source = 'item'``): Docling does not box single words.
This parser needs no Docling install, so it is tested on a synthetic document.
"""

from __future__ import annotations

from typing import Any

from efp.documents.pdfio import PageSize, Word


def _box(prov: dict[str, Any], height: float) -> tuple[float, float, float, float] | None:
    box = prov.get("bbox")
    if not isinstance(box, dict):
        return None
    try:
        left, right = float(box["l"]), float(box["r"])
        top, bottom = float(box["t"]), float(box["b"])
    except (KeyError, TypeError, ValueError):
        return None
    if str(box.get("coord_origin", "BOTTOMLEFT")).upper().startswith("BOTTOM"):
        top, bottom = height - top, height - bottom  # to origin top-left
    return min(left, right), min(top, bottom), max(left, right), max(top, bottom)


def _resolve(doc: dict[str, Any], ref: str) -> dict[str, Any] | None:
    parts = ref.lstrip("#/").split("/")
    node: Any = doc
    for part in parts:
        if isinstance(node, list):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(node, dict):
            node = node.get(part)
        else:
            return None
    return node if isinstance(node, dict) else None


def _order(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Items (texts and tables) in reading order, by walking the body tree; any item the tree does
    not reach is appended in file order so nothing is dropped."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []

    def walk(node: dict[str, Any]) -> None:
        for child in node.get("children", []) or []:
            ref = child.get("$ref") if isinstance(child, dict) else None
            if not ref or ref in seen:
                continue
            seen.add(ref)
            item = _resolve(doc, ref)
            if item is None:
                continue
            if ref.startswith("#/texts/") or ref.startswith("#/tables/"):
                out.append(item)
            walk(item)

    body = doc.get("body")
    if isinstance(body, dict):
        walk(body)
    for kind in ("texts", "tables"):
        for index, item in enumerate(doc.get(kind, []) or []):
            if f"#/{kind}/{index}" not in seen and isinstance(item, dict):
                out.append(item)
    return out


def _table_rows(item: dict[str, Any]) -> list[str]:
    cells = (item.get("data") or {}).get("table_cells") or []
    rows: dict[int, list[tuple[int, str]]] = {}
    for cell in cells:
        text = str(cell.get("text") or "").strip()
        if text:
            rows.setdefault(int(cell.get("start_row_offset_idx", 0)), []).append(
                (int(cell.get("start_col_offset_idx", 0)), text)
            )
    return [" ".join(t for _, t in sorted(row)) for _, row in sorted(rows.items())]


def parse(doc: dict[str, Any]) -> dict[int, tuple[list[list[Word]], PageSize]]:
    """``{page_no: (lines, size)}``; ``page_no`` is 1-based. Pages with no text are present."""
    sizes: dict[int, PageSize] = {}
    for key, page in (doc.get("pages") or {}).items():
        size = (page or {}).get("size") or {}
        number = int((page or {}).get("page_no", key))
        sizes[number] = PageSize(float(size.get("width", 0)), float(size.get("height", 0)))
    pages: dict[int, list[list[Word]]] = {n: [] for n in sizes}
    for item in _order(doc):
        provs = item.get("prov") or []
        if not provs:
            continue
        prov = provs[0]
        number = int(prov.get("page_no", 0))
        if number <= 0:
            continue
        size = sizes.get(number, PageSize(0, 0))
        box = _box(prov, size.height)
        if box is None:
            continue
        texts = (
            _table_rows(item)
            if "data" in item and "table_cells" in (item["data"] or {})
            else [str(item.get("text") or "")]
        )
        for text in texts:
            words = [Word(w, *box) for w in text.split()]
            if words:
                pages.setdefault(number, []).append(words)
                sizes.setdefault(number, size)
    return {n: (pages[n], sizes.get(n, PageSize(0, 0))) for n in sorted(pages)}

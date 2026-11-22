"""Row hash: ``rows.row_sha256``. The canonicalization is specified in ``docs/row_hash.md``.

Ingest computes it, export (build 19) and ``efp selftest --db`` verify it, so there is exactly one
implementation. A cell is canonicalised by type so that a ``Year`` stored as the number 2016 and one
stored as the text ``"2016"`` hash differently (export must reproduce the original cell), while
``None`` and ``""`` hash the same. Whitespace is never normalised.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Sequence

UNIT_SEPARATOR = "\x1f"  # joins the nine cells
TYPE_TAG = "\x1e"  # marks a non-text cell; never allowed inside a text cell

NINE = 9


class CellError(ValueError):
    """A cell value cannot be hashed unambiguously (unsupported type or reserved character)."""


def canonical_cell(value: object) -> str:
    """The text a cell contributes to the row hash."""
    if value is None:
        return ""
    if isinstance(value, str):
        if UNIT_SEPARATOR in value or TYPE_TAG in value:
            raise CellError("text cell contains a reserved separator character (\\x1e or \\x1f)")
        return value
    # bool is a subclass of int, so test it first.
    if isinstance(value, bool):
        return f"{TYPE_TAG}bool:{value!r}"
    if isinstance(value, int):
        return f"{TYPE_TAG}int:{value!r}"
    if isinstance(value, float):
        return f"{TYPE_TAG}float:{value!r}"
    if isinstance(value, dt.datetime):
        return f"{TYPE_TAG}datetime:{value.isoformat()}"
    if isinstance(value, dt.date):
        return f"{TYPE_TAG}date:{value.isoformat()}"
    if isinstance(value, dt.time):
        return f"{TYPE_TAG}time:{value.isoformat()}"
    if isinstance(value, dt.timedelta):
        return f"{TYPE_TAG}timedelta:{value!r}"
    raise CellError(f"unsupported cell type {type(value).__name__}")


def row_sha256(cells: Sequence[object]) -> str:
    """SHA-256 over the nine source cells in column order, joined by the unit separator."""
    if len(cells) != NINE:
        raise CellError(f"a row has {NINE} source cells, got {len(cells)}")
    joined = UNIT_SEPARATOR.join(canonical_cell(c) for c in cells)
    try:
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()
    except UnicodeEncodeError as exc:
        raise CellError("text cell is not valid Unicode (lone surrogate)") from exc

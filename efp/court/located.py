"""What a court sub-strategy hands back, and the per-row state the chain shares."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from efp.court.classify import Classification
from efp.court.names import NameParts
from efp.types import Context, Row

STAGE = "court"


@dataclass
class Located:
    """One located document: one ``court_records`` row (a release, a docket, a report entry)."""

    source_kind: str  # doj_release | pin_report | federal_docket | state_ag_release | ...
    jurisdiction: str | None
    court: str | None
    case_number: str | None
    document_kind: str | None
    url: str | None
    strategy: str  # the module that found it
    capture_id: int | None  # the capture the record rests on
    text: str | None = None  # captured text, for identifier extraction
    text_capture_id: int | None = None  # the capture ``text`` is the body of (spans point here)
    basis: dict[str, Any] = field(default_factory=dict)
    weak: bool = False  # a match with something unconfirmed (state, full name)
    docket_id: int | None = None  # CourtListener docket id, when known


@dataclass
class CaseState:
    classification: Classification
    parts: NameParts
    located: list[Located] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    flags: dict[str, Any] = field(default_factory=dict)

    def has_case_number(self) -> bool:
        return any(item.case_number for item in self.located)


class CourtTask(Row):
    """A row passing through the court chain, with its shared :class:`CaseState`."""

    case: Any

    @property
    def cs(self) -> CaseState:
        state: CaseState = self.case
        return state


def cap(ctx: Context) -> Any:
    if ctx.capture is None:
        raise RuntimeError("the court stage needs the capture client in the context")
    return ctx.capture


def store_text(
    ctx: Context,
    row_id: int,
    *,
    parent_capture_id: int,
    url: str,
    text: str,
    tool: str,
    version: str = "1",
    extra: dict[str, Any] | None = None,
) -> int:
    """Keep a document's extracted text as its own capture so identifier spans point at it.

    The same text for the same row is kept once: a re-run finds the capture it made before, so
    records and identifiers built on it are not duplicated."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if ctx.db is not None:
        found = ctx.db.execute(
            "SELECT capture_id FROM captures WHERE row_id = ? AND tool = ? AND content_sha256 = ? "
            "AND url_requested = ? ORDER BY capture_id LIMIT 1",
            (row_id, tool, digest, url),
        ).fetchone()
        if found is not None:
            return int(found[0])
    record = cap(ctx).store_bytes(
        text.encode("utf-8"),
        row_id=row_id,
        stage=STAGE,
        url_requested=url,
        url_final=url,
        tool=tool,
        tool_version=version,
        content_type="text/plain; charset=utf-8",
        headers={"parent_capture_id": parent_capture_id, **(extra or {})},
    )
    return int(record.capture_id)

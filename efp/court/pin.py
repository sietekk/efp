"""Public Integrity Section annual reports: ``court.pin_reports`` (pre-2013 federal rows).

The DOJ press-release API is substantive only from about 2013. For earlier federal rows the
fallback is the Public Integrity Section's annual *Federal Prosecution of Election Offenses*
reports: each PDF is captured once through the capture layer, its text is split into paragraphs
and indexed by name, and a row matches when a paragraph names the subject in full.

The report URLs are not in any project document and are not invented here: the operator supplies
them in ``court.pin_report_urls`` (``docs/sources.md`` lists the editions to find). Reading a PDF
needs ``pdfplumber`` or ``pypdf`` (the ``documents`` extra); without one this module reports itself
unavailable and the chain goes on.
"""

from __future__ import annotations

import importlib.util
import re
import sqlite3
from collections.abc import Callable
from io import BytesIO
from pathlib import Path

from efp.capture import CaptureError
from efp.config import Config
from efp.court import doj
from efp.court.located import STAGE, CourtTask, Located, cap, store_text
from efp.court.names import NameParts, match_name
from efp.modules import Availability, ModuleResult, Outcome
from efp.types import Context, Row

LAST_YEAR = 2012  # the API covers 2013 on
MIN_PARAGRAPH = 40
_CASE = re.compile(r"\b\d{1,2}:\d{2}-cr-\d{2,6}\b|\b(?:No\.\s*)?\d{2}-(?:CR|cr)-\d{2,6}\b")


def _backend() -> Callable[[bytes], list[str]] | None:
    if importlib.util.find_spec("pdfplumber") is not None:

        def with_pdfplumber(pdf: bytes) -> list[str]:
            import pdfplumber  # type: ignore[import-not-found,unused-ignore]

            with pdfplumber.open(BytesIO(pdf)) as document:
                return [page.extract_text() or "" for page in document.pages]

        return with_pdfplumber
    if importlib.util.find_spec("pypdf") is not None:

        def with_pypdf(pdf: bytes) -> list[str]:
            from pypdf import PdfReader  # type: ignore[import-not-found,unused-ignore]

            return [(page.extract_text() or "") for page in PdfReader(BytesIO(pdf)).pages]

        return with_pypdf
    return None


def paragraphs(page_text: str) -> list[str]:
    """Blocks of text separated by blank lines, line breaks inside a block joined."""
    blocks = [re.sub(r"\s*\n\s*", " ", b).strip() for b in re.split(r"\n\s*\n", page_text)]
    return [b for b in blocks if len(b) >= MIN_PARAGRAPH]


class PinIndex:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.executescript(
            "CREATE TABLE IF NOT EXISTS entries (id INTEGER PRIMARY KEY, url TEXT, "
            "capture_id INTEGER, page INTEGER, paragraph TEXT);"
            "CREATE INDEX IF NOT EXISTS idx_pin_url ON entries(url);"
        )

    def indexed(self, url: str) -> bool:
        return (
            self.conn.execute("SELECT 1 FROM entries WHERE url = ? LIMIT 1", (url,)).fetchone()
            is not None
        )

    def index_pages(self, url: str, capture_id: int, pages: list[str]) -> int:
        rows = [
            (url, capture_id, number, block)
            for number, text in enumerate(pages, start=1)
            for block in paragraphs(text)
        ]
        self.conn.executemany(
            "INSERT INTO entries (url, capture_id, page, paragraph) VALUES (?, ?, ?, ?)", rows
        )
        self.conn.commit()
        return len(rows)

    def find(self, parts: NameParts) -> list[tuple[str, int, int, str]]:
        escaped = parts.surname.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = self.conn.execute(
            "SELECT url, capture_id, page, paragraph FROM entries "
            "WHERE paragraph LIKE ? ESCAPE '\\'",
            (f"%{escaped}%",),
        ).fetchall()
        return [r for r in rows if (m := match_name(parts, r[3])) and m.full]

    def count(self) -> int:
        return int(self.conn.execute("SELECT count(*) FROM entries").fetchone()[0])


def index_path(config: Config) -> Path:
    return config.paths.bulk / "pin_reports.sqlite"


class CourtPin:
    name = "court.pin_reports"
    stage = STAGE

    def available(self, ctx: Context) -> Availability:
        if not ctx.config.court.pin_report_urls:
            return Availability.unavailable(
                "court.pin_report_urls is empty: supply the PIN report URLs (see docs/sources.md)"
            )
        if _backend() is None:
            return Availability.unavailable(
                "reading the reports needs pdfplumber or pypdf (the documents extra)"
            )
        return Availability.available()

    def applies(self, row: Row, ctx: Context) -> bool | str:
        task: CourtTask = row  # type: ignore[assignment]
        if not task.cs.classification.federal_strategies_apply:
            return "classified as a state case"
        if (row.year or 0) > LAST_YEAR:
            return "the DOJ press-release API covers this year; the reports are for earlier rows"
        return True if task.cs.parts.usable else "no usable name"

    def _ensure_indexed(self, ctx: Context, index: PinIndex, task: CourtTask) -> None:
        backend = _backend()
        if backend is None:
            return
        for url in ctx.config.court.pin_report_urls:
            if index.indexed(url):
                continue
            try:
                record = cap(ctx).fetch(url, row_id=None, stage=STAGE, kind="page")
            except CaptureError as exc:
                task.cs.notes.append(f"PIN report not captured: {exc}"[:200])
                continue
            if not record.ok:
                task.cs.notes.append(f"PIN report {url}: HTTP {record.http_status}"[:200])
                continue
            index.index_pages(url, record.capture_id, backend(cap(ctx).read_body(record)))

    def run(self, row: Row, ctx: Context) -> ModuleResult:
        task: CourtTask = row  # type: ignore[assignment]
        cs = task.cs
        index = PinIndex(index_path(ctx.config))
        self._ensure_indexed(ctx, index, task)
        found = index.find(cs.parts)
        cs.flags["pin"] = {"entries": index.count(), "matched": len(found)}
        any_case = False
        for url, pdf_capture, page, paragraph in found[:3]:
            numbers = _CASE.findall(paragraph)
            district = doj.DISTRICT.search(paragraph)
            text_id = store_text(
                ctx, task.row_id, parent_capture_id=pdf_capture, url=url, text=paragraph,
                tool="pin_report_text", extra={"page": page},
            )  # fmt: skip
            cs.located.append(
                Located(
                    source_kind="pin_report",
                    jurisdiction="US",
                    court=district.group(0) if district else None,
                    case_number=numbers[0] if numbers else None,
                    document_kind="report_entry",
                    url=url,
                    strategy=self.name,
                    capture_id=pdf_capture,
                    text=paragraph,
                    text_capture_id=text_id,
                    basis={"page": page},
                    weak=True,  # a report paragraph carries no state or date to corroborate
                )
            )
            any_case = any_case or bool(numbers)
        return ModuleResult(Outcome.hit if any_case else Outcome.miss)

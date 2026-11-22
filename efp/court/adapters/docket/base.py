"""The shared base of the automated docket adapters.

Every request goes through the capture layer (budgets, retries, WARC, a ``captures`` row). A
portal that refuses automated requests (HTTP 401/403) raises :class:`SiteBlocked` (from the AG
framework: the same rules apply, one refusal defers the row and only repeated refusals change a
state's recorded mode). Nothing here gets around what a portal does.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from efp.capture import CaptureRecord
from efp.court.adapters.ag.base import SiteBlocked
from efp.court.adapters.docket import DocketHit
from efp.court.located import STAGE, cap
from efp.types import Context


class DocketPortal:
    """One state's automated docket adapter: its declared facts and the search it implements."""

    state: ClassVar[str]
    portal: ClassVar[str]  # the search entry point
    platform: ClassVar[str] = "other"
    terms_url: ClassVar[str] = ""
    needs_js: ClassVar[bool] = (
        False  # results render only in a browser (Browsertrix, terms allowing)
    )

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.config = ctx.config
        self.search_capture_id: int | None = None  # the capture of the last search answer

    @property
    def adapter_module(self) -> str:
        return type(self).__module__

    def search(self, name: str, year: int, window: int = 2) -> list[DocketHit]:
        raise NotImplementedError  # pragma: no cover  (every adapter overrides this)

    def _checked(self, record: CaptureRecord) -> CaptureRecord:
        if record.http_status in (401, 403):
            raise SiteBlocked(f"{self.portal}: HTTP {record.http_status} to automated requests")
        return record

    def get(self, url: str, *, row_id: int | None = None) -> CaptureRecord:
        if self.needs_js:  # a JS-only portal (terms cleared): one WACZ capture, Docker permitting
            browsed: CaptureRecord = cap(self.ctx).browser(url, row_id=row_id, stage=STAGE)
            return browsed
        record: CaptureRecord = cap(self.ctx).fetch(url, row_id=row_id, stage=STAGE, kind="page")
        return self._checked(record)

    def post_json(self, url: str, payload: dict[str, Any], *, row_id: int | None = None) -> Any:
        """POST a JSON search and return the decoded answer (a non-JSON answer raises)."""
        record: CaptureRecord = cap(self.ctx).fetch(
            url,
            row_id=row_id,
            stage=STAGE,
            kind="api",
            method="POST",
            data=json.dumps(payload, sort_keys=True).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        self._checked(record)
        self.search_capture_id = record.capture_id  # a hit's provenance is this answer
        if not record.ok:
            from efp.capture import CaptureError

            raise CaptureError(f"{url}: HTTP {record.http_status}")
        try:
            return json.loads(cap(self.ctx).read_body(record))
        except ValueError as exc:
            from efp.capture import CaptureError

            raise CaptureError(f"{url}: the answer is not JSON (a portal error page?)") from exc

    def fetch(self, hit: DocketHit, *, row_id: int | None = None) -> CaptureRecord:
        """Capture the docket page (and so its evidence) for a hit."""
        return self.get(hit.url, row_id=row_id)

"""States whose AG site cannot be harvested automatically, each with the reason (probed 2026-11-22).

A ``manual`` state is recorded in ``state_adapters`` with its reason, is skipped by the harvester
with that reason, and is listed in the build report as a gated source with its row count. Nothing
here tries to get around what the site does.
"""

from __future__ import annotations

from typing import ClassVar

from efp.court.adapters.ag import Release, register
from efp.court.adapters.ag.base import SiteBlocked, StateAdapter

PROBED = "2026-11-22"


class ManualAdapter(StateAdapter):
    mode: ClassVar[str] = "manual"
    search_mode: ClassVar[str] = "none"

    def list_page(self, cursor: str) -> tuple[list[Release], str | None]:
        raise SiteBlocked(f"{self.state} is manual: {self.reason}")


def _manual(state: str, portal: str, reason: str) -> None:
    register(
        type(
            f"{state}Manual", (ManualAdapter,), {"state": state, "portal": portal, "reason": reason}
        )
    )


_BLOCKED = f"the site answers HTTP 403 to automated requests (checked {PROBED})"
_MANUAL = (
    ("FL", "https://www.myfloridalegal.com", _BLOCKED),
    ("NC", "https://ncdoj.gov", _BLOCKED),
    (
        "MN",
        "https://www.ag.state.mn.us",
        "no release archive or search: releases are reachable only from curated pages, "
        "with no index to walk",
    ),
    (
        "WI",
        "https://www.wisdoj.gov",
        "releases are published as PDF files only; reading them needs the optional PDF reader",
    ),
    (
        "KY",
        "https://www.ag.ky.gov",
        "the news list is loaded by script ('Load more'); the server returns no list or feed",
    ),
    (
        "IN",
        "https://www.in.gov/attorneygeneral",
        "the newsroom list is loaded by script; the page carries no server-rendered releases, "
        "feed or archive",
    ),
)
for _state, _portal, _reason in _MANUAL:
    _manual(_state, _portal, _reason)

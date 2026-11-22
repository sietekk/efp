"""State attorney-general and district-attorney press-release adapters, one module per state.

The whole harvester is the ``court.state_ag`` module of the court chain (``module.py``); inside it
each state is a swappable adapter behind the protocol below. A state with no adapter, or whose
adapter is ``manual`` or ``broken``, is skipped with its reason and the court chain continues.

The framework (``base.py``) is modelled on biglocalnews/warn-scraper: one small module per state,
a shared fetch/cache/retry layer (here the capture layer: robots.txt, budgets, retries, WARC) and a
registry. ``coverage.py`` renders what is covered as ``docs/coverage_matrix.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from efp.capture import CaptureRecord as Capture


class AGScraper(Protocol):
    state: str

    def search(self, name: str, year: int, window: int = 2) -> list[Release]: ...

    def fetch(self, release: Release) -> Capture: ...


@dataclass(frozen=True)
class Release:
    url: str
    title: str
    date: date | None
    office: str  # 'AG' | 'DA:<county>'
    snippet: str


# --- the registry ------------------------------------------------------------------------------

_ADAPTERS: dict[str, type] = {}


def register(cls: type) -> type:
    """Class decorator: make a state adapter known to the harvester."""
    _ADAPTERS[str(cls.state)] = cls  # type: ignore[attr-defined]
    return cls


def adapters() -> dict[str, type]:
    """Every registered adapter by state (the state modules are imported on first use)."""
    from efp.court.adapters.ag import states  # noqa: F401  (importing registers them)

    return dict(sorted(_ADAPTERS.items()))

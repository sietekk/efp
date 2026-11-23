"""State docket-portal adapters (build 09): the spec's three modes, one module per state.

A state's docket portal is recorded in ``state_adapters`` (``kind = 'docket'``) as ``automated``
(search and retrieve by name and year without a human), ``assisted`` (the pipeline constructs the
query, the operator runs it in a browser, the result enters through the manual import path) or
``manual`` (no online docket search, or one that wants an account or a fee). The adapters here are
the ``automated`` ones; they run only for a state the operator has cleared in
``court.docket_terms_cleared`` after reading that portal's terms. ``probe.py`` classifies the
portals, ``query.py`` builds the assisted query, ``module.py`` is the ``court.state_docket``
module of the court chain.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from efp.capture import CaptureRecord as Capture


@dataclass(frozen=True)
class DocketHit:
    court: str
    case_number: str
    caption: str
    filed_date: date | None
    url: str
    document_urls: tuple[str, ...] = ()


class DocketAdapter(Protocol):
    state: str

    def search(self, name: str, year: int, window: int = 2) -> list[DocketHit]: ...

    def fetch(self, hit: DocketHit) -> Capture: ...


_ADAPTERS: dict[str, Any] = {}


def register(cls: Any) -> Any:
    """Class decorator: make a state's docket adapter known."""
    _ADAPTERS[str(cls.state)] = cls
    return cls


def adapters() -> dict[str, Any]:
    """Every registered docket adapter by state (the state modules are imported on first use)."""
    from efp.court.adapters.docket import states  # noqa: F401  (importing registers them)

    return dict(sorted(_ADAPTERS.items()))

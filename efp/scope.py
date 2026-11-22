"""Scope rules shared by ingest, the runner (build 04) and export (build 19).

``rows.in_scope`` is fixed at ingest from the year window alone. The population choice (decision 2)
is applied at query time with :func:`population_sql`, never stored, because ``rows`` is immutable
and the decision can change after ingest.
"""

from __future__ import annotations

from typing import Literal

from efp.config import Config

Population = Literal["all", "convictions"]

CONVICTION_PREFIX = "Criminal Conviction"


def in_window(year: int, config: Config) -> bool:
    """True iff ``scope.year_from <= year <= scope.year_to`` (2000 to 2025 by default)."""
    return config.scope.year_from <= year <= config.scope.year_to


def population_sql(population: Population, *, alias: str = "r") -> str:
    """A SQL condition (no leading AND) selecting the study population from ``rows``.

    ``all``          every in-scope row.
    ``convictions``  in-scope rows whose case type begins with "Criminal Conviction"
                     (case-sensitive: a GLOB, not a LIKE).
    """
    if not alias.isidentifier():
        raise ValueError("alias must be a plain identifier")
    if population == "all":
        return f"{alias}.in_scope = 1"
    if population == "convictions":
        # GLOB is case-sensitive; LIKE is not. "Begins with" means exactly this capitalisation.
        return f"{alias}.in_scope = 1 AND {alias}.case_type GLOB '{CONVICTION_PREFIX}*'"
    raise ValueError(f"unknown population {population!r}")

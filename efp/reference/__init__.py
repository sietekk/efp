"""Reference datasets and loaders (build 11): the bulk files loaded once into the ``ref_*`` tables,
the candidate index build 12 matches against, and the enrichment-only API clients.

Bulk before API: the candidate universe is matched locally, and OpenFEC, Congress.gov and OpenStates
are called only to enrich an already-confirmed match (by ID, never by name). Raw files live under
``paths.bulk`` and are never committed. See docs/sources.md for each dataset's URL, vintage, hash,
license and attribution sentence, and docs/license_register.md.
"""

from __future__ import annotations

from efp.modules import Registry
from efp.reference.base import Dataset
from efp.reference.candidates import Congress, Dime, FecCandidates, Ftm, HarvardLocal, OpenStates
from efp.reference.population import CensusPopulation, CensusSurnames, SsaNames
from efp.reference.wmn import Wmn

DATASET_CLASSES: tuple[type[Dataset], ...] = (
    CensusSurnames,
    SsaNames,
    CensusPopulation,
    FecCandidates,
    Congress,
    OpenStates,
    HarvardLocal,
    Dime,
    Ftm,
    Wmn,
)


def datasets() -> dict[str, Dataset]:
    return {cls.name: cls() for cls in DATASET_CLASSES}


class _ModuleView:
    """Registers a dataset as the Module ``reference.<name>`` the config chain names."""

    def __init__(self, dataset: type[Dataset]) -> None:
        self.name = f"reference.{dataset.name}"
        self.stage = "reference"
        self._dataset = dataset

    def available(self, ctx):  # type: ignore[no-untyped-def]
        return self._dataset().available(ctx)

    def applies(self, row, ctx):  # type: ignore[no-untyped-def]
        return self._dataset().applies(row, ctx)

    def run(self, row, ctx):  # type: ignore[no-untyped-def]
        return self._dataset().run(row, ctx)


def register_modules(registry: Registry) -> None:
    for cls in DATASET_CLASSES:
        view = _ModuleView(cls)
        if registry.get(view.name) is None:
            registry.register(view)

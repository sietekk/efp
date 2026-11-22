"""Registration of the court chain's modules, including those later builds will implement."""

from __future__ import annotations

from efp.court import bulk, courtlistener, doj, pin, recap
from efp.court.adapters.ag.module import CourtStateAg
from efp.court.adapters.docket.module import CourtStateDocket
from efp.modules import Registry

MODULES = (
    doj.CourtDoj,
    pin.CourtPin,
    CourtStateAg,
    bulk.CourtBulk,
    courtlistener.CourtApi,
    recap.CourtRecap,
    CourtStateDocket,
)


def register_modules(registry: Registry) -> None:
    for module in MODULES:
        if registry.get(module.name) is None:
            registry.register(module)

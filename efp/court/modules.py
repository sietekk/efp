"""Registration of the court chain's modules, including those later builds will implement."""

from __future__ import annotations

from efp.court import bulk, courtlistener, doj, pin, recap
from efp.court.located import STAGE
from efp.modules import Availability, ModuleResult, Outcome, Registry
from efp.types import Context, Row


class _NotBuilt:
    stage = STAGE
    name = ""
    build = ""
    what = ""

    def available(self, ctx: Context) -> Availability:
        return Availability.unavailable(f"not built until build {self.build} ({self.what})")

    def applies(self, row: Row, ctx: Context) -> bool | str:
        return True

    def run(self, row: Row, ctx: Context) -> ModuleResult:  # never reached while unavailable
        return ModuleResult(Outcome.miss)


class CourtStateAg(_NotBuilt):
    name = "court.state_ag"
    build = "08"
    what = "state attorney general and district attorney press releases"


class CourtStateDocket(_NotBuilt):
    name = "court.state_docket"
    build = "09"
    what = "state docket adapters"


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

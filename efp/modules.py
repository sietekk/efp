"""The module chain: every stage's work is done by swappable modules behind one interface.

A module says whether it can run at all (:meth:`Module.available`), whether it fits a row
(:meth:`Module.applies`) and does the work (:meth:`Module.run`). ``config.toml`` lists each
stage's modules in order with a policy. A module that is unavailable or does not apply is skipped
with its reason recorded and the next one runs; a row is never failed because a module could not
run. Only a module that ran and raised (or returned ``error``) counts against the row.

Policies:

``first_hit``         stop at the first module that produces the stage's result.
``all``               run every applicable module.
``first_available``   run only the first module that is available and applies.
``ladder``            try modules in order until one meets the quality bar.

The registry is an explicit dict: later builds call :func:`register` from their own package's
``__init__``; nothing is discovered by import-time magic.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from efp.config import Config
from efp.logging import redact
from efp.types import Context, Row

POLICIES = ("first_hit", "all", "first_available", "ladder")


@dataclass(frozen=True)
class Availability:
    ok: bool
    reason: str = ""

    @classmethod
    def available(cls) -> Availability:
        return cls(True)

    @classmethod
    def unavailable(cls, reason: str) -> Availability:
        return cls(False, reason)


class Outcome(StrEnum):
    hit = "hit"
    miss = "miss"
    error = "error"


@dataclass
class ModuleResult:
    outcome: Outcome
    wrote: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    quality: float | None = None  # for ``ladder``: compared with the stage's quality bar


@runtime_checkable
class Module(Protocol):
    name: str
    stage: str

    def available(self, ctx: Context) -> Availability: ...

    def applies(self, row: Row, ctx: Context) -> bool | str:
        """True if the module fits this row; a string is the reason it does not."""
        ...

    def run(self, row: Row, ctx: Context) -> ModuleResult: ...


class RegistryError(Exception):
    pass


class Registry:
    def __init__(self) -> None:
        self._modules: dict[str, type[Module] | Module] = {}

    def register(self, module: type[Module] | Module) -> None:
        name = module.name
        if name in self._modules:
            raise RegistryError(f"module {name!r} is already registered")
        self._modules[name] = module

    def get(self, name: str) -> Module | None:
        found = self._modules.get(name)
        if found is None:
            return None
        return found() if isinstance(found, type) else found

    def names(self) -> list[str]:
        return sorted(self._modules)


REGISTRY = Registry()


def register(module: type[Module] | Module) -> None:
    REGISTRY.register(module)


@dataclass(frozen=True)
class Chain:
    """One configured chain: ``identity`` has two (candidates, parameters); ``social`` has two."""

    stage: str
    name: str
    modules: tuple[str, ...]
    policy: str


# (config attribute prefix) per stage: "" means the plain ``modules``/``policy`` keys.
_CHAIN_KEYS: dict[str, tuple[tuple[str, str], ...]] = {
    "identity": (("candidate", "candidate_"), ("parameter", "parameter_")),
    "social": (("enumerate", "enumerate_"), ("retrieve", "retrieve_")),
    "validate": (("label", "label_"),),
}

# Configured chains that belong to commands rather than to runner stages; ``efp modules`` lists
# them too, because the gated-source list in the report must not leave them out.
COMMAND_CHAINS = ("import", "archive", "validate", "reference")
_ATTR = {"import": "import_", "validate": "validate_"}  # keyword / BaseModel clashes


def chains_for(config: Config, stage: str) -> list[Chain]:
    """The chains configured for a stage. ``code`` has none (it is not module-driven)."""
    section = getattr(config.stages, _ATTR.get(stage, stage), None)
    if section is None:
        return []
    chains: list[Chain] = []
    for chain_name, prefix in _CHAIN_KEYS.get(stage, (("modules", ""),)):
        modules = getattr(section, f"{prefix}modules")
        policy = getattr(section, f"{prefix}policy")
        chains.append(Chain(stage, chain_name, tuple(modules), str(policy)))
    return chains


@dataclass
class ChainRun:
    """What one pass over one chain did for one row."""

    records: list[dict[str, Any]] = field(default_factory=list)
    hits: int = 0
    errors: list[str] = field(default_factory=list)
    wrote: dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        """Errors only count against the row when nothing produced the stage's result."""
        return bool(self.errors) and self.hits == 0


QualityBar = Callable[[ModuleResult], bool]


def _meets_default(result: ModuleResult) -> bool:
    return result.outcome is Outcome.hit and (result.quality is None or result.quality > 0)


def run_chain(
    chain: Chain,
    row: Row,
    ctx: Context,
    registry: Registry = REGISTRY,
    *,
    quality_bar: QualityBar | None = None,
) -> ChainRun:
    """Run one chain for one row under its policy and record every module's outcome."""
    if chain.policy not in POLICIES:
        raise RegistryError(f"unknown policy {chain.policy!r} for {chain.stage}.{chain.name}")
    bar = quality_bar or _meets_default
    out = ChainRun()

    def note(name: str, status: str, reason: str = "", ms: int = 0) -> None:
        out.records.append(
            {"chain": chain.name, "name": name, "status": status, "reason": reason, "ms": ms}
        )

    for name in chain.modules:
        module = registry.get(name)
        if module is None:
            note(name, "skipped", "not registered (its build has not landed)")
            continue
        avail = module.available(ctx)
        if not avail.ok:
            note(name, "skipped", avail.reason or "unavailable")
            continue
        fits = module.applies(row, ctx)
        if fits is not True:
            note(name, "skipped", fits if isinstance(fits, str) and fits else "does not apply")
            continue
        started = time.perf_counter()
        try:
            result = module.run(row, ctx)
        except Exception as exc:  # a module bug must not take the row's whole batch with it
            result = ModuleResult(Outcome.error, error=redact(f"{type(exc).__name__}: {exc}"))
        ms = int((time.perf_counter() - started) * 1000)
        if result.outcome is Outcome.error:
            message = redact(result.error or "error")
            out.errors.append(f"{name}: {message}")
            note(name, "error", message, ms)
        else:
            if result.outcome is Outcome.hit:
                out.hits += 1
                out.wrote[name] = result.wrote
            note(name, result.outcome.value, "", ms)
        if chain.policy == "first_hit" and result.outcome is Outcome.hit:
            break
        if chain.policy == "first_available":
            break
        if chain.policy == "ladder" and bar(result):
            break
    return out


def run_stage(
    config: Config,
    stage: str,
    row: Row,
    ctx: Context,
    registry: Registry = REGISTRY,
    *,
    quality_bar: QualityBar | None = None,
) -> tuple[str, dict[str, Any], str | None]:
    """Run every chain of a stage. Returns ``(status, summary_json, error)``.

    ``done`` when the policies are satisfied or every module was attempted; ``failed`` only when
    a chain produced nothing and at least one module raised, so the row can be retried.
    """
    modules: list[dict[str, Any]] = []
    errors: list[str] = []
    wrote: dict[str, Any] = {}
    for chain in chains_for(config, stage):
        run = run_chain(chain, row, ctx, registry, quality_bar=quality_bar)
        modules.extend(run.records)
        wrote.update(run.wrote)
        if run.failed:
            errors.extend(run.errors)
    summary: dict[str, Any] = {"modules": modules, "wrote": wrote}
    if errors:
        return "failed", summary, redact("; ".join(errors))[:500]
    return "done", summary, None


@dataclass(frozen=True)
class ModuleStatus:
    name: str
    stage: str
    chain: str
    registered: bool
    ok: bool
    reason: str


def describe(
    config: Config, ctx: Context, registry: Registry = REGISTRY, stage: str | None = None
) -> list[ModuleStatus]:
    """Every configured module with its availability: the gated-source list for the report."""
    rows: list[ModuleStatus] = []
    for stage_name in [*config.runner.stages, *COMMAND_CHAINS]:
        if stage is not None and stage_name != stage:
            continue
        for chain in chains_for(config, stage_name):
            for name in chain.modules:
                module = registry.get(name)
                if module is None:
                    rows.append(
                        ModuleStatus(name, stage_name, chain.name, False, False, "not built yet")
                    )
                    continue
                avail = module.available(ctx)
                rows.append(
                    ModuleStatus(name, stage_name, chain.name, True, avail.ok, avail.reason)
                )
    return rows

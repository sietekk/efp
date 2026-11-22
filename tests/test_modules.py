from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from efp import modules as chain
from efp.cli import app
from efp.config import Config
from efp.modules import Availability, Chain, ModuleResult, Outcome, Registry
from efp.runner import make_context
from efp.types import Context, Row

runner = CliRunner()


class Fake:
    """A module whose availability, fit and result are set by the test."""

    def __init__(
        self,
        name: str,
        *,
        ok: bool = True,
        why: str = "",
        applies: bool | str = True,
        outcome: Outcome = Outcome.hit,
        quality: float | None = None,
        boom: bool = False,
        stage: str = "court",
    ) -> None:
        self.name, self.stage = name, stage
        self._ok, self._why, self._applies = ok, why, applies
        self._outcome, self._quality, self._boom = outcome, quality, boom
        self.ran = 0

    def available(self, ctx: Context) -> Availability:
        return Availability(self._ok, self._why)

    def applies(self, row: Row, ctx: Context) -> bool | str:
        return self._applies

    def run(self, row: Row, ctx: Context) -> ModuleResult:
        self.ran += 1
        if self._boom:
            raise RuntimeError("kaput")
        return ModuleResult(
            self._outcome,
            wrote={"n": 1},
            quality=self._quality,
            error="bad" if self._outcome is Outcome.error else None,
        )


def _registry(*modules: Fake) -> Registry:
    registry = Registry()
    for module in modules:
        registry.register(module)
    return registry


def _run(
    policy: str, *modules: Fake, bar: Callable[[ModuleResult], bool] | None = None
) -> chain.ChainRun:
    cfg_ctx = Context.model_construct(config=None, db=None, capture=None, log=None)  # type: ignore[arg-type]
    row = Row(row_id=1)
    spec = Chain("court", "modules", tuple(m.name for m in modules), policy)
    return chain.run_chain(spec, row, cfg_ctx, _registry(*modules), quality_bar=bar)


def _statuses(run: chain.ChainRun) -> list[tuple[str, str]]:
    return [(r["name"], r["status"]) for r in run.records]


def test_first_hit_skips_unavailable_and_stops_at_the_first_hit() -> None:
    a = Fake("a", ok=False, why="no key")
    b = Fake("b", outcome=Outcome.miss)
    c, d = Fake("c"), Fake("d")
    run = _run("first_hit", a, b, c, d)
    assert _statuses(run) == [("a", "skipped"), ("b", "miss"), ("c", "hit")]
    assert run.records[0]["reason"] == "no key"
    assert (a.ran, b.ran, c.ran, d.ran) == (0, 1, 1, 0)
    assert not run.failed


def test_all_runs_every_applicable_module() -> None:
    a, b = Fake("a"), Fake("b", applies="year window does not reach 2017")
    c = Fake("c", outcome=Outcome.miss)
    run = _run("all", a, b, c)
    assert _statuses(run) == [("a", "hit"), ("b", "skipped"), ("c", "miss")]
    assert run.records[1]["reason"] == "year window does not reach 2017"
    assert run.hits == 1


def test_first_available_runs_only_the_first_module_that_can() -> None:
    a = Fake("a", ok=False, why="Docker absent")
    b, c = Fake("b", outcome=Outcome.miss), Fake("c")
    run = _run("first_available", a, b, c)
    assert _statuses(run) == [("a", "skipped"), ("b", "miss")]
    assert c.ran == 0


def test_ladder_stops_at_the_first_module_that_meets_the_bar() -> None:
    a = Fake("a", quality=0.2)
    b = Fake("b", quality=0.9)
    c = Fake("c", quality=1.0)
    run = _run("ladder", a, b, c, bar=lambda r: (r.quality or 0) >= 0.8)
    assert _statuses(run) == [("a", "hit"), ("b", "hit")]
    assert c.ran == 0


def test_an_unavailable_module_never_fails_a_row() -> None:
    run = _run("first_hit", Fake("a", ok=False, why="gated"), Fake("b", applies=False))
    assert run.errors == [] and not run.failed


def test_an_unregistered_module_is_skipped_with_a_reason() -> None:
    cfg_ctx = Context.model_construct(config=None, db=None, capture=None, log=None)  # type: ignore[arg-type]
    spec = Chain("court", "modules", ("court.not_built",), "first_hit")
    run = chain.run_chain(spec, Row(row_id=1), cfg_ctx, Registry())
    assert run.records[0]["status"] == "skipped"
    assert "not registered" in run.records[0]["reason"]


def test_a_raising_module_is_an_error_and_the_chain_goes_on() -> None:
    a, b = Fake("a", boom=True), Fake("b")
    run = _run("first_hit", a, b)
    assert _statuses(run) == [("a", "error"), ("b", "hit")]
    assert run.errors and not run.failed  # b produced the result, so the row is not failed


def test_errors_with_no_hit_fail_the_chain() -> None:
    run = _run("all", Fake("a", outcome=Outcome.error), Fake("b", outcome=Outcome.miss))
    assert run.failed


def test_an_unknown_policy_is_an_error() -> None:
    with pytest.raises(chain.RegistryError, match="policy"):
        _run("sometimes", Fake("a"))


def test_a_module_name_registers_once() -> None:
    registry = _registry(Fake("a"))
    with pytest.raises(chain.RegistryError, match="already registered"):
        registry.register(Fake("a"))


def test_chains_follow_the_config(config: Config) -> None:
    court = chain.chains_for(config, "court")
    assert [c.policy for c in court] == ["first_hit"] and court[0].modules[0] == "court.doj"
    assert [c.name for c in chain.chains_for(config, "identity")] == ["candidate", "parameter"]
    assert [c.name for c in chain.chains_for(config, "social")] == ["enumerate", "retrieve"]
    assert chain.chains_for(config, "code") == []


def test_run_stage_records_modules_and_is_done_when_nothing_could_run(config: Config) -> None:
    ctx = make_context(config, None)
    status, summary, error = chain.run_stage(config, "court", Row(row_id=1), ctx, Registry())
    assert (status, error) == ("done", None)  # nothing registered: every module skipped
    assert {m["status"] for m in summary["modules"]} == {"skipped"}


def test_run_stage_fails_only_when_a_module_raised_and_nothing_hit(config: Config) -> None:
    ctx = make_context(config, None)
    names = chain.chains_for(config, "court")[0].modules
    registry = _registry(Fake(names[0], boom=True))
    status, summary, error = chain.run_stage(config, "court", Row(row_id=1), ctx, registry)
    assert status == "failed" and "kaput" in (error or "")
    assert summary["modules"][0]["status"] == "error"


def test_describe_lists_availability_with_reasons(full_config: Config) -> None:
    ctx = make_context(full_config, None)
    registry = _registry(Fake("court.doj", ok=False, why="gated source"))
    rows = {r.name: r for r in chain.describe(full_config, ctx, registry)}
    assert rows["court.doj"].reason == "gated source" and not rows["court.doj"].ok
    assert rows["news.gdelt"].registered is False and rows["news.gdelt"].reason == "not built yet"
    only = chain.describe(full_config, ctx, registry, stage="news")
    assert {r.stage for r in only} == {"news"}


def test_modules_command_lists_every_configured_module(
    full_config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EFP_CONFIG", str(full_config_path))
    result = runner.invoke(app, ["modules"])
    assert result.exit_code == 0, result.output
    assert "court.doj" in result.output and "not built yet" in result.output
    only = runner.invoke(app, ["modules", "--stage", "news"])
    assert "news.gdelt" in only.output and "court.doj" not in only.output
    assert runner.invoke(app, ["modules", "--stage", "nope"]).exit_code == 2
    archive = runner.invoke(app, ["modules", "--stage", "archive"])  # a command chain, not a stage
    assert archive.exit_code == 0 and "archive.spn" in archive.output

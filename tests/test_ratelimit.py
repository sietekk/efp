from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest

from efp.config import Config, load_config
from efp.ratelimit import DomainLimiter, RateLimitTimeout, interval_ms


@pytest.fixture(autouse=True)
def _close_limiters(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Every limiter a test builds is closed afterwards, so no leaker thread outlives its database."""
    made: list[DomainLimiter] = []
    original = DomainLimiter.__init__

    def tracking(self: DomainLimiter, *args: object, **kwargs: object) -> None:
        original(self, *args, **kwargs)  # type: ignore[arg-type]
        made.append(self)

    monkeypatch.setattr(DomainLimiter, "__init__", tracking)
    yield
    for limiter in made:
        limiter.close()


def test_interval_is_the_inverse_of_the_budget() -> None:
    assert interval_ms(0.5) == 2000  # the default: one request per two seconds
    assert interval_ms(0.32) == 3125  # 80% of Wayback's published ~0.4/s
    assert interval_ms(3.0) == 334
    with pytest.raises(ValueError):
        interval_ms(0)


def test_budgets_in_force_come_from_the_configuration(config: Config) -> None:
    limiter = DomainLimiter(config)
    assert limiter.budget("zzz.test").describe() == "zzz.test: 1 request per 2 s (0.5/s)"
    assert limiter.budget("EXAMPLE.test").rps == 1.0  # hostnames are case-insensitive
    assert limiter.budget("example.test").daily_cap == 10
    assert {b.domain for b in limiter.budgets()} >= {"example.test"}


def test_a_burst_past_the_budget_blocks_for_the_expected_interval(config: Config) -> None:
    limiter = DomainLimiter(config)
    limiter.define("burst.test", 5.0)  # one request every 200 ms
    started = time.monotonic()
    waits = [limiter.acquire("burst.test") for _ in range(4)]
    elapsed = time.monotonic() - started
    assert 0.55 <= elapsed < 2.0  # three intervals of 200 ms between four requests
    assert waits[0] < 0.1 and max(waits[1:]) >= 0.15


def test_each_domain_has_its_own_bucket(config: Config) -> None:
    limiter = DomainLimiter(config)
    limiter.define("a.test", 2.0)
    limiter.define("b.test", 2.0)
    started = time.monotonic()
    limiter.acquire("a.test")
    limiter.acquire("b.test")  # a different domain does not wait for a.test
    assert time.monotonic() - started < 0.3


def test_the_budget_survives_a_restart(config: Config) -> None:
    first = DomainLimiter(config)
    first.define("persist.test", 2.0)  # 500 ms
    first.acquire("persist.test")
    first.close()
    second = DomainLimiter(config)  # a new process would build exactly this
    second.define("persist.test", 2.0)
    assert second.acquire("persist.test") >= 0.3  # still inside the previous request's interval


def test_a_budget_that_cannot_free_up_in_time_raises(config: Config) -> None:
    limiter = DomainLimiter(config)
    limiter.define("slow.test", 0.5)  # 2 s between requests
    limiter.acquire("slow.test")
    with pytest.raises(RateLimitTimeout, match=r"slow\.test"):
        limiter.acquire("slow.test", max_wait=0.2)


def test_a_daily_cap_is_enforced_in_the_same_bucket(
    write_config: Callable[..., Path],
) -> None:
    config = load_config(
        write_config({'"example.test" = 10': '"example.test" = 2'})  # cap of 2 per day
    )
    limiter = DomainLimiter(config)
    limiter.define("example.test", 1000.0)
    limiter.acquire("example.test")
    limiter.acquire("example.test")
    with pytest.raises(RateLimitTimeout):
        limiter.acquire("example.test", max_wait=0.3)  # the third is over the daily cap


def test_two_processes_share_one_budget(write_config: Callable[..., Path], tmp_path: Path) -> None:
    import subprocess
    import sys

    from tests.conftest import REPO

    config_path = write_config()
    go = tmp_path / "go"
    outs = [tmp_path / "a.txt", tmp_path / "b.txt"]
    procs = [
        subprocess.Popen(  # noqa: S603  (our own interpreter and module)
            [
                sys.executable,
                "-m",
                "tests.limiter_worker",
                str(config_path),
                "shared.test",
                "5",
                str(out),
                str(go),
            ],
            cwd=REPO,
        )
        for out in outs
    ]
    time.sleep(1.0)  # both are past their imports, waiting on the barrier
    go.touch()
    assert [p.wait(timeout=60) for p in procs] == [0, 0]
    stamps = sorted(float(x) for out in outs for x in out.read_text().split())
    assert len(stamps) == 10
    # Ten requests at one per 200 ms take about 1.8 s however the two processes interleave; two
    # processes with separate budgets would finish in about 0.8 s. The span is immune to the
    # jitter in when each process happens to write its timestamp.
    assert stamps[-1] - stamps[0] >= 1.5


def test_a_daily_cap_of_one_is_valid_and_allows_exactly_one_request(
    write_config: Callable[..., Path],
) -> None:
    config = load_config(write_config({'"example.test" = 10': '"example.test" = 1'}))
    limiter = DomainLimiter(config)
    limiter.define("example.test", 1000.0)
    limiter.acquire("example.test")
    with pytest.raises(RateLimitTimeout):
        limiter.acquire("example.test", max_wait=0.2)

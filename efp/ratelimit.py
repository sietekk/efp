"""Per-domain request budgets that survive restarts and are shared by every efp process.

One bucket per domain in a SQLite file under ``paths.cache`` (pyrate-limiter 4.x, file-locked so two
runners share one budget). A budget of ``rps`` requests per second is enforced as evenly spaced
requests (one every ``1/rps`` seconds), never as a burst, so 0.5 means one request per two
seconds. ``rate_limits.daily_caps`` adds a second, per-day limit in the same bucket.

Budgets are 80% of a published limit, or ``rate_limits.default_rps`` (1 per 2 s) where none is
published; this module only applies the configured numbers.
"""

from __future__ import annotations

import math
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pyrate_limiter import (
    AbstractBucket,
    BucketFactory,
    Limiter,
    Rate,
    RateItem,
    SQLiteBucket,
)

from efp.config import Config

DAY_MS = 86_400_000


class RateLimitTimeout(Exception):
    """The budget did not free up within ``capture.max_wait_seconds``."""


@dataclass(frozen=True)
class Budget:
    domain: str
    rps: float
    interval_ms: int
    daily_cap: int | None

    def describe(self) -> str:
        every = self.interval_ms / 1000
        cap = f", {self.daily_cap}/day" if self.daily_cap is not None else ""
        return f"{self.domain}: 1 request per {every:g} s ({self.rps:g}/s){cap}"


def interval_ms(rps: float) -> int:
    """Milliseconds between requests for a budget of ``rps`` per second."""
    if rps <= 0:
        raise ValueError("rps must be positive")
    return max(1, math.ceil(1000 / rps))


def _table(name: str) -> str:
    return "b_" + re.sub(r"[^a-z0-9]", "_", name.lower())


class _Buckets(BucketFactory):
    """Routes each named budget to its own SQLite bucket (created on first use)."""

    def __init__(self, db_path: Path, budget_for: Callable[[str], Budget]) -> None:
        self._db_path = db_path
        self._budget_for = budget_for
        self._buckets: dict[str, AbstractBucket] = {}
        self._lock = threading.Lock()

    def wrap_item(self, name: str, weight: int = 1) -> RateItem:
        return RateItem(name, int(time.time() * 1000), weight=weight)

    def get(self, item: RateItem) -> AbstractBucket:
        with self._lock:
            bucket = self._buckets.get(item.name)
            if bucket is None:
                budget = self._budget_for(item.name)
                rates = [Rate(1, budget.interval_ms)]
                if budget.daily_cap is not None:
                    rates.append(Rate(budget.daily_cap, DAY_MS))
                bucket = self.create(
                    SQLiteBucket.init_from_file,  # type: ignore[arg-type]
                    rates,
                    table=_table(item.name),
                    db_path=str(self._db_path),
                    use_file_lock=True,
                )
                self._buckets[item.name] = bucket
            return bucket


class DomainLimiter:
    def __init__(self, config: Config, *, db_path: Path | None = None) -> None:
        self._config = config
        self._extra: dict[str, float] = {}  # named budgets that are not hostnames (e.g. "spn")
        cache_dir = config.paths.cache
        cache_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path or cache_dir / "ratelimit.sqlite"
        self._buckets = _Buckets(self._db_path, self.budget)
        self._limiter = Limiter(self._buckets, buffer_ms=0)

    def budget(self, domain: str) -> Budget:
        name = domain.lower()
        limits = self._config.rate_limits
        rps = self._extra.get(name)
        if rps is None:
            rps = limits.domains().get(name, limits.default_rps)
        return Budget(name, rps, interval_ms(rps), limits.daily_caps.get(name))

    def define(self, name: str, rps: float) -> None:
        """A budget for something that is not a hostname, e.g. ``spn`` at ``spn_per_minute/60``."""
        self._extra[name.lower()] = rps

    def acquire(self, domain: str, *, max_wait: float | None = None) -> float:
        """Block until the domain's budget allows a request. Returns seconds waited."""
        wait = self._config.capture.max_wait_seconds if max_wait is None else max_wait
        # Create the bucket first: the library timestamps the request before it looks the bucket
        # up, so a slow first-use (opening SQLite, taking the file lock) would otherwise make the
        # first request look earlier than it is and let it through too soon after another process's.
        self._buckets.get(RateItem(domain.lower(), int(time.time() * 1000)))
        started = time.monotonic()
        if not self._limiter.try_acquire(domain.lower(), blocking=True, timeout=wait):
            raise RateLimitTimeout(f"{domain}: budget not available within {wait:g} s")
        return time.monotonic() - started

    def budgets(self, domains: list[str] | None = None) -> list[Budget]:
        """The budgets in force: the default plus every domain named in the configuration."""
        limits = self._config.rate_limits
        names = domains or sorted({*limits.domains(), *limits.daily_caps})
        return [self.budget(n) for n in names]

    def close(self) -> None:
        """Stop the library's background leaker thread and close every bucket's connection."""
        self._limiter.close()

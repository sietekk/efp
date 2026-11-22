"""A worker process for the shared-budget test: acquire a domain's budget N times, log each time.

Usage: python -m tests.limiter_worker CONFIG DOMAIN N OUT GO
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from efp.config import load_config
from efp.ratelimit import DomainLimiter


def main(argv: list[str]) -> int:
    config = load_config(Path(argv[0]))
    domain, count, out, go = argv[1], int(argv[2]), Path(argv[3]), Path(argv[4])
    limiter = DomainLimiter(config)
    limiter.define(domain, 5.0)  # one request every 200 ms, the same in every process
    while not go.exists():
        time.sleep(0.005)
    for _ in range(count):
        limiter.acquire(domain)
        with out.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.time()}\n")
    limiter.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

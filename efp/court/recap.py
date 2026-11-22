"""RECAP Fetch and PACER: gated, and never used in the first pass.

RECAP Fetch needs the operator's PACER credentials, and PACER charges per page. The academic fee
exemption forbids internet redistribution while RECAP Fetch uploads to a public archive, which the
spec says are not combined without written clarification. So the client below exists, behind
``PACER_USER`` / ``PACER_PASS`` and ``court.recap_enabled`` (default false), and the first pass does
not turn it on. There is deliberately no function here that sends a purchase request: it is written
when the written clarification exists, not before. When a federal docket is found whose documents
are not in RECAP, it is written to ``queues/pacer.csv`` (under the data directory) instead,
for the operator to decide on.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from efp.config import Config
from efp.court.located import STAGE, CourtTask
from efp.modules import Availability, ModuleResult, Outcome
from efp.secrets import SecretsError, load_secrets
from efp.types import Context, Row

QUEUE_HEADER = ("row_id", "court", "case_number", "docket_id", "missing")


@dataclass(frozen=True)
class QueueItem:
    row_id: int
    court: str
    case_number: str
    docket_id: int | None
    missing: str

    def as_row(self) -> list[str]:
        return [
            str(self.row_id),
            self.court,
            self.case_number,
            "" if self.docket_id is None else str(self.docket_id),
            self.missing,
        ]


def queue_path(config: Config) -> Path:
    path = Path(config.court.pacer_queue).expanduser()
    # A relative path is under the data directory: the queue names row ids and case numbers, and
    # row-level data stays with the data.
    return path if path.is_absolute() else config.paths.data_dir / path


def append_queue(config: Config, items: list[QueueItem]) -> int:
    """Add dockets to the PACER queue, once each (a row and docket are not listed twice)."""
    if not items:
        return 0
    path = queue_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: set[tuple[str, str, str]] = set()
    if path.exists():
        with path.open(newline="", encoding="utf-8") as handle:
            for record in csv.reader(handle):
                if record and record[0] != "row_id":
                    existing.add((record[0], record[2], record[3]))
    fresh = [
        i
        for i in items
        if (str(i.row_id), i.case_number, "" if i.docket_id is None else str(i.docket_id))
        not in existing
    ]
    if not fresh:
        return 0
    new_file = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if new_file:
            writer.writerow(QUEUE_HEADER)
        writer.writerows(i.as_row() for i in fresh)
    return len(fresh)


def pacer_credentials(ctx: Context) -> tuple[str, str] | None:
    try:
        secrets = load_secrets(ctx.config.paths.env_file)
    except SecretsError:
        return None
    user, password = secrets.get("PACER_USER"), secrets.get("PACER_PASS")
    return (user, password) if user and password else None


class CourtRecap:
    name = "court.recap"
    stage = STAGE

    def available(self, ctx: Context) -> Availability:
        if not ctx.config.court.recap_enabled:
            return Availability.unavailable(
                "RECAP Fetch is off (court.recap_enabled = false): PACER charges per page and "
                "the fee exemption forbids RECAP's public uploads; the first pass never "
                "fetches from PACER"
            )
        if pacer_credentials(ctx) is None:
            return Availability.unavailable("PACER_USER / PACER_PASS not set")
        return Availability.available()

    def applies(self, row: Row, ctx: Context) -> bool | str:
        task: CourtTask = row  # type: ignore[assignment]
        if not any(item.source_kind == "federal_docket" for item in task.cs.located):
            return "no federal docket was found to fetch"
        return True

    def run(self, row: Row, ctx: Context) -> ModuleResult:
        # Reaching here means the operator enabled RECAP Fetch deliberately. Requesting purchases
        # needs a CourtListener court id and PACER case id per docket, which the bulk index and
        # search results carry; that wiring is left until the exemption question is answered in
        # writing, so this records the request rather than making it.
        task: CourtTask = row  # type: ignore[assignment]
        task.cs.notes.append("RECAP Fetch is enabled but purchases are not wired in the first pass")
        return ModuleResult(Outcome.miss)

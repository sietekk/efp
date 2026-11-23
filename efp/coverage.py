"""The coverage matrix: per state, what the AG harvester can do and what it found.

``docs/coverage_matrix.md`` is generated from ``state_adapters`` and the located records
(``efp status --coverage``): per state its row count, the AG adapter and its mode (with the reason
for every ``manual`` or ``broken``), and the rows with a current located record by strategy.
States with no adapter are listed as ``none`` with their row counts, so the order of future work
is visible.
"""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from efp.config import Config
from efp.court.adapters.ag import adapters
from efp.court.doj import STATES, normalize_state

FIRST_STATES = ("MN", "TX", "FL", "NC", "CA", "WI", "OH", "KY", "IN", "IL")
STRATEGIES = (
    "court.doj",
    "court.pin_reports",
    "court.state_ag",
    "court.courtlistener_bulk",
    "court.courtlistener_api",
    "court.state_docket",
)


@dataclass
class StateCoverage:
    state: str
    rows: int
    module: str
    mode: str
    reason: str
    last_verified: str
    located: dict[str, int] = field(default_factory=dict)  # strategy -> rows with a record
    any_located: int = 0


STATUS_ORDER = (
    "primary_record",
    "candidate_only",
    "press_release_only",
    "gated_portal",
    "deferred_error",
    "deferred_quota",
    "deferred_coverage",
    "not_found",
)


@dataclass
class DocketCoverage:
    """A state's docket portal: its recorded mode, what it wants, and how its rows came out."""

    state: str
    rows: int
    mode: str
    wants: str
    portal: str
    adapter: bool
    reason: str
    verified: str
    statuses: dict[str, int] = field(default_factory=dict)  # court_status -> rows


@dataclass
class Coverage:
    states: list[StateCoverage]
    total_rows: int
    dockets: list[DocketCoverage] = field(default_factory=list)


def build(conn: sqlite3.Connection, config: Config) -> Coverage:
    rows = Counter[str]()
    state_of: dict[int, str] = {}
    for row_id, raw in conn.execute("SELECT row_id, state FROM rows WHERE in_scope = 1"):
        state = normalize_state(raw) or "unknown"
        rows[state] += 1
        state_of[row_id] = state
    recorded = {
        r[0]: r
        for r in conn.execute(
            "SELECT state, adapter_module, mode, reason, last_verified FROM state_adapters "
            "WHERE kind = 'ag'"
        )
    }
    by_strategy: dict[str, Counter[str]] = defaultdict(Counter)
    found_rows: dict[str, set[int]] = defaultdict(set)
    for row_id, strategy in conn.execute(
        "SELECT DISTINCT row_id, strategy FROM court_records_current"
    ):
        where = state_of.get(row_id)
        if where is not None:
            by_strategy[where][strategy] += 1
            found_rows[where].add(row_id)
    registry = adapters()
    out: list[StateCoverage] = []
    for state in sorted({*rows, *STATES}, key=lambda s: (-rows.get(s, 0), s)):
        cls = registry.get(state)
        row = recorded.get(state)
        module: str
        if row is not None:
            module, mode, reason, verified = row[1] or "", row[2], row[3] or "", row[4] or ""
        elif cls is not None:
            module, mode = cls.__module__, getattr(cls, "mode", "none")
            reason, verified = getattr(cls, "reason", ""), "not verified"
        else:
            module, mode, reason, verified = "", "none", "", ""
        out.append(
            StateCoverage(
                state=state,
                rows=rows.get(state, 0),
                module=module.rsplit(".", 1)[-1] if module and mode != "manual" else "",
                mode=mode,
                reason=reason,
                last_verified=verified,
                located=dict(by_strategy.get(state, {})),
                any_located=len(found_rows.get(state, set())),
            )
        )
    return Coverage(out, sum(rows.values()), _dockets(conn, rows, state_of))


def _dockets(
    conn: sqlite3.Connection, rows: Counter[str], state_of: dict[int, str]
) -> list[DocketCoverage]:
    from efp.court.adapters.docket import adapters as docket_adapters

    statuses: dict[str, Counter[str]] = defaultdict(Counter)
    for row_id, status in conn.execute(
        "SELECT row_id, json_extract(summary_json, '$.court_status') FROM case_stage "
        "WHERE stage = 'court' AND summary_json IS NOT NULL"
    ):
        where = state_of.get(row_id)
        if where is not None and status:
            statuses[where][status] += 1
    have = docket_adapters()
    out = []
    for state, portal, mode, reason, verified in conn.execute(
        "SELECT state, coalesce(portal, ''), mode, coalesce(reason, ''), "
        "coalesce(last_verified, '') FROM state_adapters WHERE kind = 'docket'"
    ):
        wants = reason.split(":", 1)[0].strip() if ":" in reason else ""
        out.append(
            DocketCoverage(
                state=state,
                rows=rows.get(state, 0),
                mode=mode,
                wants=wants,
                portal=portal,
                adapter=state in have,
                reason=reason,
                verified=verified,
                statuses=dict(statuses.get(state, {})),
            )
        )
    return sorted(out, key=lambda d: (-d.rows, d.state))


def render_md(cov: Coverage) -> str:
    header = [
        "State",
        "Rows",
        "AG module",
        "Mode",
        "Verified",
        *[s.removeprefix("court.") for s in STRATEGIES],
        "Any located",
    ]
    lines = [
        "# Coverage matrix",
        "",
        "Generated by `efp status --coverage` from `state_adapters` and the current located "
        "records. Rows are the in-scope rows of the dataset; the located columns count rows with "
        "a current court record from that strategy. A `manual` or `broken` mode always has its "
        "reason below the table; states with no adapter are `none`.",
        "",
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * len(header)) + " |",
    ]
    for s in cov.states:
        cells = [
            s.state,
            f"{s.rows:,}",
            s.module or "—",
            s.mode,
            s.last_verified or "—",
            *[str(s.located.get(strategy, 0)) for strategy in STRATEGIES],
            str(s.any_located),
        ]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("| **Total** | " + f"**{cov.total_rows:,}**" + " |" + " |" * (len(header) - 2))
    lines += _docket_md(cov)
    reasons = [s for s in cov.states if s.reason and s.mode in ("manual", "broken")]
    if reasons:
        lines += ["", "## Why a state is not automated", ""]
        lines += [f"- **{s.state}** ({s.mode}, {s.rows:,} rows): {s.reason}" for s in reasons]
    lines += [""]
    return "\n".join(lines)


def _docket_md(cov: Coverage) -> list[str]:
    """The docket-portal section: mode, what the portal wants, adapter, rows, status by state."""
    if not cov.dockets:
        return []
    header = ["State", "Rows", "Docket mode", "Wants", "Adapter", "Verified", *STATUS_ORDER_SHORT]
    out = [
        "",
        "## Docket portals (court sub-strategy 4)",
        "",
        "Mode is `automated` (search and retrieve without a human), `assisted` (the pipeline "
        "builds the query, the operator searches in a browser) or `manual` (no online search, or "
        'an entry point that is not there). "Wants" is what stands in the way. The status '
        "columns count rows by `court_status` after the court stage has run (`gated_portal` rows "
        "are the ones waiting on the operator).",
        "",
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * len(header)) + " |",
    ]
    for d in cov.dockets:
        cells = [
            d.state,
            f"{d.rows:,}",
            d.mode,
            d.wants or "—",
            "yes" if d.adapter else "no",
            d.verified or "—",
            *[str(d.statuses.get(s, 0)) for s in STATUS_ORDER],
        ]
        out.append("| " + " | ".join(cells) + " |")
    out += ["", "### Docket portal reasons", ""]
    out += [f"- **{d.state}** ({d.mode}): {d.reason}" for d in cov.dockets if d.reason]
    return out


STATUS_ORDER_SHORT = [s.replace("_", " ") for s in STATUS_ORDER]

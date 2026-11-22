"""Link survival and the source-domain table (``efp status --links``).

Written to ``reports/first-pass/domains.md`` by ``efp links report``.

Two views of the ``links`` table over the study population:

* **Link survival**, the first of the plan's nine numbers: of the rows with a source URL, how many
  are ``live``, ``archived`` only, ``dead`` or ``unresolved``. A row's status is the best status
  among its links (live, then archived, then dead, then unresolved). Reported overall and by year
  band, with link-level counts beside it. Rows whose links have not been processed yet are
  ``pending``, so a partial run says so instead of looking like rot.
* **The domain table**: source domains (of the *expanded* URL, ``www.`` removed) ranked by row
  count, with each domain's live / archived / dead / unresolved links, and the shortener share.
  It sets the adapter order for the court and press-release builds.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from efp.config import Config
from efp.resolve_links.parse import SHORTENERS
from efp.scope import population_sql

STATUSES = ("live", "archived", "dead", "unresolved")
ROW_STATUSES = (*STATUSES, "pending")
BANDS: tuple[tuple[str, int, int], ...] = (
    ("2000-09", 2000, 2009),
    ("2010-16", 2010, 2016),
    ("2017-25", 2017, 2025),
)
_RANK = {s: i for i, s in enumerate(STATUSES)}


def band_of(year: int) -> str:
    for label, low, high in BANDS:
        if low <= year <= high:
            return label
    return "other"


def _host(url: str | None) -> str:
    if not url:
        return ""
    parts = urlsplit(url if "//" in url else f"//{url}")
    return (parts.hostname or "").lower().removeprefix("www.")


@dataclass
class DomainRow:
    domain: str
    rows: set[int] = field(default_factory=set)
    hashes: set[str] = field(default_factory=set)
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(ROW_STATUSES, 0))

    @property
    def links(self) -> int:
        return sum(self.counts.values())


@dataclass
class LinksReport:
    population: int
    rows_no_url: int
    rows_not_yet_seen: int  # in the population but with no links row at all (stage not run)
    rows_with_url: int
    row_status: dict[str, int]
    row_status_by_band: dict[str, dict[str, int]]
    link_status: dict[str, int]
    domains: list[DomainRow]
    shortener_links: dict[str, dict[str, int]]  # shortener host -> status counts
    total_links: int

    def pct(self, count: int, of: int) -> str:
        return f"{100 * count / of:.1f}%" if of else "n/a"


def build(conn: sqlite3.Connection, config: Config) -> LinksReport:
    scope = population_sql(config.scope.population, alias="r")
    population = int(conn.execute(f"SELECT count(*) FROM rows r WHERE {scope}").fetchone()[0])
    no_url = int(
        conn.execute(
            f"SELECT count(DISTINCT r.row_id) FROM rows r JOIN links l ON l.row_id = r.row_id "
            f"WHERE {scope} AND l.note = 'no_url'"
        ).fetchone()[0]
    )
    seen = int(
        conn.execute(
            f"SELECT count(DISTINCT r.row_id) FROM rows r JOIN links l ON l.row_id = r.row_id "
            f"WHERE {scope}"
        ).fetchone()[0]
    )
    rows = conn.execute(
        "SELECT l.row_id, l.url_raw, l.url_expanded, l.status, l.resolved_at, r.year, "
        "r.row_sha256, l.note "
        f"FROM links l JOIN rows r ON r.row_id = l.row_id WHERE {scope} "
        "AND coalesce(l.note, '') != 'no_url' ORDER BY l.row_id, l.link_id"
    ).fetchall()
    best: dict[int, str] = {}
    year_of: dict[int, int] = {}
    has_pending: set[int] = set()
    link_status = dict.fromkeys(ROW_STATUSES, 0)
    domains: dict[str, DomainRow] = {}
    shorteners: dict[str, dict[str, int]] = defaultdict(lambda: dict.fromkeys(ROW_STATUSES, 0))
    for row_id, url_raw, expanded, status, resolved_at, year, sha, _note in rows:
        effective = status if resolved_at is not None else "pending"
        if effective == "pending":
            has_pending.add(row_id)
        link_status[effective] += 1
        year_of[row_id] = int(year)
        current = best.get(row_id)
        if effective != "pending" and (
            current in (None, "pending") or _RANK[effective] < _RANK[current]
        ):
            best[row_id] = effective
        elif current is None:
            best[row_id] = "pending"
        raw_host = _host(url_raw)
        if raw_host in SHORTENERS:
            shorteners[raw_host][effective] += 1
        host = _host(expanded)
        if host:
            label = host
        elif raw_host in SHORTENERS:
            label = f"(unexpanded) {raw_host}"  # a shortener nobody could expand
        else:
            label = raw_host or "(none)"  # a plain URL is its own domain, processed or not
        entry = domains.setdefault(label, DomainRow(label))
        entry.rows.add(row_id)
        entry.hashes.add(str(sha))
        entry.counts[effective] += 1
    for row_id in has_pending:  # a row with a link still to do is not yet dead
        if best.get(row_id) in ("dead", "unresolved"):
            best[row_id] = "pending"
    row_status = dict.fromkeys(ROW_STATUSES, 0)
    by_band = {label: dict.fromkeys(ROW_STATUSES, 0) for label, _, _ in BANDS}
    for row_id, status in best.items():
        row_status[status] += 1
        by_band.setdefault(band_of(year_of[row_id]), dict.fromkeys(ROW_STATUSES, 0))[status] += 1
    ranked = sorted(domains.values(), key=lambda d: (-len(d.rows), -d.links, d.domain))
    return LinksReport(
        population=population,
        rows_no_url=no_url,
        rows_not_yet_seen=population - seen,
        rows_with_url=len(best),
        row_status=row_status,
        row_status_by_band=by_band,
        link_status=link_status,
        domains=ranked,
        shortener_links=dict(shorteners),
        total_links=sum(link_status.values()),
    )


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    out = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join("---:" if i else "---" for i in range(len(header))) + "|",
    ]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return out


def render_survival(report: LinksReport) -> str:
    n = report.rows_with_url
    lines = [
        f"Rows in scope: {report.population:,}. With a source URL: {n:,}. "
        f"With no URL in the Source cell: {report.rows_no_url:,}. "
        f"Not yet processed by `resolve_links`: {report.rows_not_yet_seen:,}.",
        "",
        "### Link survival (rows with a source URL; a row counts at its best link)",
        "",
    ]
    header = ["", *ROW_STATUSES, "rows"]
    body = [
        [
            "all years",
            *(
                f"{report.row_status[s]:,} ({report.pct(report.row_status[s], n)})"
                for s in ROW_STATUSES
            ),
            f"{n:,}",
        ]
    ]
    for label, counts in report.row_status_by_band.items():
        total = sum(counts.values())
        body.append(
            [
                label,
                *(f"{counts[s]:,} ({report.pct(counts[s], total)})" for s in ROW_STATUSES),
                f"{total:,}",
            ]
        )
    lines += _table(header, body)
    done = n - report.row_status["pending"]
    not_live = (
        report.row_status["archived"] + report.row_status["dead"] + report.row_status["unresolved"]
    )
    gone = report.row_status["dead"] + report.row_status["unresolved"]
    lines += [
        "",
        f"Link rot, measured on the {done:,} processed rows: no longer live "
        f"{report.pct(not_live, done)} (archived copy exists for "
        f"{report.pct(report.row_status['archived'], done)}); no copy found at all "
        f"{report.pct(gone, done)}. The spec expects 20-40% rot.",
        "",
        f"Link level ({report.total_links:,} links): "
        + ", ".join(f"{s} {report.link_status[s]:,}" for s in ROW_STATUSES),
    ]
    return "\n".join(lines)


def render_domains(report: LinksReport, top: int = 40) -> str:
    lines = ["### Source domains (of the expanded URL), ranked by row count", ""]
    rows = [
        [
            d.domain,
            f"{len(d.rows):,}",
            f"{len(d.hashes):,}",
            f"{d.links:,}",
            *(f"{d.counts[s]:,}" for s in ROW_STATUSES),
        ]
        for d in report.domains[:top]
    ]
    lines += _table(["domain", "rows", "distinct rows", "links", *ROW_STATUSES], rows)
    if len(report.domains) > top:
        lines += ["", f"... and {len(report.domains) - top:,} more domains."]
    lines += [
        "",
        "`distinct rows` counts rows once when they are exact duplicates of each other.",
        "",
    ]
    lines += ["### Shorteners", ""]
    total = report.total_links
    if report.shortener_links:
        short_total = sum(sum(c.values()) for c in report.shortener_links.values())
        lines.append(
            f"{short_total:,} of {total:,} links ({report.pct(short_total, total)}) "
            "are shortener URLs."
        )
        lines.append("")
        lines += _table(
            ["shortener", "links", *ROW_STATUSES],
            [
                [h, f"{sum(c.values()):,}", *(f"{c[s]:,}" for s in ROW_STATUSES)]
                for h, c in sorted(
                    report.shortener_links.items(), key=lambda kv: -sum(kv[1].values())
                )
            ],
        )
    else:
        lines.append("No shortener URLs.")
    return "\n".join(lines)


def render_md(report: LinksReport, *, title: bool = True) -> str:
    head = ["# Source domains and link survival", ""] if title else []
    return "\n".join([*head, render_survival(report), "", render_domains(report), ""])

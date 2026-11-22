"""CourtListener bulk data: listing, download, a local DuckDB index, and the offline search.

Bulk before API is a rule: the candidate universe is matched locally and the REST API (125 a day)
is only for what the index lacks. The quarterly CSVs are tens of gigabytes, so they are streamed to
``paths.bulk`` (never copied into the content store) and only what a name/state/year search needs
is kept in ``paths.bulk/<bulk_index_file>``: federal criminal dockets from 1999, the courts (for
the state), and the parties of those dockets. The CSVs are streamed row by row; only the ids of
the dockets and parties being kept are held in memory (a few hundred thousand integers).

The CSV column layout is not documented in this repository, so the loader is *header-driven*: it
finds each needed column by name (with a few aliases) and, if one is missing, stops and says which
columns the file actually has instead of guessing.
"""

from __future__ import annotations

import bz2
import csv
import gzip
import json
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

import duckdb

from efp.capture import CaptureError
from efp.config import Config
from efp.court.doj import STATES, normalize_state
from efp.court.located import STAGE, CourtTask, Located, cap, store_text
from efp.court.names import NameParts, match_name
from efp.modules import Availability, ModuleResult, Outcome
from efp.types import Context, Row

KINDS = ("dockets", "courts", "parties", "party-types")
FILE_NAME = re.compile(
    r"(?P<kind>dockets|courts|parties|party-types)-(?P<date>\d{4}-\d{2}-\d{2})\.csv(?P<comp>\.bz2|\.gz)?$"
)
_CONTENTS = re.compile(r"<Contents>(.*?)</Contents>", re.S)
FROM_YEAR = 1999
COLUMN_ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    "dockets": {
        "id": ("id", "docket_id"),
        "court_id": ("court_id", "court"),
        "docket_number": ("docket_number",),
        "case_name": ("case_name", "case_name_full", "case_name_short"),
        "date_filed": ("date_filed",),
        "date_terminated": ("date_terminated",),
        "pacer_case_id": ("pacer_case_id",),
        "absolute_url": ("absolute_url",),
    },
    "courts": {
        "id": ("id", "court_id"),
        "full_name": ("full_name", "name"),
        "short_name": ("short_name",),
        "jurisdiction": ("jurisdiction",),
    },
    "parties": {"id": ("id", "party_id"), "name": ("name",)},
    "party-types": {"docket_id": ("docket_id", "docket"), "party_id": ("party_id", "party")},
}
REQUIRED = {
    "dockets": ("id", "court_id", "docket_number", "case_name"),
    "courts": ("id", "full_name"),
    "parties": ("id", "name"),
    "party-types": ("docket_id", "party_id"),
}
_FEDERAL_JURISDICTIONS = {
    "F",
    "FD",
    "FS",
    "FB",
}  # appellate, district, special; bankruptcy is kept out
_BANKRUPTCY = {"FB"}
_CRIMINAL = re.compile(r"-cr-|\bcr\b[-\s]\d|:\d{2}-cr|^\d{2}-cr-", re.I)
_US_V = re.compile(r"^\s*(?:united states(?: of america)?|usa|u\.s\.a?\.?)\s+v[s]?\.?\s", re.I)


class BulkLayoutError(Exception):
    """A bulk CSV does not have a column the loader needs."""


@dataclass(frozen=True)
class BulkFile:
    kind: str
    date: str
    key: str
    size: int
    last_modified: str
    compression: str

    def url(self, config: Config) -> str:
        return config.court.bulk_download_base.rstrip("/") + "/" + self.key.lstrip("/")


def _tag(block: str, name: str) -> str:
    found = re.search(rf"<{name}>(.*?)</{name}>", block, re.S)
    return found.group(1) if found else ""


def parse_listing(xml: str) -> tuple[list[BulkFile], str | None]:
    """The bulk files in one S3 listing page, and the continuation token if there is another."""
    files: list[BulkFile] = []
    for block in _CONTENTS.findall(xml):
        key = _tag(block, "Key")
        match = FILE_NAME.search(key)
        if not match:
            continue
        size = int(_tag(block, "Size") or 0)
        compression = (match["comp"] or "").lstrip(".")
        modified = _tag(block, "LastModified")
        files.append(BulkFile(match["kind"], match["date"], key, size, modified, compression))
    token = re.search(r"<NextContinuationToken>(.*?)</NextContinuationToken>", xml)
    truncated = re.search(r"<IsTruncated>true</IsTruncated>", xml)
    return files, (token.group(1) if token and truncated else None)


def list_bulk_files(ctx: Context) -> list[BulkFile]:
    """Every bulk file the listing names (through the capture layer, so the call is recorded)."""
    config = ctx.config
    found: list[BulkFile] = []
    token: str | None = None
    for _ in range(50):
        params = {"continuation-token": token} if token else None
        record = cap(ctx).fetch(
            config.court.bulk_listing_url, row_id=None, stage="bulk", kind="api", params=params
        )
        if not record.ok:
            raise CaptureError(f"bulk listing answered HTTP {record.http_status}")
        page, token = parse_listing(cap(ctx).read_body(record).decode("utf-8", "replace"))
        found.extend(page)
        if token is None:
            break
    return found


def latest_set(files: Iterable[BulkFile]) -> dict[str, BulkFile]:
    """The newest file of each kind we load."""
    best: dict[str, BulkFile] = {}
    for item in files:
        if item.kind not in best or item.date > best[item.kind].date:
            best[item.kind] = item
    return best


def total_gb(files: Iterable[BulkFile]) -> float:
    return sum(f.size for f in files) / 1e9


def fetch_files(
    ctx: Context, files: dict[str, BulkFile], *, max_gb: float
) -> list[tuple[BulkFile, int]]:
    """Download the chosen files to ``paths.bulk``. Returns (file, capture id of its manifest)."""
    config = ctx.config
    if total_gb(files.values()) > max_gb:
        raise CaptureError(
            f"{total_gb(files.values()):.1f} GB exceeds court.bulk_max_gb ({max_gb}); "
            "raise it deliberately"
        )
    out: list[tuple[BulkFile, int]] = []
    for kind in KINDS:
        item = files.get(kind)
        if item is None:
            continue
        dest = config.paths.bulk / "courtlistener" / Path(item.key).name
        earlier = _earlier_download(ctx, dest, item)
        if earlier is not None:  # already here, the right size, and recorded: do not fetch again
            out.append((item, earlier))
            continue
        record = cap(ctx).download(
            item.url(config), dest, stage="bulk", expected_size=item.size or None
        )
        out.append((item, record.capture_id))
    return out


def _earlier_download(ctx: Context, dest: Path, item: BulkFile) -> int | None:
    if not dest.is_file() or (item.size and dest.stat().st_size != item.size) or ctx.db is None:
        return None
    found = ctx.db.execute(
        "SELECT capture_id FROM captures WHERE tool = 'bulk_download' AND url_requested LIKE ? "
        "ORDER BY capture_id DESC LIMIT 1",
        (f"%{dest.name}",),
    ).fetchone()
    return None if found is None else int(found[0])


# --- reading the CSVs -----------------------------------------------------------------------


def _open(path: Path) -> IO[str]:
    if path.suffix == ".bz2":
        return bz2.open(path, "rt", encoding="utf-8", newline="")
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", newline="")
    return path.open("r", encoding="utf-8", newline="")


def map_columns(kind: str, header: list[str]) -> dict[str, str]:
    """canonical column -> the column the file actually has; raises with what it does have."""
    lowered = {h.lower(): h for h in header}
    mapping: dict[str, str] = {}
    for canonical, aliases in COLUMN_ALIASES[kind].items():
        for alias in aliases:
            if alias in lowered:
                mapping[canonical] = lowered[alias]
                break
    missing = [c for c in REQUIRED[kind] if c not in mapping]
    if missing:
        raise BulkLayoutError(
            f"the {kind} file has no column for {missing}; its columns are: {sorted(header)}"
        )
    return mapping


def read_rows(path: Path, kind: str) -> Iterator[dict[str, str]]:
    """Rows of a bulk CSV keyed by canonical column name."""
    csv.field_size_limit(1 << 27)
    with _open(path) as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header is None:
            return
        mapping = map_columns(kind, header)
        position = {name: header.index(actual) for name, actual in mapping.items()}
        for row in reader:
            if len(row) < len(header):
                continue
            yield {name: row[i] for name, i in position.items()}


def state_of_court(full_name: str) -> str | None:
    """The state a federal court sits in, from its name ("... District of Connecticut")."""
    for abbreviation, name in sorted(STATES.items(), key=lambda kv: -len(kv[1])):
        if re.search(rf"\b{re.escape(name)}\b", full_name):
            return abbreviation
    return None


def looks_criminal(docket_number: str, case_name: str) -> bool:
    return bool(_CRIMINAL.search(docket_number or "") or _US_V.match(case_name or ""))


def _date(value: str) -> str | None:
    text = (value or "").strip()[:10]
    return text if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) else None


@dataclass
class LoadResult:
    bad_rows: int = 0  # rows skipped because an id was not a number
    dockets: int = 0
    courts: int = 0
    parties: int = 0
    links: int = 0
    dataset_date: str = ""
    rows_seen: dict[str, int] = field(default_factory=dict)


def index_path(config: Config) -> Path:
    return config.paths.bulk / config.court.bulk_index_file


def load_index(
    config: Config,
    paths: dict[str, Path],
    *,
    dataset_date: str,
    manifest_captures: dict[str, int],
    progress: Callable[[str, int], None] | None = None,
) -> LoadResult:
    """Build the DuckDB index from the downloaded CSVs (streamed; nothing is held in memory)."""
    target = index_path(config)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".loading")
    tmp.unlink(missing_ok=True)
    result = LoadResult(dataset_date=dataset_date)
    con = duckdb.connect(str(tmp))
    try:
        con.execute(
            "CREATE TABLE courts (court_id VARCHAR PRIMARY KEY, full_name VARCHAR, "
            "short_name VARCHAR, jurisdiction VARCHAR, state VARCHAR);"
            "CREATE TABLE dockets (docket_id BIGINT, court_id VARCHAR, docket_number VARCHAR, "
            "case_name VARCHAR, date_filed DATE, date_terminated DATE, pacer_case_id VARCHAR, "
            "absolute_url VARCHAR);"
            "CREATE TABLE docket_parties (docket_id BIGINT, party_id BIGINT);"
            "CREATE TABLE parties (party_id BIGINT PRIMARY KEY, name VARCHAR);"
            "CREATE TABLE meta (key VARCHAR PRIMARY KEY, value VARCHAR);"
        )
        federal: set[str] = set()
        have_jurisdiction = False
        batch: list[tuple[Any, ...]] = []
        for row in read_rows(paths["courts"], "courts"):
            result.rows_seen["courts"] = result.rows_seen.get("courts", 0) + 1
            jurisdiction = row.get("jurisdiction", "")
            have_jurisdiction = have_jurisdiction or "jurisdiction" in row
            if have_jurisdiction and (
                jurisdiction not in _FEDERAL_JURISDICTIONS or jurisdiction in _BANKRUPTCY
            ):
                continue
            federal.add(row["id"])
            batch.append(
                (row["id"], row["full_name"], row.get("short_name", ""), jurisdiction,
                 state_of_court(row["full_name"]))
            )  # fmt: skip
        con.executemany("INSERT INTO courts VALUES (?, ?, ?, ?, ?)", batch)
        result.courts = len(batch)

        kept: set[int] = set()
        batch = []
        for row in read_rows(paths["dockets"], "dockets"):
            result.rows_seen["dockets"] = result.rows_seen.get("dockets", 0) + 1
            if progress and result.rows_seen["dockets"] % 1_000_000 == 0:
                progress("dockets", result.rows_seen["dockets"])
            if federal and row["court_id"] not in federal:
                continue
            if not looks_criminal(row["docket_number"], row["case_name"]):
                continue
            filed = _date(row.get("date_filed", ""))
            if filed and int(filed[:4]) < FROM_YEAR:
                continue
            try:
                docket_id = int(row["id"])
            except ValueError:
                result.bad_rows += 1
                continue
            kept.add(docket_id)
            batch.append(
                (docket_id, row["court_id"], row["docket_number"], row["case_name"], filed,
                 _date(row.get("date_terminated", "")), row.get("pacer_case_id", ""),
                 row.get("absolute_url", ""))
            )  # fmt: skip
            if len(batch) >= 50_000:
                con.executemany("INSERT INTO dockets VALUES (?, ?, ?, ?, ?, ?, ?, ?)", batch)
                batch = []
        if batch:
            con.executemany("INSERT INTO dockets VALUES (?, ?, ?, ?, ?, ?, ?, ?)", batch)
        result.dockets = len(kept)

        wanted_parties: set[int] = set()
        links: list[tuple[int, int]] = []
        for row in read_rows(paths["party-types"], "party-types"):
            result.rows_seen["party-types"] = result.rows_seen.get("party-types", 0) + 1
            try:
                docket_id, party_id = int(row["docket_id"]), int(row["party_id"])
            except ValueError:
                continue
            if docket_id in kept:
                links.append((docket_id, party_id))
                wanted_parties.add(party_id)
            if len(links) >= 100_000:
                con.executemany("INSERT INTO docket_parties VALUES (?, ?)", links)
                result.links += len(links)
                links = []
        if links:
            con.executemany("INSERT INTO docket_parties VALUES (?, ?)", links)
            result.links += len(links)

        batch = []
        seen: set[int] = set()
        for row in read_rows(paths["parties"], "parties"):
            result.rows_seen["parties"] = result.rows_seen.get("parties", 0) + 1
            try:
                party_id = int(row["id"])
            except ValueError:
                continue
            if party_id in wanted_parties and party_id not in seen:
                seen.add(party_id)
                batch.append((party_id, row["name"]))
            if len(batch) >= 50_000:
                con.executemany("INSERT INTO parties VALUES (?, ?)", batch)
                batch = []
        if batch:
            con.executemany("INSERT INTO parties VALUES (?, ?)", batch)
        result.parties = len(seen)
        con.execute("CREATE INDEX idx_dockets_court ON dockets(court_id)")
        meta = {
            "dataset_date": dataset_date,
            "loaded_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "manifest_captures": json.dumps(manifest_captures),
            "dockets": str(result.dockets),
            "courts": str(result.courts),
            "parties": str(result.parties),
            "docket_party_links": str(result.links),
            "rows_seen": json.dumps(result.rows_seen),
            "bad_rows": str(result.bad_rows),
        }
        con.executemany("INSERT INTO meta VALUES (?, ?)", list(meta.items()))
    except BaseException:
        con.close()
        tmp.unlink(missing_ok=True)  # a half-built index is never left in place
        tmp.with_name(tmp.name + ".wal").unlink(missing_ok=True)
        raise
    else:
        con.close()
    tmp.with_name(tmp.name + ".wal").unlink(missing_ok=True)
    tmp.replace(target)
    return result


@dataclass(frozen=True)
class IndexInfo:
    dataset_date: str
    loaded_at: str
    dockets: int
    courts: int
    parties: int
    manifest_captures: dict[str, int]


def index_info(config: Config) -> IndexInfo | None:
    path = index_path(config)
    if not path.is_file():
        return None
    try:
        con = duckdb.connect(str(path), read_only=True)
    except duckdb.Error:
        return None
    try:
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    except duckdb.Error:
        return None
    finally:
        con.close()
    return IndexInfo(
        meta.get("dataset_date", ""),
        meta.get("loaded_at", ""),
        int(meta.get("dockets", 0)),
        int(meta.get("courts", 0)),
        int(meta.get("parties", 0)),
        json.loads(meta.get("manifest_captures", "{}")),
    )


# --- searching the index ----------------------------------------------------------------------


@dataclass(frozen=True)
class DocketHit:
    docket_id: int
    court_id: str
    court_name: str
    state: str | None
    docket_number: str
    case_name: str
    date_filed: str | None
    date_terminated: str | None
    absolute_url: str
    score: int
    matched_on: str
    initial_only: bool = False


def search_dockets(
    config: Config, parts: NameParts, *, state: str | None, year: int
) -> list[DocketHit]:
    """Dockets whose party or case name carries the subject's name, in the row's state and years."""
    path = index_path(config)
    if not path.is_file() or not parts.usable:
        return []
    state = normalize_state(state)  # junk is unknown: a filter on "N/A" would match nothing
    cfg = config.court
    surname = parts.surname.lower()
    escaped = surname.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"%{escaped}%"
    last_filed = f"{year + cfg.window_years_after}-12-31"
    first_ended = f"{year - cfg.terminated_slack_years}-01-01"
    con = duckdb.connect(str(path), read_only=True)
    try:
        # pre-filter on the surname first (case name, or a party of the docket), then join only
        # the candidates: the full dockets x parties join is never built per row
        rows = con.execute(
            "WITH cand AS ("
            "SELECT docket_id FROM dockets WHERE lower(case_name) LIKE ? ESCAPE '\\' "
            "UNION SELECT dp.docket_id FROM parties p JOIN docket_parties dp "
            "ON dp.party_id = p.party_id WHERE lower(p.name) LIKE ? ESCAPE '\\') "
            "SELECT d.docket_id, d.court_id, c.full_name, c.state, d.docket_number, d.case_name, "
            "CAST(d.date_filed AS VARCHAR), CAST(d.date_terminated AS VARCHAR), d.absolute_url, "
            "coalesce(string_agg(DISTINCT p.name, ' | '), '') "
            "FROM cand JOIN dockets d ON d.docket_id = cand.docket_id "
            "LEFT JOIN courts c ON c.court_id = d.court_id "
            "LEFT JOIN docket_parties dp ON dp.docket_id = d.docket_id "
            "LEFT JOIN parties p ON p.party_id = dp.party_id "
            "WHERE (d.date_filed IS NULL OR d.date_filed <= CAST(? AS DATE)) "
            "AND (d.date_terminated IS NULL OR d.date_terminated >= CAST(? AS DATE)) "
            "AND (? IS NULL OR c.state IS NULL OR c.state = ?) "
            "GROUP BY ALL",
            [like, like, last_filed, first_ended, state, state],
        ).fetchall()
    finally:
        con.close()
    hits: list[DocketHit] = []
    for did, court_id, court_name, court_state, number, case_name, filed, ended, url, names in rows:
        score, matched = 0, ""
        party_match = match_name(parts, names) if names else None
        case_match = match_name(parts, case_name or "")
        initial_only = False
        if party_match and party_match.full:
            score, matched = 3, f"party name: {party_match.span}"
            initial_only = party_match.initial_only
        elif case_match and case_match.full:
            score, matched = 2, f"case name: {case_match.span}"
            initial_only = case_match.initial_only
        if score and _US_V.match(case_name or ""):
            score += 1
        if score:
            hits.append(
                DocketHit(
                    int(did),
                    court_id,
                    court_name or court_id,
                    court_state,
                    number,
                    case_name,
                    filed,
                    ended,
                    url or "",
                    score,
                    matched,
                    initial_only,
                )
            )
    hits.sort(key=lambda h: (-h.score, h.date_filed or "", h.docket_number))
    return hits[: cfg.top_candidates]


class CourtBulk:
    name = "court.courtlistener_bulk"
    stage = STAGE

    def available(self, ctx: Context) -> Availability:
        if index_info(ctx.config) is None:
            return Availability.unavailable(
                "the CourtListener bulk index is not loaded (efp court bulk fetch, then load)"
            )
        return Availability.available()

    def applies(self, row: Row, ctx: Context) -> bool | str:
        task: CourtTask = row  # type: ignore[assignment]
        if not task.cs.classification.federal_strategies_apply:
            return "classified as a state case"
        return True if task.cs.parts.usable else "no usable name"

    def run(self, row: Row, ctx: Context) -> ModuleResult:
        task: CourtTask = row  # type: ignore[assignment]
        cs = task.cs
        info = index_info(ctx.config)
        hits = search_dockets(ctx.config, cs.parts, state=row.state, year=row.year or 0)
        cs.flags["bulk"] = {
            "dataset_date": info.dataset_date if info else None,
            "candidates": len(hits),
        }
        if not hits or info is None:
            return ModuleResult(Outcome.miss)
        manifest = info.manifest_captures.get("dockets")
        for hit in hits:
            evidence = (
                f"{hit.case_name}\ndocket number: {hit.docket_number}\ncourt: {hit.court_name}\n"
                f"filed: {hit.date_filed or 'unknown'}"
            )
            text_id = store_text(
                ctx, task.row_id, parent_capture_id=manifest or 0,
                url=f"courtlistener-bulk:docket:{hit.docket_id}", text=evidence,
                tool="courtlistener_bulk_row",
                extra={"dataset_date": info.dataset_date, "docket_id": hit.docket_id},
            )  # fmt: skip
            cs.located.append(
                Located(
                    source_kind="federal_docket",
                    jurisdiction="US",
                    court=hit.court_name,
                    case_number=hit.docket_number,
                    document_kind="docket",
                    url=(
                        f"https://www.courtlistener.com{hit.absolute_url}"
                        if hit.absolute_url
                        else None
                    ),
                    strategy=self.name,
                    capture_id=text_id,
                    text=evidence,
                    text_capture_id=text_id,
                    basis={
                        "matched_on": hit.matched_on,
                        "score": hit.score,
                        "state": hit.state,
                        "candidates": len(hits),
                    },
                    # several dockets for one name, or a weaker kind of match, is not settled
                    weak=(
                        normalize_state(row.state) is None
                        or hit.state is None
                        or len(hits) > 1
                        or hit.score < 3
                        or hit.initial_only
                    ),
                    docket_id=hit.docket_id,
                )
            )
        # A weak-only result (several candidates, initial-only, state not corroborated) is kept as
        # candidates but is not a hit: the chain goes on to the modules that can confirm it.
        confirmed = any(not item.weak for item in cs.located if item.strategy == self.name)
        return ModuleResult(
            Outcome.hit if confirmed else Outcome.miss, wrote={"candidates": len(hits)}
        )

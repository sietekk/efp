"""Load the workbook into ``rows`` once, immutably.

Order of work: check the file against ``MANIFEST.txt`` (stop on a mismatch), read and validate every
row without writing, then write ``rows``, ``case_stage`` and ``source_workbook`` in a single
transaction. A second run against a populated database is a no-op only if the workbook hash, the
year window and every ``row_sha256`` match; otherwise it is refused. ``rows`` is written here and
nowhere else.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from openpyxl.utils.exceptions import InvalidFileException

from efp import db
from efp.config import Config
from efp.ingest import sample as sampling
from efp.ingest.reader import WorkbookData, WorkbookError, read_workbook
from efp.scope import in_window

# The stages the schema's CHECK constraint allows (spec section 3).
ALLOWED_STAGES = (
    "resolve_links",
    "court",
    "identity",
    "news",
    "documents",
    "social",
    "extract",
    "code",
)

INSERT_ROW = (
    "INSERT INTO rows (row_id, year, state, name, case_type, fraud_type, details, source_raw, "
    "ideological_motivation, ideological_source, in_scope, row_sha256, ingested_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
INSERT_STAGE = "INSERT INTO case_stage (row_id, stage, status) VALUES (?, ?, 'pending')"
INSERT_WORKBOOK = (
    "INSERT INTO source_workbook (workbook_id, sha256, filename, sheet_name, header_row, "
    "data_rows, year_from, year_to, ingested_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)"
)

_MANIFEST_LINE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(.+?)\s*$")


class IngestError(Exception):
    """Ingest cannot proceed (bad manifest, unreadable workbook, invalid configuration)."""


class IngestRefused(IngestError):
    """The database already holds rows that do not match this workbook."""


_URL = re.compile(r"https?://", re.I)


@dataclass(frozen=True)
class Summary:
    total_rows: int
    in_window: int
    source_nonempty: int  # Source cell not blank, all rows
    source_with_url: int  # Source cell contains an http(s) URL, all rows
    window_source_nonempty: int  # the same two, restricted to in-window rows
    window_source_with_url: int
    duplicate_rows: int  # rows identical to another row in all nine columns
    by_band_and_case_type: dict[tuple[str, str], int]


@dataclass(frozen=True)
class Prepared:
    """A validated workbook, ready to load. Nothing has been written."""

    path: Path
    sha256: str
    data: WorkbookData
    summary: Summary
    sample: sampling.Sample


@dataclass
class Result:
    status: str  # "ingested" | "noop" | "checked"
    prepared: Prepared
    sample_file: str = ""  # "written" | "verified" | "would write" | "would verify"
    notes: list[str] = field(default_factory=list)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_manifest(text: str) -> dict[str, str]:
    """``shasum -a 256`` output: ``<64 hex>  <filename>`` (a ``*`` marks binary mode)."""
    entries: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        match = _MANIFEST_LINE.match(line)
        if match is None:
            raise IngestError(f"MANIFEST.txt line {number} is not `shasum -a 256` output")
        name = Path(match.group(2)).name
        digest = match.group(1).lower()
        if entries.get(name, digest) != digest:
            raise IngestError(f"MANIFEST.txt lists {name} twice with different hashes")
        entries[name] = digest
    return entries


def check_manifest(workbook: Path) -> str:
    """Hash the workbook and compare with MANIFEST.txt beside it. Returns the SHA-256."""
    if not workbook.is_file():
        raise IngestError(f"workbook not found: {workbook}")
    manifest = workbook.parent / "MANIFEST.txt"
    actual = sha256_file(workbook)
    if not manifest.is_file():
        raise IngestError(
            f"MANIFEST.txt not found beside the workbook ({manifest}); the workbook hashes to "
            f"{actual}. Create it with: (cd {workbook.parent} && shasum -a 256 {workbook.name} "
            "> MANIFEST.txt)"
        )
    entries = parse_manifest(manifest.read_text(encoding="utf-8-sig"))  # tolerate a BOM
    expected = entries.get(workbook.name)
    if expected is None:
        raise IngestError(
            f"MANIFEST.txt has no entry for {workbook.name}; the workbook hashes to {actual}"
        )
    if expected != actual:
        raise IngestError(
            f"workbook does not match MANIFEST.txt:\n  manifest: {expected}\n  workbook: {actual}"
        )
    return actual


def display_band(year: int) -> str:
    if year < 2000:
        return "before 2000"
    if year > 2025:
        return "after 2025"
    return sampling.year_band(year)


def summarise(data: WorkbookData, config: Config) -> Summary:
    counts: Counter[tuple[str, str]] = Counter()
    in_scope = nonempty = with_url = window_nonempty = window_url = 0
    for row in data.rows:
        counts[(display_band(row.year), (row.values[3] or "").strip() or "(blank)")] += 1
        source = (row.values[6] or "").strip()
        has_url = bool(_URL.search(source))
        inside = in_window(row.year, config)
        in_scope += inside
        nonempty += bool(source)
        with_url += has_url
        window_nonempty += inside and bool(source)
        window_url += inside and has_url
    duplicates = len(data.rows) - len({r.sha256 for r in data.rows})
    return Summary(
        len(data.rows),
        in_scope,
        nonempty,
        with_url,
        window_nonempty,
        window_url,
        duplicates,
        dict(counts),
    )


def _candidates(data: WorkbookData, config: Config) -> list[sampling.Candidate]:
    return [
        sampling.Candidate(r.row_id, r.year, r.values[4], r.values[6], r.values[5], r.sha256)
        for r in data.rows
        if in_window(r.year, config)
    ]


def prepare(config: Config, workbook: Path) -> Prepared:
    """Manifest check, then read and validate every row. Writes nothing anywhere."""
    sha = check_manifest(workbook)
    unknown = [s for s in config.runner.stages if s not in ALLOWED_STAGES]
    if unknown:
        raise IngestError(
            f"runner.stages names stage(s) the schema does not allow: {', '.join(unknown)}"
        )
    try:
        data = read_workbook(workbook)
    except WorkbookError as exc:
        raise IngestError(str(exc)) from exc
    except (zipfile.BadZipFile, InvalidFileException, KeyError, OSError) as exc:
        raise IngestError(f"{workbook.name} cannot be read as an .xlsx file: {exc}") from exc
    drawn = sampling.draw(
        _candidates(data, config), seed=config.scope.sample_seed, size=config.scope.sample_size
    )
    return Prepared(workbook, sha, data, summarise(data, config), drawn)


def _sample_action(prepared: Prepared, config: Config, *, write: bool) -> str:
    path = sampling.resolve_sample_path(config)
    state = sampling.compare_with_file(prepared.sample, path)
    if state == "differs":
        raise IngestRefused(
            f"{path} exists and differs from the sample this workbook and seed produce. The sample "
            "is frozen and never silently redrawn; delete the file only if you mean to redraw it."
        )
    if state == "same":
        return "verified" if write else "would verify"
    if write:
        try:
            sampling.write_sample(prepared.sample, path)
        except (sampling.SampleError, OSError) as exc:
            raise IngestError(
                f"the sample file could not be written ({exc}). The rows ARE loaded; fix the "
                "problem and run `efp ingest` again, which only writes the sample."
            ) from exc
        return "written"
    return "would write"


def check_only(config: Config, workbook: Path) -> Result:
    """Everything ingest checks, with no writes at all (no database, no sample file)."""
    prepared = prepare(config, workbook)
    action = _sample_action(prepared, config, write=False)
    return Result("checked", prepared, action)


def _existing_differences(
    conn: sqlite3.Connection, prepared: Prepared, config: Config
) -> list[str]:
    record = conn.execute(
        "SELECT sha256, sheet_name, header_row, data_rows, year_from, year_to FROM source_workbook"
    ).fetchone()
    if record is None:
        return ["rows is populated but there is no source_workbook record"]
    problems: list[str] = []
    expected = (
        prepared.sha256,
        prepared.data.sheet_name,
        prepared.data.header_row,
        len(prepared.data.rows),
        config.scope.year_from,
        config.scope.year_to,
    )
    labels = ("workbook hash", "sheet name", "header row", "row count", "year_from", "year_to")
    problems.extend(
        f"{label}: database has {have!r}, this run has {want!r}"
        for label, have, want in zip(labels, record, expected, strict=True)
        if have != want
    )
    stored = dict(conn.execute("SELECT row_id, row_sha256 FROM rows"))
    fresh = {r.row_id: r.sha256 for r in prepared.data.rows}
    changed = sorted(i for i in fresh if stored.get(i) != fresh[i])
    extra = sorted(set(stored) - set(fresh))
    if changed:
        problems.append(f"{len(changed)} row hash(es) differ, first row_id(s): {changed[:5]}")
    if extra:
        problems.append(f"{len(extra)} row(s) in the database are not in the workbook")
    return problems


def ingest(config: Config, prepared: Prepared) -> Result:
    """Write the validated workbook, or confirm it is already loaded. Idempotent and race-safe.

    Whether ``rows`` is empty is decided inside the write transaction, so two ingests started at
    once cannot both try to insert: the second waits, then takes the no-op path.
    """
    conn = db.connect(config)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute("SELECT count(*) FROM rows").fetchone()[0]
            if existing:
                conn.execute("ROLLBACK")  # nothing to write; release the lock
                return _confirm_existing(conn, prepared, config)
            # A fresh load: a conflicting sample file refuses before anything is written.
            _sample_action(prepared, config, write=False)
            _insert_all(conn, prepared, config)
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        return Result("ingested", prepared, _sample_action(prepared, config, write=True))
    finally:
        conn.close()


def _confirm_existing(conn: sqlite3.Connection, prepared: Prepared, config: Config) -> Result:
    problems = _existing_differences(conn, prepared, config)
    if problems:
        raise IngestRefused(
            "the database already holds rows and this workbook does not match them:\n  - "
            + "\n  - ".join(problems)
            + "\nrows is immutable; to start over, delete the database and ingest again."
        )
    notes = _stage_notes(conn, prepared, config)
    return Result("noop", prepared, _sample_action(prepared, config, write=True), notes)


def _stage_notes(conn: sqlite3.Connection, prepared: Prepared, config: Config) -> list[str]:
    expected = len(prepared.data.rows) * len(config.runner.stages)
    have = conn.execute("SELECT count(*) FROM case_stage").fetchone()[0]
    if have != expected:
        return [
            f"case_stage has {have} record(s) but runner.stages implies {expected}; "
            "ingest does not change an existing database"
        ]
    return []


def _insert_all(conn: sqlite3.Connection, prepared: Prepared, config: Config) -> None:
    now = datetime.now(UTC).isoformat()
    rows = [
        (
            r.row_id,
            r.year,
            *r.values[1:],
            1 if in_window(r.year, config) else 0,
            r.sha256,
            now,
        )
        for r in prepared.data.rows
    ]
    stages = [(r.row_id, s) for r in prepared.data.rows for s in config.runner.stages]
    conn.executemany(INSERT_ROW, rows)
    conn.executemany(INSERT_STAGE, stages)
    conn.execute(
        INSERT_WORKBOOK,
        (
            prepared.sha256,
            prepared.path.name,
            prepared.data.sheet_name,
            prepared.data.header_row,
            len(prepared.data.rows),
            config.scope.year_from,
            config.scope.year_to,
            now,
        ),
    )

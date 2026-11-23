"""The reference loader framework: one :class:`Dataset` per source, each a ``Module`` of the
``[stages.reference]`` chain with ``download``, ``load`` and ``verify``.

A dataset whose source is offline, gated or not configured is skipped with the reason and the
others load. Every load writes a ``ref_sources`` row (URL, time, file SHA-256, size, license,
attribution, rows, notes) and the rows of the data table carry its ``load_id``. A byte-identical
file already loaded is a no-op; a different file is a new load and the old one becomes
``current = 0`` (its rows stay, the ``*_current`` views hide them). Nothing is ever deleted, and
the raw files stay under ``paths.bulk`` and are never committed.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from efp.modules import Availability, ModuleResult, Outcome
from efp.reference import files
from efp.reference.files import Fetched, Part
from efp.selftest import Check
from efp.types import Context, Row

log = logging.getLogger(__name__)
BATCH = 20_000


class LayoutError(Exception):
    """A file is not in the layout the loader knows (a column is missing, a member absent). The
    message says what was seen; the load is rolled back and recorded as gated."""


@dataclass
class Known:
    """A value of the real data recomputed by ``efp selftest --reference``."""

    description: str
    sql: str
    params: tuple[Any, ...]
    expected: float | str
    tolerance: float = 0.0


@dataclass
class LoadResult:
    dataset: str
    part: str
    status: str  # loaded | unchanged | gated | skipped | failed
    reason: str = ""
    rows: int = 0
    load_id: int | None = None


@dataclass
class Counter:
    """Rows written and the notes a loader wants kept (the floor, the columns, the cycles)."""

    rows: int = 0
    notes: dict[str, Any] = field(default_factory=dict)


def batches(rows: Iterable[tuple[Any, ...]], size: int = BATCH) -> Iterator[list[tuple[Any, ...]]]:
    batch: list[tuple[Any, ...]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


class Dataset:
    """Subclasses name the dataset, its files and how to read them."""

    name: ClassVar[str]  # module name is "reference.<name>"
    table: ClassVar[str]  # the ref_* data table it fills
    license: ClassVar[str]
    attribution: ClassVar[str]
    version: ClassVar[str] = ""
    stage = "reference"
    needs_url: ClassVar[str | None] = None  # a [reference] key that must be set (dime_url, ftm_url)
    known: ClassVar[list[Known]] = []

    # ---- what a subclass supplies ------------------------------------------------------------

    def parts(self, ctx: Context) -> list[Part]:
        raise NotImplementedError

    def read(self, ctx: Context, part: Part, path: Path, load_id: int) -> Counter:
        """Insert the file's rows (carrying ``load_id``) and return the count and notes."""
        raise NotImplementedError

    # ---- the Module interface (the loaders are driven by `efp reference`, not per row) ----

    @property
    def module_name(self) -> str:
        return f"reference.{self.name}"

    def available(self, ctx: Context) -> Availability:
        if self.needs_url and not getattr(ctx.config.reference, self.needs_url):
            ready = any(
                (files.dataset_dir(ctx.config, self.name) / p.filename).is_file()
                for p in self.parts(ctx)
            )
            if not ready:
                return Availability.unavailable(
                    f"reference.{self.needs_url} is not set and no file was imported "
                    f"(`efp reference import {self.name} FILE`)"
                )
        return Availability.available()

    def applies(self, row: Row, ctx: Context) -> bool | str:
        return "a reference dataset is loaded by `efp reference`, not per row"

    def run(self, row: Row, ctx: Context) -> ModuleResult:
        return ModuleResult(Outcome.miss)

    def import_part(self, ctx: Context, source: Path, key: str | None) -> Part:
        """Which of the dataset's files a hand-downloaded file is: the named part, the one whose
        file name it has, or the only one."""
        parts = self.parts(ctx)
        if key is not None:
            found = next((p for p in parts if p.key == key), None)
            if found is None:
                raise LayoutError(
                    f"{self.name} has no part {key!r} (parts: {[p.key for p in parts]})"
                )
            return found
        by_name = next((p for p in parts if p.filename.lower() == source.name.lower()), None)
        if by_name is not None:
            return by_name
        if len(parts) == 1:
            return parts[0]
        raise LayoutError(
            f"{source.name} is not one of {self.name}'s files; name the part with --part "
            f"({[p.key for p in parts]})"
        )

    # ---- operations -------------------------------------------------------------------------

    def download(self, ctx: Context, *, refresh: bool = False) -> list[Fetched]:
        out: list[Fetched] = []
        for part in self.parts(ctx):
            fetched = files.fetch(ctx, self.name, part, refresh=refresh)
            if fetched.status != "ok":
                self._record_refusal(ctx, part, fetched)
            out.append(fetched)
        return out

    def load(self, ctx: Context, *, fetched: list[Fetched] | None = None) -> list[LoadResult]:
        conn = ctx.db
        if conn is None:
            raise RuntimeError("the reference loaders need the database")
        results: list[LoadResult] = []
        for part in self.parts(ctx):
            path = files.dataset_dir(ctx.config, self.name) / part.filename
            if not path.is_file():
                results.append(LoadResult(self.name, part.key, "skipped", "not downloaded"))
                continue
            problem = files.sniff(path, part.kind)
            if problem:
                results.append(LoadResult(self.name, part.key, "gated", problem))
                continue
            results.append(self._load_file(ctx, conn, part, path))
        return results

    def _load_file(
        self, ctx: Context, conn: sqlite3.Connection, part: Part, path: Path
    ) -> LoadResult:
        sha = files.sha256_file(path)
        same = conn.execute(
            "SELECT load_id, row_count, current FROM ref_sources WHERE dataset = ? AND part = ? "
            "AND file_sha256 = ? AND status = 'loaded'",
            (self.name, part.key, sha),
        ).fetchone()
        if same is not None:
            if not same[2]:  # an older file loaded again: it becomes the current one
                self._make_current(conn, self.name, part.key, int(same[0]))
                conn.commit()
            return LoadResult(self.name, part.key, "unchanged", "same file already loaded",
                              int(same[1] or 0), int(same[0]))  # fmt: skip
        capture_id = files.earlier_capture(conn, path, sha)
        url = part.url
        if not url and capture_id is not None:  # a file found through a listing: where it came from
            got = conn.execute(
                "SELECT url_requested FROM captures WHERE capture_id = ?", (capture_id,)
            ).fetchone()
            url = str(got[0]).split("?")[0] if got else ""  # never a signed query
        cursor = conn.execute(
            "INSERT INTO ref_sources (dataset, part, url, version, retrieved_at, file_sha256, "
            "file_size, capture_id, license, attribution, status, current, loaded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?, 'failed', 0, ?)",
            (self.name, part.key, url or None, self.version, files.now(), sha,
             path.stat().st_size, capture_id, self.license, self.attribution, files.now()),
        )  # fmt: skip
        load_id = int(cursor.lastrowid or 0)
        try:
            counted = self.read(ctx, part, path, load_id)
        except LayoutError as exc:
            conn.rollback()
            return self._record_failed(conn, part, path, sha, capture_id, "gated", str(exc))
        except Exception as exc:
            conn.rollback()
            log.exception("loading %s %s failed", self.name, part.key)
            return self._record_failed(
                conn, part, path, sha, capture_id, "failed", f"{type(exc).__name__}: {exc}"[:300]
            )
        conn.execute(
            "UPDATE ref_sources SET status = 'loaded', row_count = ?, notes_json = ? "
            "WHERE load_id = ?",
            (counted.rows, json.dumps(counted.notes, sort_keys=True), load_id),
        )
        self._make_current(conn, self.name, part.key, load_id)
        conn.commit()
        return LoadResult(self.name, part.key, "loaded", rows=counted.rows, load_id=load_id)

    @staticmethod
    def _make_current(conn: sqlite3.Connection, dataset: str, part: str, load_id: int) -> None:
        conn.execute(
            "UPDATE ref_sources SET current = (load_id = ?) WHERE dataset = ? AND part = ? "
            "AND status = 'loaded'",
            (load_id, dataset, part),
        )

    def _record_failed(
        self, conn: sqlite3.Connection, part: Part, path: Path, sha: str,
        capture_id: int | None, status: str, reason: str,
    ) -> LoadResult:  # fmt: skip
        conn.execute(
            "INSERT INTO ref_sources (dataset, part, url, version, retrieved_at, file_sha256, "
            "file_size, capture_id, license, attribution, notes_json, status, current, loaded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?)",
            (self.name, part.key, part.url or None, self.version, files.now(), sha,
             path.stat().st_size, capture_id, self.license, self.attribution,
             json.dumps({"reason": reason}), status, files.now()),
        )  # fmt: skip
        conn.commit()
        return LoadResult(self.name, part.key, status, reason)

    def _record_refusal(self, ctx: Context, part: Part, fetched: Fetched) -> None:
        """A download that could not be had is on the record, with why (and is not loaded)."""
        conn = ctx.db
        if conn is None:
            return
        conn.execute(
            "INSERT INTO ref_sources (dataset, part, url, version, retrieved_at, license, "
            "attribution, notes_json, status, current, loaded_at) VALUES (?,?,?,?,?,?,?,?,?,0,?)",
            (self.name, part.key, part.url or None, self.version, files.now(), self.license,
             self.attribution, json.dumps({"reason": fetched.reason}), fetched.status, files.now()),
        )  # fmt: skip
        conn.commit()

    def verify(self, ctx: Context) -> list[Check]:
        """Each current load: the rows in the table match ``ref_sources.row_count`` and the file on
        disk still hashes to what was recorded."""
        conn = ctx.db
        if conn is None:
            raise RuntimeError("the reference loaders need the database")
        out: list[Check] = []
        rows = conn.execute(
            "SELECT load_id, part, file_sha256, row_count, loaded_at FROM ref_sources "
            "WHERE dataset = ? AND status = 'loaded' AND current = 1 ORDER BY part",
            (self.name,),
        ).fetchall()
        if not rows:
            return [Check(f"reference.{self.name}", False, "not loaded", required=False)]
        for load_id, part, sha, want, loaded_at in rows:
            label = f"reference.{self.name}" + (f" [{part}]" if part else "")
            have = conn.execute(
                f"SELECT count(*) FROM {self.table} WHERE load_id = ?",  # noqa: S608 (class constant)
                (load_id,),
            ).fetchone()[0]
            if have != want:
                out.append(Check(label, False, f"{have:,} rows in {self.table}, {want:,} recorded"))
                continue
            spec = next((p for p in self.parts(ctx) if p.key == part), None)
            path = files.dataset_dir(ctx.config, self.name) / (spec.filename if spec else "")
            if spec is not None and path.is_file() and files.sha256_file(path) != sha:
                out.append(
                    Check(label, False, "the file on disk no longer matches its recorded hash")
                )
                continue
            out.append(
                Check(
                    label,
                    True,
                    f"{have:,} rows, sha256 {str(sha)[:12]}, loaded {str(loaded_at)[:10]}",
                )
            )
        return out

    def check_known(self, ctx: Context) -> list[Check]:
        conn = ctx.db
        if conn is None:
            raise RuntimeError("the reference loaders need the database")
        out: list[Check] = []
        for known in self.known:
            row = conn.execute(known.sql, known.params).fetchone()
            got = None if row is None else row[0]
            if isinstance(known.expected, str):
                ok = got is not None and str(got) == known.expected
            else:
                ok = got is not None and abs(float(got) - known.expected) <= known.tolerance
            out.append(
                Check(f"known: {known.description}", ok, f"{got} (expected {known.expected})")
            )
        return out

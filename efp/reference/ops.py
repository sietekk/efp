"""What ``efp reference`` does: select datasets, download, load, verify, import, index, report.

Every dataset is independent: one that is offline, gated or not configured is skipped with its
reason and the others carry on. ``download`` and ``load`` return what happened per file; nothing
here raises for a single dataset's trouble.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from efp.reference import datasets, index
from efp.reference.base import Dataset, LayoutError, LoadResult
from efp.reference.files import Fetched, import_file
from efp.selftest import Check
from efp.types import Context


class UnknownDataset(Exception):
    pass


def select(names: list[str] | None) -> list[Dataset]:
    every = datasets()
    if not names:
        return list(every.values())
    missing = [n for n in names if n not in every]
    if missing:
        raise UnknownDataset(f"unknown dataset {missing}; known: {sorted(every)}")
    return [every[n] for n in names]


def download(
    ctx: Context, names: list[str] | None = None, *, refresh: bool = False
) -> dict[str, list[Fetched]]:
    out: dict[str, list[Fetched]] = {}
    for dataset in select(names):
        avail = dataset.available(ctx)
        if not avail.ok:
            out[dataset.name] = [
                Fetched(p, None, "skipped", avail.reason) for p in dataset.parts(ctx)
            ]
            continue
        out[dataset.name] = dataset.download(ctx, refresh=refresh)
    return out


def load(ctx: Context, names: list[str] | None = None) -> dict[str, list[LoadResult]]:
    out: dict[str, list[LoadResult]] = {}
    for dataset in select(names):
        out[dataset.name] = dataset.load(ctx)
    return out


def verify(ctx: Context, names: list[str] | None = None) -> list[Check]:
    checks: list[Check] = []
    for dataset in select(names):
        checks.extend(dataset.verify(ctx))
    return checks


def import_by_hand(
    ctx: Context, name: str, source: Path, key: str | None = None
) -> tuple[Fetched, list[LoadResult]]:
    """Copy a hand-downloaded file in, record it, and load it."""
    (dataset,) = select([name])
    try:
        part = dataset.import_part(ctx, source, key)
    except LayoutError as exc:
        return Fetched(dataset.parts(ctx)[0], None, "failed", str(exc)), []
    fetched = import_file(ctx, dataset.name, part, source)
    if fetched.status != "ok":
        return fetched, []
    return fetched, [r for r in dataset.load(ctx) if r.part == part.key]


@dataclass
class StatusRow:
    dataset: str
    available: bool
    reason: str
    status: str
    rows: int
    loaded_at: str
    sha: str


def status(ctx: Context) -> list[StatusRow]:
    conn = ctx.db
    assert conn is not None  # noqa: S101
    rows: list[StatusRow] = []
    for dataset in select(None):
        avail = dataset.available(ctx)
        got = conn.execute(
            "SELECT status, COALESCE(SUM(CASE WHEN status = 'loaded' AND current = 1 "
            "THEN row_count END), 0), MAX(loaded_at), MAX(substr(file_sha256, 1, 12)), "
            "SUM(status = 'loaded' AND current = 1), notes_json "
            "FROM ref_sources WHERE dataset = ?",
            (dataset.name,),
        ).fetchone()
        loaded = int(got[4] or 0)
        if loaded:
            state, why = "loaded", ""
        else:
            last = conn.execute(
                "SELECT status, notes_json FROM ref_sources WHERE dataset = ? "
                "ORDER BY load_id DESC LIMIT 1",
                (dataset.name,),
            ).fetchone()
            state = last[0] if last else "not loaded"
            why = last[1] if last else ""
        rows.append(
            StatusRow(
                dataset.name,
                avail.ok,
                avail.reason or why,
                state,
                int(got[1] or 0),
                str(got[2] or ""),
                str(got[3] or ""),
            )
        )
    return rows


def rebuild_index(ctx: Context) -> index.IndexResult:
    conn = ctx.db
    assert conn is not None  # noqa: S101
    return index.rebuild(conn)

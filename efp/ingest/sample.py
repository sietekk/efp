"""The frozen 50-row stratified sample (plan decision 5).

Drawn once at ingest and written to ``scope.sample_file`` (``tests/fixtures/sample_rows.json``):
only ``row_id`` and ``row_sha256`` per row, the seed, and the strata counts. No names. Build 04's
``efp run <stage> --sample`` and the calibration comparison use exactly these rows.

Strata (in-scope rows only): year band (2000-09, 2010-16, 2017-25) x jurisdiction (federal or
state) x fraud type (the five most frequent categories, everything else "other").

Selection is deterministic and independent of the Python version: within a stratum, rows are
ranked by ``SHA-256(seed|row_id|row_sha256)`` and the first *n* are taken. Allocation gives every
non-empty stratum one row and shares the rest in proportion to stratum size.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from efp.config import Config
from efp.version import repo_root

YEAR_BANDS: tuple[tuple[str, int], ...] = (("2000-09", 2009), ("2010-16", 2016), ("2017-25", 9999))
JURISDICTIONS = ("federal", "state")
OTHER = "other"
BLANK = "(blank)"
TOP_N = 5
FORMAT_VERSION = 1

# "Federal" means the source or details name a U.S. Attorney, the U.S. Department of Justice, or a
# federal court. Bare "Department of Justice" or "DOJ" is not enough: several states call their
# attorney general's office a Department of Justice (Wisconsin, California, New Hampshire), and the
# bare word "federal" also appears in "federal election". The "US" forms are case-sensitive so the
# pronoun in "tell us attorney fees" does not match; the rest ignores case.
_US = r"(?:(?-i:U\.?\s?S\.?)|United\s+States)"
_FEDERAL = re.compile(
    rf"""
      \b{_US}\s*Attorney
    | \b{_US}\s+(?:Dept\.?|Department)\s+of\s+Justice
    | \b(?-i:U\.?\s?S\.?)\s+(?-i:DOJ)\b
    | justice\.gov
    | \busao\b
    | \b{_US}\s*District\s+Court
    | \b{_US}\s+Court\s+of\s+Appeals
    | \bUnited\s+States\s+v\.
    | \bfederal\s+(?:district\s+)?court
    """,
    re.I | re.X,
)

Key = tuple[str, str, str]  # (year band, jurisdiction, fraud label)


class SampleError(Exception):
    """The sample cannot be drawn, or an existing sample file disagrees with a fresh draw."""


@dataclass(frozen=True)
class Candidate:
    row_id: int
    year: int
    fraud_type: str | None
    source_raw: str | None
    details: str | None
    row_sha256: str


@dataclass(frozen=True)
class Sample:
    seed: int
    requested_size: int
    top_fraud_types: tuple[str, ...]
    strata: tuple[tuple[Key, int, int], ...]  # (key, population, sampled), sorted by key
    rows: tuple[tuple[int, str], ...]  # (row_id, row_sha256), sorted by row_id

    @property
    def size(self) -> int:
        return len(self.rows)

    @property
    def sample_sha256(self) -> str:
        return hashlib.sha256(_canonical_rows(self.rows).encode("utf-8")).hexdigest()


def year_band(year: int) -> str:
    for label, upper in YEAR_BANDS:
        if year <= upper:
            return label
    return YEAR_BANDS[-1][0]


def jurisdiction(source_raw: str | None, details: str | None) -> str:
    text = f"{source_raw or ''}\n{details or ''}"
    return "federal" if _FEDERAL.search(text) else "state"


def _fraud_key(value: str | None) -> str:
    return " ".join(value.split()).casefold() if value and value.strip() else BLANK


def top_fraud_types(candidates: list[Candidate]) -> tuple[str, ...]:
    """The most frequent fraud-type categories (normalised), ties broken alphabetically."""
    counts = Counter(_fraud_key(c.fraud_type) for c in candidates)
    counts.pop(BLANK, None)
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return tuple(name for name, _ in ranked[:TOP_N])


def allocate(sizes: dict[Key, int], total: int) -> dict[Key, int]:
    """Rows per stratum: one each, the rest proportional to size (largest remainder), capped."""
    keys = sorted(k for k, n in sizes.items() if n > 0)
    if total >= sum(sizes[k] for k in keys):
        return {k: sizes[k] for k in keys}
    if total < len(keys):
        # Not enough rows for one per stratum: cover the largest strata (ties by key).
        chosen = sorted(keys, key=lambda k: (-sizes[k], k))[:total]
        return {k: 1 for k in sorted(chosen)}
    alloc = {k: 1 for k in keys}
    remaining = total - len(keys)
    while remaining > 0:
        open_keys = [k for k in keys if alloc[k] < sizes[k]]
        if not open_keys:
            break
        weight = sum(sizes[k] for k in open_keys)
        quotas = {k: remaining * sizes[k] / weight for k in open_keys}
        gave = 0
        for k in open_keys:
            step = min(int(quotas[k]), sizes[k] - alloc[k])
            alloc[k] += step
            gave += step
        remaining -= gave
        if remaining > 0:
            by_remainder = sorted(open_keys, key=lambda k: (-(quotas[k] - int(quotas[k])), k))
            for k in by_remainder:
                if remaining == 0:
                    break
                if alloc[k] < sizes[k]:
                    alloc[k] += 1
                    remaining -= 1
    return alloc


def _rank(seed: int, candidate: Candidate) -> str:
    material = f"{seed}|{candidate.row_id}|{candidate.row_sha256}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def draw(candidates: list[Candidate], *, seed: int, size: int) -> Sample:
    """Draw the stratified sample from in-scope candidates."""
    top = top_fraud_types(candidates)
    strata: dict[Key, list[Candidate]] = {}
    for c in candidates:
        fraud = _fraud_key(c.fraud_type)
        label = fraud if fraud in top else OTHER
        strata.setdefault(
            (year_band(c.year), jurisdiction(c.source_raw, c.details), label), []
        ).append(c)
    sizes = {k: len(v) for k, v in strata.items()}
    allocation = allocate(sizes, size)
    chosen: list[Candidate] = []
    for key in sorted(allocation):
        ranked = sorted(strata[key], key=lambda c: _rank(seed, c))
        chosen.extend(ranked[: allocation[key]])
    rows = tuple(sorted((c.row_id, c.row_sha256) for c in chosen))
    table = tuple((k, sizes[k], allocation.get(k, 0)) for k in sorted(sizes))
    return Sample(seed, size, top, table, rows)


def _canonical_rows(rows: tuple[tuple[int, str], ...]) -> str:
    return json.dumps(
        [{"row_id": r, "row_sha256": h} for r, h in rows], separators=(",", ":"), sort_keys=True
    )


def to_document(sample: Sample) -> dict[str, object]:
    return {
        "version": FORMAT_VERSION,
        "seed": sample.seed,
        "requested_size": sample.requested_size,
        "size": sample.size,
        "year_bands": [label for label, _ in YEAR_BANDS],
        "jurisdictions": list(JURISDICTIONS),
        "fraud_type_top": list(sample.top_fraud_types),
        "strata": [
            {
                "year_band": k[0],
                "jurisdiction": k[1],
                "fraud_type": k[2],
                "population": population,
                "sampled": sampled,
            }
            for k, population, sampled in sample.strata
        ],
        "sample_sha256": sample.sample_sha256,
        "rows": [{"row_id": r, "row_sha256": h} for r, h in sample.rows],
    }


def to_json(sample: Sample) -> str:
    return json.dumps(to_document(sample), indent=2, sort_keys=False) + "\n"


def resolve_sample_path(config: Config) -> Path:
    path = Path(config.scope.sample_file)
    if path.is_absolute():
        return path
    root = repo_root()
    if root is None:
        raise SampleError("scope.sample_file is relative but the repository root was not found")
    return root / path


def compare_with_file(sample: Sample, path: Path) -> str:
    """'missing', 'same', or 'differs' (compared by ``sample_sha256`` and seed)."""
    if not path.exists():
        return "missing"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        same = (
            document.get("sample_sha256") == sample.sample_sha256
            and document.get("seed") == sample.seed
        )
    except (OSError, ValueError, AttributeError):
        return "differs"
    return "same" if same else "differs"


def verify_file_integrity(path: Path) -> bool:
    """True if the file's rows still hash to its recorded ``sample_sha256`` (nothing edited)."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        rows = tuple((int(r["row_id"]), str(r["row_sha256"])) for r in document["rows"])
        digest = hashlib.sha256(_canonical_rows(tuple(sorted(rows))).encode("utf-8")).hexdigest()
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return bool(document.get("sample_sha256") == digest)


def write_sample(sample: Sample, path: Path) -> None:
    """Write the fixture atomically, never replacing an existing file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".sample-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(to_json(sample))
        try:
            os.link(tmp, path)
        except FileExistsError as exc:
            raise SampleError(
                f"{path} already exists; the sample is frozen and never redrawn"
            ) from exc
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)

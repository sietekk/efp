"""Pinned instruments: hash every file the pipeline's results depend on.

A result is only reproducible if the exact allowlist, codebook, prompts, Splink settings, site data
and gold set behind it can be named. Writers (builds 12, 15, 16 and 18) call :func:`pin` and store
the returned SHA-256 in the row-level record that used the instrument.

An instrument is a name for a file, or for a directory or glob (its hash covers every file in it,
by relative path, in sorted order). Paths are relative to the repository root unless absolute.
Builds register their own with :func:`register`; the defaults below are the ones the spec names.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from efp.version import repo_root


class InstrumentMissing(Exception):
    """An instrument's file is not there (yet): the module that needs it is unavailable."""


@dataclass(frozen=True)
class Instrument:
    name: str
    pattern: str
    sha256: str
    files: tuple[str, ...]


# name -> path or glob, relative to the repository root.
_REGISTRY: dict[str, str] = {
    "allowlist": "allowlist.json",
    "codebook": "codebook/codebook.yaml",
    "prompts": "prompts/*",
    "splink_settings": "reference/splink_settings.json",
    "wmn_data": "reference/wmn-data.json",
    "maigret_data": "reference/maigret_data.json",
    "gold_set": "reference/gold_set.csv",
}


def register(name: str, pattern: str) -> None:
    """Name another instrument. Re-registering a name with a different path is an error."""
    if _REGISTRY.get(name, pattern) != pattern:
        raise ValueError(f"instrument {name!r} is already registered for {_REGISTRY[name]!r}")
    _REGISTRY[name] = pattern


def names() -> list[str]:
    return sorted(_REGISTRY)


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve(pattern: str, base: Path) -> list[Path]:
    path = Path(pattern)
    if path.is_absolute():
        base, pattern = path.parent, path.name
    if any(ch in pattern for ch in "*?["):
        return sorted(p for p in base.glob(pattern) if p.is_file())
    target = base / pattern
    if target.is_dir():
        return sorted(p for p in target.rglob("*") if p.is_file())
    return [target] if target.is_file() else []


def pin(name_or_path: str, *, base: Path | None = None) -> Instrument:
    """The hash of a registered instrument (or of a path or glob given directly)."""
    root = base or repo_root() or Path.cwd()
    pattern = _REGISTRY.get(name_or_path, name_or_path)
    files = _resolve(pattern, root)
    if not files:
        raise InstrumentMissing(f"{name_or_path}: nothing at {pattern!r}")
    if len(files) == 1 and not any(ch in pattern for ch in "*?["):
        return Instrument(name_or_path, pattern, hash_file(files[0]), (files[0].name,))
    digest = hashlib.sha256()
    relative: list[str] = []
    anchor = Path(pattern).parent if Path(pattern).is_absolute() else root
    for file in files:
        rel = file.relative_to(anchor).as_posix()
        relative.append(rel)
        digest.update(rel.encode() + b"\0" + hash_file(file).encode() + b"\n")
    return Instrument(name_or_path, pattern, digest.hexdigest(), tuple(relative))


def pinned(selected: list[str] | None = None, *, base: Path | None = None) -> dict[str, str]:
    """``{name: sha256}`` for every registered instrument that exists; missing ones are omitted."""
    out: dict[str, str] = {}
    for name in selected or names():
        try:
            out[name] = pin(name, base=base).sha256
        except InstrumentMissing:
            continue
    return out

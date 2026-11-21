"""Package version, git SHA and lockfile hash (used by --version, selftest and the audit log)."""

from __future__ import annotations

import hashlib
import subprocess
from importlib import metadata
from pathlib import Path


def package_version() -> str:
    try:
        return metadata.version("efp")
    except metadata.PackageNotFoundError:
        return "0.0.0+unknown"


def repo_root() -> Path | None:
    """The repository root when running from a checkout, else None."""
    candidate = Path(__file__).resolve().parents[1]
    return candidate if (candidate / "pyproject.toml").is_file() else None


def git_sha() -> str:
    root = repo_root()
    if root is None:
        return "unknown"
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() or "unknown"


def lockfile_sha256() -> str | None:
    root = repo_root()
    if root is None or not (root / "uv.lock").is_file():
        return None
    return hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest()

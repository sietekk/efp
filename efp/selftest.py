"""``efp selftest``: what is installed, configured and present. Names only, never secret values."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from dataclasses import dataclass
from importlib import metadata

from efp.config import Config
from efp.secrets import SECRET_NAMES, SecretsError, env_file_mode, load_secrets
from efp.version import git_sha, package_version

# One marker module per optional extra.
EXTRAS: dict[str, str] = {
    "documents": "pdfplumber",
    "ocr-fallback": "paddleocr",
    "social": "naminter",
    "extraction": "instructor",
    "validation": "sklearn",
    "archive-docker": "auto_archiver",
}

KEY_PACKAGES: tuple[str, ...] = (
    "typer",
    "pydantic",
    "sqlite-utils",
    "duckdb",
    "requests",
    "requests-cache",
    "pyrate-limiter",
    "stamina",
    "splink",
    "trafilatura",
)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


def _installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _package_version(dist: str) -> str:
    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return "not installed"


def run_checks(config: Config | None, config_error: str | None) -> list[Check]:
    """Gather every check. Reads only; creates nothing."""
    py = sys.version_info
    checks: list[Check] = [
        Check("python", (py.major, py.minor) == (3, 12), sys.version.split()[0]),
        Check("efp", True, f"{package_version()} (git {git_sha()})"),
    ]
    for dist in KEY_PACKAGES:
        version = _package_version(dist)
        checks.append(Check(f"package {dist}", version != "not installed", version))

    if config is None:
        checks.append(Check("config", False, config_error or "config.toml could not be loaded"))
        checks.append(Check("extras", False, "skipped: needs config", required=False))
        return checks
    checks.append(Check("config", True, "config.toml loaded and valid"))

    paths = config.paths
    checks.append(Check("data directory", paths.data_dir.is_dir(), _where(paths.data_dir)))
    for label, path in (
        ("store", paths.store),
        ("bulk", paths.bulk),
        ("manifests", paths.manifests),
        ("bags", paths.bags),
        ("logs", paths.logs),
        ("exports", paths.exports),
        ("import inbox", paths.import_inbox),
    ):
        checks.append(Check(f"dir {label}", path.is_dir(), _where(path), required=False))
    checks.append(
        Check("database", paths.database.is_file(), _where(paths.database), required=False)
    )

    mode = env_file_mode(paths.env_file)
    if mode is None:
        checks.append(Check("env file", False, f"missing: {paths.env_file}"))
    else:
        checks.append(
            Check("env file", mode & 0o077 == 0, f"{paths.env_file} mode {mode:04o} (need 0600)")
        )
        try:
            secrets = load_secrets(paths.env_file)
        except SecretsError as exc:
            checks.append(Check("secrets", False, str(exc)))
        else:
            present = secrets.present()
            checks.append(
                Check(
                    "secrets present",
                    True,
                    f"{len(present)}/{len(SECRET_NAMES)}: {', '.join(present) or 'none'}",
                    required=False,
                )
            )
            if secrets.unparseable_lines:
                checks.append(
                    Check(
                        "secrets unparseable lines",
                        False,
                        f"{secrets.unparseable_lines} line(s) are not NAME=value; fix the file",
                        required=False,
                    )
                )
            if secrets.unknown_names:
                checks.append(
                    Check(
                        "secrets unknown names",
                        True,
                        ", ".join(secrets.unknown_names),
                        required=False,
                    )
                )

    for extra, module in EXTRAS.items():
        checks.append(
            Check(f"extra {extra}", _installed(module), f"marker module {module}", required=False)
        )
    checks.append(
        Check("irb cleared", config.irb.cleared, "social stage needs [irb].cleared", required=False)
    )
    docker = shutil.which("docker")
    checks.append(Check("docker", docker is not None, docker or "not on PATH", required=False))
    return checks


def _where(path: object) -> str:
    return f"{path}"

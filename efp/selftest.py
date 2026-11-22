"""``efp selftest``: what is installed, configured and present. Names only, never secret values."""

from __future__ import annotations

import importlib.util
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from importlib import metadata
from urllib.parse import urlsplit

from efp.config import Config
from efp.secrets import SECRET_NAMES, SecretsError, env_file_mode, load_secrets
from efp.version import git_sha, package_version, repo_root

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
        ("cache", paths.cache),
        ("warc", paths.warc),
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

    present_secrets: list[str] = []
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
            present_secrets = list(present)
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

    checks.extend(capture_checks(config, present_secrets))
    checks.extend(court_checks(config, present_secrets))
    for extra, module in EXTRAS.items():
        checks.append(
            Check(f"extra {extra}", _installed(module), f"marker module {module}", required=False)
        )
    docker = shutil.which("docker")
    checks.append(Check("docker", docker is not None, docker or "not on PATH", required=False))
    return checks


CAPTURE_LIBS = (
    ("requests_cache", "HTTP cache"),
    ("pyrate_limiter", "rate limiter"),
    ("stamina", "retries"),
    ("warcio", "WARC writer"),
    ("rfc3161_client", "RFC 3161"),
    ("bagit", "BagIt"),
    ("filelock", "cross-process lock"),
)


def _tsa_anchors(config: Config) -> int:
    root = repo_root()
    folder = (root / "reference" / "tsa") if root else None
    return len(list(folder.glob("*.pem"))) if folder is not None and folder.is_dir() else 0


def _spn_queue(config: Config) -> str:
    if not config.paths.database.is_file():
        return "no database yet"
    try:
        conn = sqlite3.connect(f"{config.paths.database.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return "database not readable"
    try:
        counts = dict(conn.execute("SELECT status, count(*) FROM spn_queue GROUP BY status"))
    except sqlite3.Error:
        return "spn_queue not migrated yet (run `efp db migrate`)"
    finally:
        conn.close()
    waiting = counts.get("pending", 0) + counts.get("deferred", 0)
    return (
        f"{waiting} waiting, {counts.get('submitted', 0)} in flight, "
        f"{counts.get('done', 0)} archived"
    )


def capture_checks(config: Config, present_secrets: list[str]) -> list[Check]:
    """The capture layer's health: libraries, budgets in force, and which optional keys exist."""
    missing = [label for module, label in CAPTURE_LIBS if not _installed(module)]
    out = [
        Check(
            "capture layer",
            not missing,
            "http cache, rate limiter, retries, WARC, RFC 3161 and BagIt libraries importable"
            if not missing
            else "missing: " + ", ".join(missing),
        ),
    ]
    limits = config.rate_limits
    named = ", ".join(f"{h} {1 / r:g}s" for h, r in sorted(limits.domains().items()))
    out.append(
        Check(
            "budgets",
            True,
            f"default 1 per {1 / limits.default_rps:g}s; {named}; robots.txt "
            f"{'honored' if limits.honor_robots_txt else 'NOT honored'}",
        )
    )
    spn_keys = all(k in present_secrets for k in ("IA_ACCESS", "IA_SECRET"))
    out.append(
        Check(
            "save page now",
            spn_keys,
            f"keys present; ~{config.capture.spn_per_minute}/min; queue: {_spn_queue(config)}"
            if spn_keys
            else f"IA_ACCESS/IA_SECRET absent: pages queue, nothing is submitted; "
            f"queue: {_spn_queue(config)}",
            required=False,
        )
    )
    perma = "PERMA_API_KEY" in present_secrets
    out.append(
        Check(
            "perma.cc",
            perma,
            "key present (cited URLs only)"
            if perma
            else "PERMA_API_KEY absent: no-op (free tier needs a registrar affiliation: gated)",
            required=False,
        )
    )
    hosts = {urlsplit(u).hostname for u in config.capture.tsa_urls}
    out.append(
        Check(
            "rfc3161 tsas",
            len(hosts) >= 2,
            f"{len(hosts)} independent TSA(s): {', '.join(sorted(h for h in hosts if h))}",
            required=False,
        )
    )
    anchors = _tsa_anchors(config)
    out.append(
        Check(
            "tsa trust anchors",
            anchors > 0,
            f"{anchors} pinned in reference/tsa/"
            if anchors
            else "none pinned in reference/tsa/: tokens verify the imprint, not the signature",
            required=False,
        )
    )
    return out


def court_checks(config: Config, present_secrets: list[str]) -> list[Check]:
    """The court stage's inputs: the bulk index (rows and the quarter it came from), the API
    token, and the sources that are gated or not yet configured."""
    from efp.court.bulk import index_info

    info = index_info(config)
    out = [
        Check(
            "courtlistener bulk index",
            info is not None,
            f"dataset {info.dataset_date} (loaded {info.loaded_at}): {info.dockets:,} dockets, "
            f"{info.courts:,} courts, {info.parties:,} parties"
            if info
            else "not loaded: efp court bulk fetch --yes, then efp court bulk load",
            required=False,
        ),
        Check(
            "courtlistener api",
            "COURTLISTENER_TOKEN" in present_secrets,
            "token present; budget "
            f"{config.rate_limits.daily_caps.get('www.courtlistener.com', '?')}/day"
            if "COURTLISTENER_TOKEN" in present_secrets
            else "COURTLISTENER_TOKEN absent: the bulk index is searched, the API is not",
            required=False,
        ),
        Check(
            "pin reports",
            bool(config.court.pin_report_urls),
            f"{len(config.court.pin_report_urls)} report URL(s) configured"
            if config.court.pin_report_urls
            else "court.pin_report_urls is empty: pre-2013 federal rows have no fallback",
            required=False,
        ),
        Check(
            "recap fetch",
            False,
            "off: PACER charges per page; the first pass never fetches from PACER"
            if not config.court.recap_enabled
            else "enabled by configuration",
            required=False,
        ),
    ]
    return out


def _where(path: object) -> str:
    return f"{path}"

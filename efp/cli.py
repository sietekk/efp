"""Command-line entry point. Every command from spec section 10 is registered here.

Commands other than ``selftest`` are stubs until the build named in their message. Each
invocation records an ``audit_log`` row on exit once build 02 creates the table.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import NoReturn

import click
import typer

from efp import audit
from efp.config import Config, ConfigError, load_config
from efp.logging import register_secret_values, setup_logging
from efp.secrets import SecretsError, load_secrets
from efp.selftest import run_checks
from efp.version import git_sha, package_version

app = typer.Typer(
    name="efp",
    help="Election Fraud Pipeline: research instrument for the Heritage workbook.",
    no_args_is_help=True,
    add_completion=False,
)
identity_app = typer.Typer(help="Identity resolution tools.", no_args_is_help=True)
app.add_typer(identity_app, name="identity")


@dataclass
class AppState:
    config: Config | None
    config_error: str | None


class LicensedSource(StrEnum):
    newsbank = "newsbank"
    newspapers_com = "newspapers_com"
    westlaw = "westlaw"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"efp {package_version()} (git {git_sha()})")
        raise typer.Exit


def _exit_code_from_exception(exc: BaseException | None) -> int:
    if exc is None:
        return 0
    if isinstance(exc, click.exceptions.Exit | typer.Exit):
        return int(exc.exit_code)
    if isinstance(exc, click.exceptions.Abort):
        return 1
    if isinstance(exc, SystemExit):
        return exc.code if isinstance(exc.code, int) else 1
    return 1


@app.callback()
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Print the package version and git SHA.",
    ),
) -> None:
    """Load configuration, set up logging and register the audit-log hook."""
    started = datetime.now(UTC)
    argv = sys.argv[1:]
    config: Config | None = None
    config_error: str | None = None
    try:
        config = load_config()
    except ConfigError as exc:
        config_error = str(exc)
    ctx.obj = AppState(config=config, config_error=config_error)

    secret_values: list[str] = []
    if config is not None:
        try:
            secret_values = load_secrets(config.paths.env_file).secret_values()
        except SecretsError:
            secret_values = []
    setup_logging(config, secret_values)
    register_secret_values(secret_values)

    def _record() -> None:
        if config is not None:
            audit.record(config, argv, started, _exit_code_from_exception(sys.exception()))

    ctx.call_on_close(_record)


def _not_built(build: str) -> NoReturn:
    typer.echo(f"not built yet (build {build})", err=True)
    raise typer.Exit(2)


@app.command()
def ingest(workbook: Path = typer.Argument(..., help="Heritage workbook (.xlsx).")) -> None:
    """Load the workbook into the database."""
    _not_built("03")


@app.command()
def run(
    stage: str = typer.Argument(..., help="Stage name."),
    limit: int | None = typer.Option(None, "--limit", help="Process at most N rows."),
    state: str | None = typer.Option(None, "--state", help="Two-letter state code."),
    year_from: int | None = typer.Option(None, "--year-from", help="Earliest year."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Select rows; change nothing."),
    sample: bool = typer.Option(False, "--sample", help="Only the frozen 50-row sample."),
    reset: bool = typer.Option(False, "--reset", help="Re-run the selected rows."),
) -> None:
    """Run one stage over eligible rows."""
    _not_built("04")


@app.command()
def status(
    stage: str | None = typer.Option(None, "--stage", help="Limit to one stage."),
    state: str | None = typer.Option(None, "--state", help="Limit to one state."),
) -> None:
    """Counts by stage and status."""
    _not_built("19")


@app.command()
def review(
    stage: str = typer.Argument(..., help="Stage whose needs_review rows to walk."),
    sample: int | None = typer.Option(None, "--sample", help="Review N random rows."),
) -> None:
    """Interactive queue over needs_review rows."""
    _not_built("04")


@app.command("import")
def import_(
    file: Path = typer.Argument(..., help="File exported from a licensed source."),
    row: int = typer.Option(..., "--row", help="Row id the file belongs to."),
    source: LicensedSource = typer.Option(..., "--source", help="Where the file came from."),
) -> None:
    """Manual import path for licensed sources."""
    _not_built("14")


@identity_app.command("explain")
def identity_explain(row_id: int = typer.Argument(..., help="Row id.")) -> None:
    """Print the waterfall and spans behind an identity decision."""
    _not_built("12")


@app.command("audit")
def audit_cmd(row_id: int = typer.Argument(..., help="Row id.")) -> None:
    """Walk code -> extraction -> capture and print the span in situ."""
    _not_built("19")


@app.command()
def export(
    redacted: bool = typer.Option(False, "--redacted", help="Write the redacted CSV only."),
) -> None:
    """Write the coded workbook (or the redacted CSV)."""
    _not_built("19")


@app.command()
def manifest(
    timestamp: bool = typer.Option(False, "--timestamp", help="Add an RFC 3161 token."),
) -> None:
    """Weekly hash manifest."""
    _not_built("20")


@app.command()
def selftest(
    ctx: typer.Context,
    strict: bool = typer.Option(False, "--strict", help="Exit 1 if any required item is missing."),
) -> None:
    """Report versions, secrets present (names only), data directory, env file and extras."""
    state: AppState = ctx.obj
    checks = run_checks(state.config, state.config_error)
    width = max(len(c.name) for c in checks)
    for c in checks:
        mark = "ok     " if c.ok else ("MISSING" if c.required else "absent ")
        typer.echo(f"[{mark}] {c.name:<{width}}  {c.detail}")
    missing = [c for c in checks if not c.ok and c.required]
    if missing:
        typer.echo(
            f"\n{len(missing)} required item(s) missing: {', '.join(c.name for c in missing)}"
        )
    if strict and missing:
        raise typer.Exit(1)

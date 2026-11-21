"""Command-line entry point. Every command from spec section 10 is registered here.

Commands other than ``selftest`` are stubs until the build named in their message. Each
invocation records an ``audit_log`` row on exit once build 02 creates the table.
"""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import NoReturn

import typer

from efp import audit
from efp import db as database
from efp.config import Config, ConfigError, find_config, load_config
from efp.dbcheck import run_db_checks
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
db_app = typer.Typer(help="Database administration.", no_args_is_help=True)
app.add_typer(db_app, name="db")


def _no_audit() -> int | None:
    return None


@dataclass
class AppState:
    config: Config | None
    config_error: str | None
    audit_id: int | None = None
    # Writes this invocation's audit row. A command that creates the database calls it again
    # afterwards, so the invocation that makes the database is recorded too.
    start_audit: Callable[[], int | None] = field(default=_no_audit)


class LicensedSource(StrEnum):
    newsbank = "newsbank"
    newspapers_com = "newspapers_com"
    westlaw = "westlaw"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"efp {package_version()} (git {git_sha()})")
        raise typer.Exit


def _file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _exit_code_from_exception(exc: BaseException | None) -> int:
    """Exit code for the exception active when the command context closes.

    Typer vendors its own Click, so exceptions are matched by their ``exit_code`` attribute
    (typer.Exit and usage errors carry one) rather than by class.
    """
    if exc is None:
        return 0
    code = getattr(exc, "exit_code", None)
    if isinstance(code, int):
        return code
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
    config_sha256 = _file_sha256(find_config()) if config is not None else None
    state = AppState(config=config, config_error=config_error)
    ctx.obj = state

    secret_values: list[str] = []
    if config is not None:
        try:
            secret_values = load_secrets(config.paths.env_file).secret_values()
        except SecretsError:
            secret_values = []
    setup_logging(config, secret_values)
    register_secret_values(secret_values)

    if config is not None:
        audited = config

        def _start() -> int | None:
            return audit.start(
                audited, argv, started, config_sha256=config_sha256, secret_values=secret_values
            )

        state.start_audit = _start
        state.audit_id = _start()

    def _finish() -> None:
        if config is not None:
            audit.finish(config, state.audit_id, _exit_code_from_exception(sys.exception()))

    ctx.call_on_close(_finish)


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


@db_app.command("migrate")
def db_migrate(ctx: typer.Context) -> None:
    """Create the database if needed and apply pending migrations."""
    state: AppState = ctx.obj
    if state.config is None:
        typer.echo(f"configuration error: {state.config_error}", err=True)
        raise typer.Exit(1)
    try:
        applied = database.migrate(state.config)
    except (database.DatabaseError, database.MigrationError, sqlite3.Error) as exc:
        typer.echo(f"migrate failed: {exc}", err=True)
        raise typer.Exit(1) from None
    if state.audit_id is None:
        state.audit_id = state.start_audit()
    if applied:
        typer.echo("applied: " + ", ".join(f"{m.id:04d}_{m.name}" for m in applied))
    else:
        typer.echo("up to date: no unapplied migrations")


@app.command()
def selftest(
    ctx: typer.Context,
    strict: bool = typer.Option(False, "--strict", help="Exit 1 if any required item is missing."),
    db: bool = typer.Option(
        False, "--db", help="Check the database instead: migrations, triggers, bodies, keys."
    ),
) -> None:
    """Report versions, secrets present (names only), data directory, env file and extras."""
    state: AppState = ctx.obj
    if db:
        if state.config is None:
            typer.echo(f"configuration error: {state.config_error}", err=True)
            raise typer.Exit(1)
        db_checks = run_db_checks(state.config)
        db_width = max(len(c.name) for c in db_checks)
        for check in db_checks:
            typer.echo(
                f"[{'ok    ' if check.ok else 'FAILED'}] {check.name:<{db_width}}  {check.detail}"
            )
        failed = [c for c in db_checks if not c.ok]
        typer.echo(f"\n{len(db_checks)} check(s) run, {len(failed)} failed")
        if failed:
            raise typer.Exit(1)
        return
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

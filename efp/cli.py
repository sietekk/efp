"""Command-line entry point. Every command from spec section 10 is registered here.

Commands other than ``selftest`` are stubs until the build named in their message. Each
invocation records an ``audit_log`` row on exit once build 02 creates the table.
"""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import NoReturn

import typer

from efp import audit
from efp import db as database
from efp import status as status_view
from efp.config import Config, ConfigError, find_config, load_config
from efp.dbcheck import run_db_checks
from efp.export import roundtrip
from efp.ingest import loader
from efp.ingest import report as ingest_report
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


def _no_audit(extra: Mapping[str, object] | None = None) -> int | None:
    return None


@dataclass
class AppState:
    config: Config | None
    config_error: str | None
    audit_id: int | None = None
    # Writes this invocation's audit row. A command that creates the database calls it again
    # afterwards, so the invocation that makes the database is recorded too.
    # ``extra`` adds command-specific facts to args_json (ingest adds the workbook hash).
    start_audit: Callable[[Mapping[str, object] | None], int | None] = field(default=_no_audit)


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

        def _start(extra: Mapping[str, object] | None = None) -> int | None:
            return audit.start(
                audited,
                argv,
                started,
                config_sha256=config_sha256,
                secret_values=secret_values,
                extra=extra,
            )

        state.start_audit = _start
        # ingest writes its row itself, once it knows the workbook hash, sheet and header row.
        if ctx.invoked_subcommand != "ingest":
            state.audit_id = _start()

    def _finish() -> None:
        if config is not None:
            audit.finish(config, state.audit_id, _exit_code_from_exception(sys.exception()))

    ctx.call_on_close(_finish)


def _not_built(build: str) -> NoReturn:
    typer.echo(f"not built yet (build {build})", err=True)
    raise typer.Exit(2)


@app.command()
def ingest(
    ctx: typer.Context,
    workbook: Path = typer.Argument(..., help="Heritage workbook (.xlsx)."),
    check: bool = typer.Option(
        False, "--check", help="Validate and report only: no database, no sample file, no writes."
    ),
) -> None:
    """Load the workbook into the database once, immutably (F1)."""
    state: AppState = ctx.obj
    if state.config is None:
        typer.echo(f"configuration error: {state.config_error}", err=True)
        raise typer.Exit(1)
    extra: dict[str, object] = {}
    try:
        prepared = loader.prepare(state.config, workbook)
        extra.update(
            workbook_sha256=prepared.sha256,
            sheet=prepared.data.sheet_name,
            header_row=prepared.data.header_row,
        )
        result = (
            loader.check_only(state.config, workbook)
            if check
            else loader.ingest(state.config, prepared)
        )
        typer.echo(ingest_report.render(result))
        for note in result.notes:
            typer.echo(f"note: {note}", err=True)
    except (
        loader.IngestError,
        sqlite3.Error,
        database.MigrationError,
        database.DatabaseError,
    ) as exc:
        typer.echo(f"ingest failed: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        if state.audit_id is None:
            state.audit_id = state.start_audit(extra)


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
    ctx: typer.Context,
    stage: str | None = typer.Option(None, "--stage", help="Limit to one stage."),
    state: str | None = typer.Option(None, "--state", help="Limit to one state."),
) -> None:
    """Counts by stage and status over the study population."""
    app_state: AppState = ctx.obj
    if app_state.config is None:
        typer.echo(f"configuration error: {app_state.config_error}", err=True)
        raise typer.Exit(1)
    try:
        conn = database.connect(app_state.config, migrate=False, create=False)
    except (database.DatabaseError, sqlite3.Error):
        typer.echo("no database yet: run `efp ingest <workbook.xlsx>` first", err=True)
        raise typer.Exit(1) from None
    try:
        population, matrix = status_view.status_matrix(
            conn, app_state.config, stage=stage, state=state
        )
    finally:
        conn.close()
    typer.echo(status_view.render(app_state.config.scope.population, population, matrix))


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
    ctx: typer.Context,
    redacted: bool = typer.Option(False, "--redacted", help="Write the redacted CSV only."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="F1 only: round-trip the source columns and verify every row."
    ),
    workbook: Path | None = typer.Option(
        None,
        "--workbook",
        help="With --dry-run: workbook to verify (default paths.source_workbook).",
    ),
) -> None:
    """Write the coded workbook (or the redacted CSV). `--dry-run` runs the F1 round trip."""
    if not dry_run:
        _not_built("19")
    state: AppState = ctx.obj
    if state.config is None:
        typer.echo(f"configuration error: {state.config_error}", err=True)
        raise typer.Exit(1)
    path = workbook if workbook is not None else state.config.paths.source_workbook
    try:
        conn = database.connect(state.config, migrate=False, create=False)
    except (database.DatabaseError, sqlite3.Error):
        typer.echo("no database yet: run `efp ingest <workbook.xlsx>` first", err=True)
        raise typer.Exit(1) from None
    try:
        outcome = roundtrip.verify_round_trip(state.config, conn, path)
    except roundtrip.RoundTripError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    finally:
        conn.close()
    typer.echo("F1 round trip: PASS (only a temporary copy was written, and it was deleted)")
    typer.echo(f"  rows verified against rows.row_sha256 : {outcome.rows_verified:,}")
    typer.echo(f"  rows in the year window (in_scope)    : {outcome.rows_in_window:,}")
    typer.echo(f"  rows with a non-empty Source cell     : {outcome.source_nonempty:,}")
    typer.echo(f"  Source cells carrying a hyperlink     : {outcome.source_hyperlinks:,}")


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
        state.audit_id = state.start_audit(None)
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

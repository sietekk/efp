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

import duckdb
import typer

from efp import audit, linkreport, runner
from efp import capture as capture_mod
from efp import db as database
from efp import manifest as manifest_mod
from efp import modules as module_chain
from efp import spn as spn_mod
from efp import status as status_view
from efp.builtin_modules import register_builtin
from efp.config import Config, ConfigError, find_config, load_config
from efp.court import bulk as court_bulk
from efp.dbcheck import run_db_checks
from efp.export import roundtrip
from efp.ingest import loader
from efp.ingest import report as ingest_report
from efp.livecheck import live_checks
from efp.logging import register_secret_values, setup_logging
from efp.secrets import SecretsError, load_secrets
from efp.selftest import Check, run_checks
from efp.version import git_sha, package_version, repo_root

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
spn_app = typer.Typer(help="Save Page Now queue.", no_args_is_help=True)
app.add_typer(spn_app, name="spn")
links_app = typer.Typer(help="Link survival and the source-domain table.", no_args_is_help=True)
app.add_typer(links_app, name="links")
court_app = typer.Typer(help="Court-record tools.", no_args_is_help=True)
app.add_typer(court_app, name="court")
bulk_app = typer.Typer(help="The CourtListener bulk-data index.", no_args_is_help=True)
court_app.add_typer(bulk_app, name="bulk")


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
    register_builtin()

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
        # Written before any database change, so a crash during the load leaves a trace. (On the
        # very first ingest there is no database yet, so the row is written afterwards instead.)
        if state.audit_id is None:
            state.audit_id = state.start_audit(extra)
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
        OSError,
    ) as exc:
        typer.echo(f"ingest failed: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        if state.audit_id is None:
            state.audit_id = state.start_audit(extra)


def _need_config(state: AppState) -> Config:
    if state.config is None:
        typer.echo(f"configuration error: {state.config_error}", err=True)
        raise typer.Exit(1)
    return state.config


def _open_db(state: AppState, *, migrate: bool = False) -> sqlite3.Connection:
    if state.config is None:
        typer.echo(f"configuration error: {state.config_error}", err=True)
        raise typer.Exit(1)
    try:
        return database.connect(state.config, migrate=migrate, create=False)
    except (database.DatabaseError, sqlite3.Error):
        typer.echo("no database yet: run `efp ingest <workbook.xlsx>` first", err=True)
        raise typer.Exit(1) from None


def _check_stage(config: Config, stage: str | None) -> None:
    if stage is not None and stage not in config.runner.stages:
        typer.echo(f"unknown stage {stage!r}; stages: {', '.join(config.runner.stages)}", err=True)
        raise typer.Exit(2)


@app.command()
def run(
    ctx: typer.Context,
    stage: str = typer.Argument(..., help="Stage name."),
    limit: int | None = typer.Option(None, "--limit", min=1, help="Process at most N rows."),
    state: str | None = typer.Option(None, "--state", help="Two-letter state code."),
    year_from: int | None = typer.Option(None, "--year-from", help="Earliest year."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would run; change nothing."),
    sample: bool = typer.Option(False, "--sample", help="Only the frozen 50-row sample."),
    reset: bool = typer.Option(False, "--reset", help="Set the selected rows back to pending."),
    recover: bool = typer.Option(False, "--recover", help="Reset every running row of the stage."),
    retry_failed: bool = typer.Option(
        False, "--retry-failed", help="Give failed rows at the attempt limit one more attempt."
    ),
    recheck: bool = typer.Option(
        False, "--recheck", help="Also look at done rows; an unchanged input hash is a no-op."
    ),
    force: bool = typer.Option(False, "--force", help="Like --recheck, ignoring the hash."),
) -> None:
    """Run one stage over eligible rows."""
    app_state: AppState = ctx.obj
    config = _need_config(app_state)
    _check_stage(config, stage)
    conn = _open_db(app_state, migrate=not dry_run)
    options = runner.RunOptions(
        stage=stage,
        limit=limit,
        state=state,
        year_from=year_from,
        sample=sample,
        dry_run=dry_run,
        reset=reset,
        recover=recover,
        retry_failed=retry_failed,
        recheck=recheck,
        force=force,
    )
    try:
        run_ctx = runner.make_context(config, conn)
        if dry_run:
            report = runner.dry_run(conn, config, options, run_ctx)
            typer.echo(runner.render_dry_run(report))
            return
        result = runner.run_stage(conn, config, options, run_ctx)
        if result.no_component:
            typer.echo(f"stage {stage}: no component registered yet; nothing was run")
        typer.echo(
            f"stage {stage}: claimed {result.claimed}, done {result.done}, failed {result.failed}, "
            f"needs_review {result.needs_review}, skipped {result.skipped}, no-op {result.noop}, "
            f"recovered {result.recovered}, reset {result.reset}"
        )
        for reason, count in sorted(result.gate_skipped.items()):
            typer.echo(f"  gate {reason}: {count} rows skipped")
    except (runner.RunnerError, sqlite3.Error, database.DatabaseError) as exc:
        typer.echo(f"run failed: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        conn.close()


class StatusFormat(StrEnum):
    md = "md"
    json = "json"


@app.command()
def status(
    ctx: typer.Context,
    stage: str | None = typer.Option(None, "--stage", help="Limit to one stage."),
    state: str | None = typer.Option(None, "--state", help="Limit to one state."),
    fmt: StatusFormat = typer.Option(StatusFormat.md, "--format", help="md or json."),
    links: bool = typer.Option(
        False, "--links", help="Link survival and the source-domain table instead."
    ),
) -> None:
    """Stage x status counts, gate reasons, attempts and errors, and the frozen sample."""
    app_state: AppState = ctx.obj
    config = _need_config(app_state)
    _check_stage(config, stage)
    conn = _open_db(app_state)
    if links:
        try:
            typer.echo(linkreport.render_md(linkreport.build(conn, config), title=False))
        finally:
            conn.close()
        return
    try:
        report = status_view.build_report(conn, config, stage=stage, state=state)
    finally:
        conn.close()
    typer.echo(
        status_view.render_json(report)
        if fmt is StatusFormat.json
        else status_view.render_md(report)
    )


@bulk_app.command("fetch")
def court_bulk_fetch(
    ctx: typer.Context,
    yes: bool = typer.Option(False, "--yes", help="Download. Without it, only list what would be."),
) -> None:
    """List the newest CourtListener bulk files with their sizes; download them with --yes."""
    state: AppState = ctx.obj
    config = _need_config(state)
    conn = _open_db(state, migrate=True)
    try:
        run_ctx = runner.make_context(config, conn)
        try:
            listed = court_bulk.list_bulk_files(run_ctx)
            chosen = court_bulk.latest_set(listed)
        except capture_mod.CaptureError as exc:
            typer.echo(f"could not list the bulk files: {exc}", err=True)
            raise typer.Exit(1) from None
        if not chosen:
            if listed:
                have = sorted({f"{f.kind} {f.date}" for f in listed})
                typer.echo(
                    f"no single date has all of {', '.join(court_bulk.KINDS)}; listed: "
                    f"{'; '.join(have)}",
                    err=True,
                )
            else:
                typer.echo(
                    "the listing names no bulk files; check court.bulk_listing_url", err=True
                )
            raise typer.Exit(1)
        for kind in court_bulk.KINDS:
            item = chosen.get(kind)
            if item:
                typer.echo(f"{kind:<12} {item.date}  {item.size / 1e9:8.2f} GB  {item.key}")
            else:
                typer.echo(f"{kind:<12} (not in the listing)")
        total = court_bulk.total_gb(chosen.values())
        typer.echo(f"total {total:.1f} GB (limit court.bulk_max_gb = {config.court.bulk_max_gb})")
        if not yes:
            typer.echo("nothing downloaded; run again with --yes to download")
            return
        try:
            done = court_bulk.fetch_files(run_ctx, chosen, max_gb=config.court.bulk_max_gb)
        except capture_mod.CaptureError as exc:
            typer.echo(f"download failed: {exc}", err=True)
            raise typer.Exit(1) from None
        for item, capture_id in done:
            typer.echo(f"downloaded {item.key} (capture {capture_id})")
        typer.echo("next: uv run efp court bulk load")
    finally:
        conn.close()


@bulk_app.command("load")
def court_bulk_load(ctx: typer.Context) -> None:
    """Build the local DuckDB index from the downloaded bulk files."""
    state: AppState = ctx.obj
    config = _need_config(state)
    conn = _open_db(state, migrate=True)
    try:
        folder = config.paths.bulk / "courtlistener"
        by_date: dict[str, dict[str, Path]] = {}
        for path in sorted(folder.glob("*.csv*")) if folder.is_dir() else []:
            match = court_bulk.FILE_NAME.search(path.name)
            if match and not path.name.endswith(".part"):
                by_date.setdefault(match["date"], {})[match["kind"]] = path
        complete = [d for d, files in by_date.items() if all(k in files for k in court_bulk.KINDS)]
        if not complete:
            have = {d: sorted(f) for d, f in by_date.items()}
            typer.echo(
                f"no single dataset date has all of {', '.join(court_bulk.KINDS)} downloaded "
                f"(found: {have or 'nothing'}); run efp court bulk fetch --yes",
                err=True,
            )
            raise typer.Exit(1)
        chosen_date = max(complete)  # one quarter: never a mix of dates
        found = by_date[chosen_date]
        dates = dict.fromkeys(found, chosen_date)
        captures: dict[str, int] = {}
        for kind, path in found.items():
            row = conn.execute(
                "SELECT capture_id FROM captures WHERE tool = 'bulk_download' "
                "AND url_requested LIKE ? ORDER BY capture_id DESC LIMIT 1",
                (f"%{path.name}",),
            ).fetchone()
            if row:
                captures[kind] = int(row[0])
        try:
            result = court_bulk.load_index(
                config,
                found,
                dataset_date=max(dates.values()),
                manifest_captures=captures,
                progress=lambda kind, n: typer.echo(f"  {kind}: {n:,} rows read"),
            )
        except court_bulk.BulkLayoutError as exc:
            typer.echo(f"layout error: {exc}", err=True)
            raise typer.Exit(1) from None
        except (OSError, ValueError, duckdb.Error) as exc:
            typer.echo(f"load failed: {type(exc).__name__}: {exc}", err=True)
            raise typer.Exit(1) from None
        typer.echo(
            f"index loaded (dataset {result.dataset_date}): {result.dockets:,} dockets, "
            f"{result.courts:,} courts, {result.parties:,} parties, {result.links:,} links"
        )
    finally:
        conn.close()


@bulk_app.command("status")
def court_bulk_status(ctx: typer.Context) -> None:
    """The local index's dataset date and row counts."""
    state: AppState = ctx.obj
    config = _need_config(state)
    info = court_bulk.index_info(config)
    if info is None:
        typer.echo("the CourtListener bulk index is not loaded")
        return
    typer.echo(
        f"dataset {info.dataset_date}, loaded {info.loaded_at}: {info.dockets:,} dockets, "
        f"{info.courts:,} courts, {info.parties:,} parties"
    )


@links_app.command("report")
def links_report(
    ctx: typer.Context,
    write: bool = typer.Option(
        True, "--write/--print", help="Write reports/first-pass/domains.md (default) or print."
    ),
) -> None:
    """Link survival and the source-domain table (aggregates only: no names, no row text)."""
    state: AppState = ctx.obj
    config = _need_config(state)
    conn = _open_db(state)
    try:
        text = linkreport.render_md(linkreport.build(conn, config))
    finally:
        conn.close()
    if not write:
        typer.echo(text)
        return
    root = repo_root()
    if root is None:
        typer.echo("repository root not found; use --print", err=True)
        raise typer.Exit(1)
    target = root / "reports" / "first-pass" / "domains.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    typer.echo(f"wrote {target.relative_to(root)}")


@app.command()
def modules(
    ctx: typer.Context,
    stage: str | None = typer.Option(None, "--stage", help="Limit to one stage."),
) -> None:
    """Every configured module with its availability and the reason it cannot run."""
    app_state: AppState = ctx.obj
    config = app_state.config
    if config is None:
        typer.echo(f"configuration error: {app_state.config_error}", err=True)
        raise typer.Exit(1)
    if stage is not None and stage not in (*config.runner.stages, *module_chain.COMMAND_CHAINS):
        typer.echo(f"unknown stage {stage!r}", err=True)
        raise typer.Exit(2)
    run_ctx = runner.make_context(config, None)
    rows = module_chain.describe(config, run_ctx, stage=stage)
    width = max((len(r.name) for r in rows), default=4)
    for item in rows:
        mark = "ok     " if item.ok else ("absent " if not item.registered else "skipped")
        typer.echo(f"[{mark}] {item.stage:<13} {item.name:<{width}}  {item.reason}".rstrip())


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
    typer.echo(
        f"  Source non-empty                      : {outcome.source_nonempty:,} of all rows, "
        f"{outcome.source_nonempty_in_window:,} in the window"
    )
    typer.echo(
        f"  Source containing an http(s) URL      : {outcome.source_with_url:,} of all rows, "
        f"{outcome.source_with_url_in_window:,} in the window"
    )
    typer.echo(f"  Source cells carrying a hyperlink     : {outcome.source_hyperlinks:,}")
    typer.echo(f"  data validations in the original      : {outcome.validations_original}")
    typer.echo(f"  data validations in the openpyxl copy : {outcome.validations_copy}")
    if outcome.validations_lost:
        typer.echo(
            "WARNING: openpyxl dropped a data validation (in the Heritage workbook, the "
            "Ideological Motivation dropdown). Cell values are intact (F1 holds), but export "
            "(build 19) cannot rely on openpyxl load/save alone to keep the dropdown.",
            err=True,
        )


@app.command()
def manifest(
    ctx: typer.Context,
    timestamp: bool = typer.Option(False, "--timestamp", help="Add RFC 3161 tokens (two TSAs)."),
    verify: bool = typer.Option(False, "--verify", help="Re-check the stored tokens."),
    bag: bool = typer.Option(False, "--bag", help="Build or update the BagIt bag and validate it."),
    week: str | None = typer.Option(None, "--week", help="Limit to an ISO week, e.g. 2026-W47."),
    manifest_id: int | None = typer.Option(
        None, "--manifest", help="A manifest id for --timestamp / --verify (default: latest)."
    ),
) -> None:
    """Weekly hash manifest, RFC 3161 timestamps, and the BagIt fixity bag."""
    state: AppState = ctx.obj
    config = _need_config(state)
    conn = _open_db(state, migrate=True)
    try:
        if verify:
            target = manifest_id or manifest_mod.latest_manifest_id(conn)
            if target is None:
                typer.echo("no manifest to verify", err=True)
                raise typer.Exit(1)
            results = manifest_mod.verify_manifest(config, conn, target)
            for item in results:
                typer.echo(
                    f"[{'ok    ' if item.ok else 'FAILED'}] {item.tsa_url}  {item.token.name}  "
                    f"{item.gen_time or '-'}  {item.detail}"
                )
            if not all(item.ok for item in results):
                raise typer.Exit(1)
            return
        if bag:
            result = manifest_mod.build_bag(config)
            typer.echo(
                f"bag {'created' if result.created else 'updated'}: {result.path} "
                f"({result.files:,} files): {result.detail}"
            )
            if not result.valid:
                raise typer.Exit(1)
            return
        target = manifest_id
        if target is None and not timestamp:
            built = manifest_mod.build_manifest(config, conn, week=week)
            if built is None:
                typer.echo("nothing new to list: every capture is already in a manifest")
                return
            typer.echo(
                f"manifest {built.path.name}: {built.entry_count:,} bodies, captures "
                f"{built.first_capture_id}-{built.last_capture_id}, sha256 {built.sha256}"
            )
            return
        if target is None:  # --timestamp with no id: build a new manifest if there is anything new
            built = manifest_mod.build_manifest(config, conn, week=week)
            target = built.manifest_id if built else manifest_mod.latest_manifest_id(conn)
            if built:
                typer.echo(
                    f"manifest {built.path.name}: {built.entry_count:,} bodies, "
                    f"sha256 {built.sha256}"
                )
        if target is None:
            typer.echo("no manifest to timestamp", err=True)
            raise typer.Exit(1)
        if timestamp:
            client = capture_mod.Capture(config, conn)
            try:
                outcome = manifest_mod.timestamp_manifest(
                    config, conn, target, manifest_mod.CaptureTsa(client)
                )
            finally:
                client.close()
            for token in outcome.tokens:
                typer.echo(f"token {token.path.name}  {token.tsa_url}  {token.gen_time}")
            for message in outcome.errors:
                typer.echo(f"warning: {message}", err=True)
            if outcome.independent_tsas < 2:
                typer.echo(
                    f"warning: only {outcome.independent_tsas} TSA(s) answered; the policy is two",
                    err=True,
                )
    except (manifest_mod.ManifestError, capture_mod.CaptureError, sqlite3.Error) as exc:
        typer.echo(f"manifest failed: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        conn.close()


@spn_app.command("drain")
def spn_drain(
    ctx: typer.Context,
    limit: int | None = typer.Option(None, "--limit", min=1, help="At most N rows this pass."),
) -> None:
    """Submit queued pages to Save Page Now within its budget, and collect finished jobs."""
    state: AppState = ctx.obj
    config = _need_config(state)
    conn = _open_db(state, migrate=True)
    try:
        try:
            secrets = load_secrets(config.paths.env_file)
        except SecretsError:
            secrets = None
        client = capture_mod.Capture(config, conn)
        try:
            result = spn_mod.drain(client, conn, secrets, limit=limit)
        finally:
            client.close()
        if result.blocked_reason:
            typer.echo(result.blocked_reason)
        typer.echo(
            f"submitted {result.submitted}, archived {result.completed}, "
            f"deferred {result.deferred}, failed {result.failed}, "
            f"still running {result.pending_polls}"
        )
        for note in result.notes:
            typer.echo(f"note: {note}")
    finally:
        conn.close()


@spn_app.command("status")
def spn_status(ctx: typer.Context) -> None:
    """How many pages are queued, in flight, archived and failed."""
    state: AppState = ctx.obj
    conn = _open_db(state)
    try:
        counts = spn_mod.queue_counts(conn)
    finally:
        conn.close()
    typer.echo(", ".join(f"{k} {v}" for k, v in counts.items()))


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


_LIVE_COUNT = [0]


def _live_checks(state: AppState) -> list[Check]:
    """The network checks, recorded under stage 'selftest' in the real database."""
    if state.config is None:
        return []
    conn = _open_db(state, migrate=True)
    try:
        found = live_checks(state.config, conn)
    finally:
        conn.close()
    _LIVE_COUNT[0] = len(found)
    return found


@app.command()
def selftest(
    ctx: typer.Context,
    strict: bool = typer.Option(False, "--strict", help="Exit 1 if any required item is missing."),
    db: bool = typer.Option(
        False, "--db", help="Check the database instead: migrations, triggers, bodies, keys."
    ),
    live: bool = typer.Option(
        False, "--live", help="Also run the checks that use the real network (CDX, RFC 3161)."
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
    if live:
        checks += _live_checks(state)
        for c in checks[-_LIVE_COUNT[0] :]:
            typer.echo(f"[{'ok    ' if c.ok else 'FAILED'}] {c.name:<{width}}  {c.detail}")
    missing = [c for c in checks if not c.ok and c.required]
    if missing:
        typer.echo(
            f"\n{len(missing)} required item(s) missing: {', '.join(c.name for c in missing)}"
        )
    if strict and missing:
        raise typer.Exit(1)

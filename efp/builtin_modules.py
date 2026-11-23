"""Modules that ship with the framework itself: the third-party archivers (``[stages.archive]``).

``register_builtin`` is explicit and idempotent; later builds add their own registration calls
here. ``archive`` is a command chain, not a runner stage: an archiver that cannot run reports why
and is skipped, and nothing about a fetch depends on any of them (the local WARC is the evidence).
"""

from __future__ import annotations

import shutil

from efp.modules import REGISTRY, Availability, ModuleResult, Outcome, Registry
from efp.secrets import SecretsError, load_secrets
from efp.types import Context, Row


def _secrets(ctx: Context):  # type: ignore[no-untyped-def]
    try:
        return load_secrets(ctx.config.paths.env_file)
    except SecretsError:
        return None


class ArchiveSpn:
    name = "archive.spn"
    stage = "archive"

    def available(self, ctx: Context) -> Availability:
        secrets = _secrets(ctx)
        if secrets is None or not (secrets.get("IA_ACCESS") and secrets.get("IA_SECRET")):
            return Availability.unavailable(
                "IA_ACCESS / IA_SECRET not set: pages are queued but not submitted"
            )
        return Availability.available()

    def applies(self, row: Row, ctx: Context) -> bool | str:
        return True

    def run(self, row: Row, ctx: Context) -> ModuleResult:
        """Queue every successful capture of the row; ``efp spn drain`` submits within budget."""
        if ctx.db is None or ctx.capture is None:
            return ModuleResult(Outcome.miss)
        queued = 0
        for capture_id, url in ctx.db.execute(
            "SELECT capture_id, coalesce(url_final, url_requested) FROM captures "
            "WHERE row_id = ? AND http_status BETWEEN 200 AND 299 AND wayback_url IS NULL",
            (row.row_id,),
        ).fetchall():
            queued += int(ctx.capture.enqueue_spn(capture_id, url))
        return ModuleResult(Outcome.hit if queued else Outcome.miss, wrote={"queued": queued})


class ArchivePerma:
    name = "archive.perma"
    stage = "archive"

    def available(self, ctx: Context) -> Availability:
        secrets = _secrets(ctx)
        if secrets is None or not secrets.get("PERMA_API_KEY"):
            return Availability.unavailable(
                "PERMA_API_KEY not set (free tier needs a registrar affiliation: gated)"
            )
        return Availability.available()

    def applies(self, row: Row, ctx: Context) -> bool | str:
        return "cited URLs only: export (build 19) calls Perma for the URLs the paper cites"

    def run(self, row: Row, ctx: Context) -> ModuleResult:
        return ModuleResult(Outcome.miss)


class ArchiveAutoArchiver:
    name = "archive.autoarchiver"
    stage = "archive"

    def available(self, ctx: Context) -> Availability:
        if shutil.which("docker") is None:
            return Availability.unavailable("Docker is not on PATH (needed by Auto Archiver)")
        return Availability.unavailable("built in build 20")

    def applies(self, row: Row, ctx: Context) -> bool | str:
        return "runs over confirmed-relevant URLs in build 20"

    def run(self, row: Row, ctx: Context) -> ModuleResult:
        return ModuleResult(Outcome.miss)


def register_builtin(registry: Registry = REGISTRY) -> None:
    from efp.court.modules import register_modules as register_court
    from efp.documents.modules import register_modules as register_documents
    from efp.reference import register_modules as register_reference
    from efp.resolve_links.modules import register_modules as register_resolve_links

    for module in (ArchiveSpn, ArchivePerma, ArchiveAutoArchiver):
        if registry.get(module.name) is None:
            registry.register(module)
    register_resolve_links(registry)
    register_court(registry)
    register_documents(registry)
    register_reference(registry)

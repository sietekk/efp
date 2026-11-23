"""Identity resolution (build 12): is the person in this row one specific real person?

For each row, candidate person records are gathered from the court records and the reference
datasets (module chain ``[stages.identity]``), each gets a population prior
(``efp.identity.prior``) and Fellegi-Sunter evidence from a Splink model with fixed m and u
(``efp.identity.model``, parameters from the parameter chain), and the posterior is routed to
``confirmed``, ``needs_review`` or ``unresolved`` (``efp.identity.decide``). "None of these
candidates" is an outcome, not an error, and ``unresolved`` stops the chain (the news gate reads
it). Nothing is ever matched on name, state and year alone.

The m and u come from published priors until the operator has labeled the pairs
(``efp review identity --label``); every decision says which (``params=provisional`` or
``params=fitted``) and which prior it used (``prior=census`` or ``prior=ceiling``), and
``efp identity refit`` re-scores exactly the rows whose outcome changes.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

import splink

from efp.identity import candidates as cand
from efp.identity import decide as dec
from efp.identity import model, store
from efp.identity.normalize import load_nicknames
from efp.identity.pairs import Pair, PairBuilder, workbook_term_frequencies
from efp.identity.params import fit, label_count, labeled_examples, load_published, settings_sha
from efp.identity.prior import PriorModel
from efp.identity.profile import CaptureText, Person, row_people
from efp.modules import REGISTRY, Registry, chains_for, run_chain
from efp.reference.states import state_code
from efp.runner import input_hash as hash_values
from efp.types import Context, Row, StageResult

IMPLEMENTED = True
TOOL_VERSION = "3"
STAGE = "identity"
BUILD = "12"
MAX_CANDIDATES_STORED = 25  # a row's best candidates by posterior; the rest are counted, not kept
log = logging.getLogger(__name__)


def gate(row: Row) -> bool | str:
    """Every row whose court stage is done enters (the runner's gate checks that)."""
    return True


def row_state(row: Row) -> str | None:
    raw = row.state or row.source.get("state")
    return state_code(raw) or (str(raw).strip().upper() or None if raw else None)


def decided_by(settings: model.Settings, prior_modes: set[str], *, batch: str = "main") -> str:
    tag = f"splink:{splink.__version__}+model:{settings_sha(settings)[:12]}"
    tag += f";params={'provisional' if settings.provisional else 'fitted'}"
    tag += f";prior={'ceiling' if 'ceiling' in prior_modes else 'census'}"
    tag += f";nick:v{load_nicknames().version}"
    if batch != "main":
        tag += f";batch={batch}"
    return tag


def resolve_settings(ctx: Context, registry: Registry, row: Row) -> model.Settings:
    """Run the parameter chain (labeled pairs, else published priors) and take its settings."""
    for chain in chains_for(ctx.config, STAGE):
        if chain.name != "parameter":
            continue
        run = run_chain(chain, row, ctx, registry)
        for result in run.wrote.values():
            found = result.get("settings")
            if isinstance(found, model.Settings):
                return found
    return load_published()  # the chain is configured empty or every module declined


def _settings_identity(conn: sqlite3.Connection, ctx: Context, have: int, need: int) -> str:
    """What the parameters would be: the fitted settings' hash (a new label changes it only when it
    changes the fit), or the published priors'."""
    if have >= need:
        fitted = fit(
            load_published(), labeled_examples(conn), ctx.config.identity.prior_pseudocount
        )
        return "labeled:" + settings_sha(fitted)
    return "published:" + settings_sha(load_published())


def input_hash(row: Row, ctx: Context, registry: Registry = REGISTRY) -> str:
    conn = ctx.db
    assert conn is not None  # noqa: S101
    records = conn.execute(
        "SELECT record_id, text_capture_id, weak FROM court_records_current WHERE row_id = ? "
        "ORDER BY record_id",
        (row.row_id,),
    ).fetchall()
    idents = conn.execute(
        "SELECT count(*), max(identifier_id) FROM court_identifiers_current WHERE row_id = ?",
        (row.row_id,),
    ).fetchone()
    filings = conn.execute(
        "SELECT text_capture_id FROM documents WHERE row_id = ? AND current = 1 "
        "AND text_capture_id IS NOT NULL ORDER BY document_id",
        (row.row_id,),
    ).fetchall()
    links = conn.execute(
        "SELECT capture_id FROM links WHERE row_id = ? AND capture_id IS NOT NULL ORDER BY link_id",
        (row.row_id,),
    ).fetchall()
    index = conn.execute(
        "SELECT fingerprint FROM ref_index_state ORDER BY built_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    loads = conn.execute(
        "SELECT dataset, load_id FROM ref_sources WHERE current = 1 AND status = 'loaded' "
        "AND dataset IN ('census_surnames','ssa_names','census_population','census_place_county') "
        "ORDER BY dataset, load_id"
    ).fetchall()
    need = ctx.config.identity.labeled_pairs_required
    have = label_count(conn)
    return hash_values(
        row.source.get("name"), row_state(row), row.year, row.source.get("details"),
        [tuple(r) for r in records], tuple(idents), [tuple(r) for r in links],
        [tuple(r) for r in filings],
        index[0] if index else None, [tuple(r) for r in loads],
        _settings_identity(conn, ctx, have, need),
        load_nicknames().sha256, ctx.config.identity.confirm_threshold,
        ctx.config.identity.review_threshold, TOOL_VERSION,
    )  # fmt: skip


class Resources:
    """What one batch shares: the prior tables, the settings, the term frequencies."""

    def __init__(self, ctx: Context, settings: model.Settings) -> None:
        conn = ctx.db
        assert conn is not None  # noqa: S101
        self.settings = settings
        self.prior = PriorModel(conn, ssa_state_min_births=ctx.config.identity.ssa_state_min_births)
        self.pairs = PairBuilder(conn, self.prior)
        self.tf = {k: model.term_frequencies(v) for k, v in workbook_term_frequencies(conn).items()}
        self.texts = CaptureText(conn, ctx.config.paths.store)


def gather(
    ctx: Context, registry: Registry, row: Row, res: Resources
) -> tuple[list[Person], list[Person], list[dict[str, Any]]]:
    """``(left persons, candidates, module records)`` for one row."""
    conn = ctx.db
    assert conn is not None  # noqa: S101
    state = row_state(row)
    parts, people = row_people(conn, res.texts, row.row_id, dict(row.source), state)
    task = cand.IdentityTask(
        row_id=row.row_id, year=row.year, state=state, source=dict(row.source),
        parts=parts, people=people, texts=res.texts,
    )  # fmt: skip
    records: list[dict[str, Any]] = []
    found: list[Person] = []
    for chain in chains_for(ctx.config, STAGE):
        if chain.name != "candidate":
            continue
        run = run_chain(chain, task, ctx, registry)
        records.extend(run.records)
        for result in run.wrote.values():
            found.extend(result.get("candidates", []))
    return people, found, records


def build_pairs(
    row: Row, people: list[Person], candidates: list[Person], res: Resources, next_id: int
) -> list[tuple[Person, Pair]]:
    """Each candidate against every name form of the row (a reference candidate only against the
    form that found it)."""
    out: list[tuple[Person, Pair]] = []
    for candidate in candidates:
        lefts = (
            people if candidate.source == "court"
            else [p for p in people if p.alias_index == candidate.alias_index]
        )  # fmt: skip
        for left in lefts or people[:1]:
            out.append((candidate, res.pairs.build(next_id, left, candidate, row.year)))
            next_id += 1
    return out


def best_per_candidate(
    pairs: list[tuple[Person, Pair]], waterfalls: dict[int, dict[str, Any]]
) -> list[dec.Scored]:
    best: dict[int, dec.Scored] = {}
    for candidate, pair in pairs:
        scored = dec.score_candidate(candidate, pair, waterfalls[pair.pair_id])
        key = id(candidate)
        if key not in best or scored.posterior > best[key].posterior:
            best[key] = scored
    return list(best.values())


def process_batch(
    batch: list[Row], ctx: Context, registry: Registry = REGISTRY
) -> list[StageResult]:
    conn = ctx.db
    assert conn is not None and batch  # noqa: S101
    ident = ctx.config.identity
    settings = resolve_settings(ctx, registry, batch[0])
    res = Resources(ctx, settings)
    gathered: dict[int, tuple[list[Person], list[tuple[Person, Pair]], list[dict[str, Any]]]] = {}
    all_pairs: list[tuple[Person, Pair]] = []
    next_id = 1
    for row in batch:
        people, cands, records = gather(ctx, registry, row, res)
        pairs = build_pairs(row, people, cands, res, next_id)
        next_id += len(pairs)
        gathered[row.row_id] = (people, pairs, records)
        all_pairs.extend(pairs)
    waterfalls = model.score(
        [{"pair_id": p.pair_id, "left": p.left, "right": p.right} for _, p in all_pairs],
        settings, tf=res.tf, cap=ident.tf_adjustment_cap,
    )  # fmt: skip
    results: list[StageResult] = []
    for row in batch:
        people, pairs, records = gathered[row.row_id]
        scored = best_per_candidate(pairs, waterfalls)
        decision = dec.decide(
            scored, confirm=ident.confirm_threshold, review=ident.review_threshold
        )
        modes = {s.pair.prior.mode for s in scored} or {res.prior.default_mode}
        tag = decided_by(settings, modes)
        kept = sorted(scored, key=lambda s: s.posterior, reverse=True)[:MAX_CANDIDATES_STORED]
        human = store.human_decision(conn, row.row_id)
        with store.atomic(conn):
            store.write_candidates(conn, row.row_id, kept, settings_sha=settings_sha(settings))
            if human is None:
                store.write_decision(
                    conn, row.row_id, decision, decided_by=tag,
                    thresholds=(ident.confirm_threshold, ident.review_threshold),
                )  # fmt: skip
            # an operator's decision stands: a re-run scores again but never overrides it
        status = "needs_review" if decision.outcome == "needs_review" else "done"
        if human is not None:
            status = "done"
        results.append(
            StageResult(
                row_id=row.row_id,
                stage=STAGE,
                status=status,
                module="identity",
                input_hash=input_hash(row, ctx, registry),
                tool_version=TOOL_VERSION,
                summary_json={
                    "outcome": decision.outcome,
                    "reason": decision.reason,
                    "posterior": decision.best.posterior if decision.best else None,
                    "candidates": len(scored),
                    "candidates_stored": len(kept),
                    "params": settings.name,
                    "prior": "ceiling" if "ceiling" in modes else "census",
                    "decided_by": tag,
                    "operator_decision_kept": human[1] if human else None,
                    "modules": records,
                },
            )
        )
    return results


def run(batch: list[Row], ctx: Context) -> list[StageResult]:
    if not batch:
        return []
    try:
        return process_batch(batch, ctx)
    except Exception as exc:  # a bug must fail the batch's rows, not the whole run
        log.exception("identity failed for a batch of %d rows", len(batch))
        if ctx.db is not None and ctx.db.in_transaction:
            ctx.db.rollback()
        return [
            StageResult(
                row_id=r.row_id, stage=STAGE, status="failed",
                error=f"{type(exc).__name__}: {exc}"[:300], tool_version=TOOL_VERSION,
            )
            for r in batch
        ]  # fmt: skip


def register_modules(registry: Registry) -> None:
    from efp.identity.params import ParamsLabeledPairs, ParamsPublishedPriors

    cand.register_modules(registry)
    for module in (ParamsLabeledPairs, ParamsPublishedPriors):
        if registry.get(module.name) is None:
            registry.register(module)

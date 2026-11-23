"""Parameter modules (``[stages.identity].parameter_modules``, policy ``first_available``).

``identity.params.labeled_pairs`` fits m and u from the operator's labeled pairs once at least
``identity.labeled_pairs_required`` match / non-match labels exist; until then
``identity.params.published_priors`` supplies m and u from ``settings/published_priors_v1.json``,
and every decision made with it is tagged ``params=provisional``.

The fit is a shrinkage estimate, not EM: for each comparison the m (or u) of a level is the labeled
count among matches (non-matches) with that level, plus ``identity.prior_pseudocount`` pairs' worth
of the published value, over the total. The exact-name levels of surname and given name stay
neutral (m = u) in a fit: the population prior already carries name agreement, and a fit must not
let the name be counted twice.
"""

from __future__ import annotations

import copy
import hashlib
import json
import random  # seeded resampling, not security
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from efp.identity.labels import _stratum
from efp.identity.model import COMPARISONS, LEVELS, Settings
from efp.identity.strata import Strata
from efp.modules import Availability, ModuleResult, Outcome
from efp.types import Context, Row

STAGE = "identity"
SETTINGS_DIR = Path(__file__).with_name("settings")
PUBLISHED_PATH = SETTINGS_DIR / "published_priors_v1.json"
NEUTRAL_EXACT = ("surname", "given_name")
LABEL_OUTCOMES = {"label=match": True, "label=nonmatch": False}


def canonical(params: dict[str, Any]) -> str:
    return json.dumps(params, sort_keys=True, separators=(",", ":"))


def settings_sha(settings: Settings) -> str:
    return hashlib.sha256(canonical(settings.params).encode("utf-8")).hexdigest()


def load_published() -> Settings:
    return Settings(
        "published_priors_v1", "published_priors", json.loads(PUBLISHED_PATH.read_text())
    )


def fitted_dir(data_dir: str | Path) -> Path:
    return Path(data_dir) / "identity" / "settings"


Example = tuple[bool, dict[str, str]] | tuple[bool, dict[str, str], float]


def labeled_examples(conn: sqlite3.Connection, common_rank: int = 1000) -> list[Example]:
    """``(is_match, {comparison: level}, weight)`` for each labeled pair, from the operator's
    labels (the latest label for a candidate wins; ``unsure`` is not a label).

    The queue samples some strata more heavily than their share of the candidates, so each label
    is weighted by its stratum's population over its stratum's labeled count (a pair from a thin
    stratum stands for more candidates), then the weights are scaled to average one. The pairs
    are also spread over the posterior range inside a stratum; that is not corrected, and the
    fit's pseudocount keeps it near the published values."""
    latest: dict[int, str] = {}
    for candidate_id, outcome in conn.execute(
        "SELECT candidate_id, outcome FROM decisions WHERE kind = 'review' AND stage = 'identity' "
        "AND outcome LIKE 'label=%' AND candidate_id IS NOT NULL ORDER BY decision_id"
    ):
        latest[int(candidate_id)] = str(outcome)
    strata = Strata(conn, common_rank)
    population: dict[str, int] = {}
    for source, raw in conn.execute(
        "SELECT source, fields_json FROM identity_candidates WHERE current = 1"
    ):
        key = _stratum(strata, json.loads(raw), str(source))
        population[key] = population.get(key, 0) + 1
    stratum_of = {
        int(c): str(s)
        for c, s in conn.execute("SELECT candidate_id, stratum FROM identity_label_queue")
    }
    out: list[tuple[bool, dict[str, str], float]] = []
    ids: list[int] = []
    for candidate_id, outcome in latest.items():
        if outcome not in LABEL_OUTCOMES:
            continue
        found = conn.execute(
            "SELECT waterfall_json FROM identity_candidates WHERE candidate_id = ?", (candidate_id,)
        ).fetchone()
        if found is None or not found[0]:
            continue
        steps = json.loads(found[0])["steps"]
        out.append((LABEL_OUTCOMES[outcome], {s["comparison"]: s["level"] for s in steps}, 1.0))
        ids.append(candidate_id)
    counted: dict[str, int] = {}
    for c in ids:
        counted[stratum_of.get(c, "")] = counted.get(stratum_of.get(c, ""), 0) + 1
    weights = [
        float(population.get(stratum_of.get(c, ""), 1)) / counted[stratum_of.get(c, "")]
        for c in ids
    ]
    mean = sum(weights) / len(weights) if weights else 1.0
    return [(m, lv, w / mean) for (m, lv, _), w in zip(out, weights, strict=True)]


def label_count(conn: sqlite3.Connection) -> int:
    return len(labeled_examples(conn))


def fit(published: Settings, examples: Sequence[Example], pseudocount: float) -> Settings:
    """m and u from labeled pairs, shrunk towards the published values."""
    params = copy.deepcopy(published.params)
    matches = [(e[1], e[2] if len(e) > 2 else 1.0) for e in examples if e[0]]
    non_matches = [(e[1], e[2] if len(e) > 2 else 1.0) for e in examples if not e[0]]
    for comparison in COMPARISONS:
        entries = params["comparisons"][comparison]["levels"]
        for which, group in (("m", matches), ("u", non_matches)):
            counts = {lvl: 0.0 for lvl in LEVELS[comparison]}
            for levels, weight in group:
                lvl = levels.get(comparison)
                if lvl in counts:  # a null comparison says nothing about m or u
                    counts[lvl] += weight
            total = sum(counts.values())
            prior_total = sum(float(entries[lvl][which]) for lvl in LEVELS[comparison])
            for lvl in LEVELS[comparison]:
                prior_share = float(entries[lvl][which]) / prior_total if prior_total else 0.0
                entries[lvl][which] = (counts[lvl] + pseudocount * prior_share) / (
                    total + pseudocount
                )
        for lvl in LEVELS[comparison]:
            entries[lvl]["basis"] = "labeled"
            entries[lvl]["note"] = (
                f"shrinkage fit: {len(matches)} matches, {len(non_matches)} non-matches, "
                f"pseudocount {pseudocount:g}, towards published_priors_v1"
            )
        if comparison in NEUTRAL_EXACT:
            exact = entries["exact"]
            exact["m"] = exact["u"] = float(exact["u"])
            exact["note"] += "; exact-name level kept neutral (the prior carries the name)"
    params["name"] = "labeled_pairs"
    params["fitted_from"] = {
        "matches": len(matches), "non_matches": len(non_matches), "pseudocount": pseudocount,
        "published": "published_priors_v1",
    }  # fmt: skip
    sha = hashlib.sha256(canonical(params).encode()).hexdigest()[:12]
    return Settings(f"labeled_{sha}", "labeled_pairs", params)


class ParamsPublishedPriors:
    name = "identity.params.published_priors"
    stage = STAGE

    def available(self, ctx: Context) -> Availability:
        if not PUBLISHED_PATH.is_file():
            return Availability.unavailable(f"{PUBLISHED_PATH.name} is missing")
        return Availability.available()

    def applies(self, row: Row, ctx: Context) -> bool | str:
        return True

    def run(self, row: Row, ctx: Context) -> ModuleResult:
        return ModuleResult(Outcome.hit, wrote={"settings": load_published()})


class ParamsLabeledPairs:
    name = "identity.params.labeled_pairs"
    stage = STAGE

    def available(self, ctx: Context) -> Availability:
        if ctx.db is None:
            return Availability.unavailable("no database")
        have, need = label_count(ctx.db), ctx.config.identity.labeled_pairs_required
        if have < need:
            return Availability.unavailable(
                f"{have} of {need} labeled pairs (label them with `efp review identity --label`)"
            )
        return Availability.available()

    def applies(self, row: Row, ctx: Context) -> bool | str:
        return True

    def run(self, row: Row, ctx: Context) -> ModuleResult:
        assert ctx.db is not None  # noqa: S101
        settings = fit(
            load_published(), labeled_examples(ctx.db), ctx.config.identity.prior_pseudocount
        )
        target = fitted_dir(ctx.config.paths.data_dir)
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{settings.name}.json").write_text(canonical(settings.params))
        return ModuleResult(Outcome.hit, wrote={"settings": settings})


def bootstrap_fits(
    published: Settings, examples: Sequence[Example], pseudocount: float,
    *, resamples: int, seed: int,
) -> list[Settings]:  # fmt: skip
    rng = random.Random(seed)  # noqa: S311
    return [
        fit(published, [rng.choice(examples) for _ in examples], pseudocount)
        for _ in range(resamples)
    ]

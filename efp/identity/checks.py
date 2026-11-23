"""``efp selftest --identity``: the model's own guarantees, checked locally on this workbook.

The central one is the property the design exists for (spec section 8): a candidate that agrees only
on name, state and year has evidence of zero and a posterior equal to the prior, which for any
common name is far below the review threshold. It is run here, through the same prior, model and
decision rule the stage uses, for the ten most common surnames in the workbook, under the prior
that is loaded and under the ceiling prior. Only ranks and posteriors are printed, never a name.
"""

from __future__ import annotations

import sqlite3
from collections import Counter

from efp import db as database
from efp.config import Config
from efp.court.names import parse_name
from efp.identity import decide as dec
from efp.identity import model, params
from efp.identity.normalize import load_nicknames
from efp.identity.pairs import PairBuilder, workbook_term_frequencies
from efp.identity.prior import PriorModel
from efp.identity.profile import Person
from efp.selftest import Check

LEVEL_KEYS = ("m", "u", "basis")


def name_only_posteriors(
    conn: sqlite3.Connection,
    config: Config,
    settings: model.Settings,
    top: int = 10,
    *,
    rarest: bool = False,
) -> list[tuple[int, str, float, float]]:
    """``(surname rank in the workbook, prior mode, evidence bits, posterior)`` for each of the
    ``top`` most common (or, with ``rarest``, least common) workbook surnames, a candidate agreeing
    on the name and state only. The real prior is strongest where the surname is rare, so the
    guarantee is checked at both ends."""
    surnames: Counter[str] = Counter()
    givens: dict[str, Counter[str]] = {}
    states: dict[str, Counter[str]] = {}
    for name, state in conn.execute("SELECT name, state FROM rows WHERE in_scope = 1"):
        parts = parse_name(name)
        if not parts.surname:
            continue
        surnames[parts.surname] += 1
        givens.setdefault(parts.surname, Counter())[parts.given or "John"] += 1
        states.setdefault(parts.surname, Counter())[str(state or "")] += 1
    real = PriorModel(conn, ssa_state_min_births=config.identity.ssa_state_min_births)
    # the same tables with nothing loaded: the ceiling prior
    empty = sqlite3.connect(":memory:")
    empty.executescript(
        "CREATE TABLE ref_surname_freq_current(surname, prop100k, rank); "
        "CREATE TABLE ref_given_freq_current(name, sex, year, state, count); "
        "CREATE TABLE ref_population_current(state, age_band, year, count); "
        "CREATE TABLE ref_sources(dataset, notes_json, load_id, current, status);"
    )
    ceiling = PriorModel(empty)
    tf = {k: model.term_frequencies(v) for k, v in workbook_term_frequencies(conn).items()}
    out: list[tuple[int, str, float, float]] = []
    ordered = (
        sorted(surnames.items(), key=lambda kv: (kv[1], kv[0]))
        if rarest
        else surnames.most_common()
    )
    for rank, (surname, _) in enumerate(ordered[:top], 1):
        given = givens[surname].most_common(1)[0][0]
        state = states[surname].most_common(1)[0][0] or None
        for mode, prior_model in (("census", real), ("ceiling", ceiling)):
            builder = PairBuilder(conn, prior_model)
            left = Person("row", "0", given, "", surname, state=state)
            right = Person("court", "x", given, "", surname, state=state)
            pair = builder.build(1, left, right, None)
            water = model.score(
                [{"pair_id": 1, "left": pair.left, "right": pair.right}], settings, tf=tf,
                cap=config.identity.tf_adjustment_cap,
            )[1]  # fmt: skip
            scored = dec.score_candidate(right, pair, water)
            out.append((rank, mode, water["evidence_bits"], scored.posterior))
    return out


def identity_checks(config: Config, conn: sqlite3.Connection | None = None) -> list[Check]:
    own = conn is None
    connection = conn or database.connect(config, migrate=False, create=False)
    try:
        checks: list[Check] = []
        published = params.load_published()
        bad = [
            f"{c}.{lvl}"
            for c, entry in published.params["comparisons"].items()
            for lvl, v in entry["levels"].items()
            if not all(k in v for k in LEVEL_KEYS) or not (0 < v["m"] < 1 and 0 < v["u"] < 1)
        ]
        checks.append(
            Check(
                "identity.settings",
                not bad,
                f"published_priors_v1 sha {params.settings_sha(published)[:12]}"
                + (f"; levels without m, u and a basis: {bad}" if bad else ""),
            )
        )
        nicknames = load_nicknames()
        checks.append(
            Check(
                "identity.nicknames",
                nicknames.version != "0",
                f"v{nicknames.version} sha {nicknames.sha256[:12]}",
            )
        )
        labels = params.label_count(connection)
        need = config.identity.labeled_pairs_required
        checks.append(
            Check(
                "identity.parameters",
                True,
                f"{labels} of {need} labeled pairs: "
                + (
                    "labeled_pairs would run"
                    if labels >= need
                    else "published_priors (provisional)"
                ),
                required=False,
            )
        )
        review = config.identity.review_threshold
        for name, rarest, word in (
            ("identity.guarantee", False, "most common"),
            ("identity.guarantee_rare", True, "rarest"),
        ):
            results = name_only_posteriors(connection, config, published, rarest=rarest)
            worst = max((p for _, _, _, p in results), default=0.0)
            checks.append(
                Check(
                    name,
                    worst < review if results else True,
                    f"name+state-only candidates for the {len(results) // 2} {word} surnames: "
                    f"highest posterior {worst:.4g} (review threshold {review})"
                    if results
                    else "no rows to check",
                )
            )
        return checks
    finally:
        if own:
            connection.close()

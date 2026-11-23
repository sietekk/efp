"""The evidence model: Splink 4.0.x on DuckDB with fixed m and u (spec section 8).

Each pair is one left person (the row, or one of its aliases) and one right person (a candidate).
The pairs are built by ``efp.identity.pairs`` and blocked on ``pair_id``: candidates were
gathered by
name, state and the row's own documents already, so Splink is used for what it is good at, the
comparison levels, the Bayes factors and the per-comparison explanation. m and u are fixed by the
parameter module and Splink never estimates anything (1,481 rows are too few for stable EM).

Evidence is the sum of the per-comparison log2 Bayes factors. Splink's own match probability is not
used: it would carry a prior of its own, and the prior here is the population prior of
``efp.identity.prior``. The term-frequency adjustment is on for surname and given name (weight 0.5,
``tf_minimum_u_value = 1e-4``) with frequencies from the whole workbook, so a batch never changes a
score, and may only lower a weight, never raise it (``identity.tf_adjustment_cap``): rarity is
already in the prior.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from typing import Any

import pandas as pd
import splink.comparison_level_library as cll
import splink.comparison_library as cl
from splink import DuckDBAPI, Linker, SettingsCreator, block_on

logging.getLogger("splink").setLevel(logging.WARNING)  # per-batch timing lines are noise

NULL_LEVEL = "null"

# comparison -> its levels, best first, the else level last. ``null`` is implicit (BF 1).
LEVELS: dict[str, tuple[str, ...]] = {
    "surname": ("exact", "jaro_winkler", "double_metaphone", "else"),
    "given_name": ("exact", "nickname", "jaro_winkler", "else"),
    "middle": ("exact", "initial", "conflict"),
    "dob": ("exact", "within_1", "within_3", "conflict"),
    "city": ("exact", "same_county", "same_state", "conflict"),
    "case_number": ("exact", "else"),
}
COMPARISONS = tuple(LEVELS)
TF_COLUMNS = {"surname": "surname", "given_name": "given_name"}

_SQL: dict[str, dict[str, str]] = {
    "surname": {
        "exact": "surname_l = surname_r",
        "jaro_winkler": "jaro_winkler_similarity(surname_l, surname_r) >= 0.92",
        "double_metaphone": "len(list_intersect(surname_dm_l, surname_dm_r)) > 0",
    },
    "given_name": {
        "exact": "given_name_l = given_name_r",
        "nickname": "list_contains(given_forms_r, given_name_l) "
        "OR list_contains(given_forms_l, given_name_r)",
        "jaro_winkler": "jaro_winkler_similarity(given_name_l, given_name_r) >= 0.92",
    },
    "middle": {
        "exact": "middle_l = middle_r AND length(middle_l) > 1",
        "initial": "substr(middle_l, 1, 1) = substr(middle_r, 1, 1) "
        "AND (length(middle_l) = 1 OR length(middle_r) = 1)",
    },
    "dob": {
        "exact": "dob_l IS NOT NULL AND dob_r IS NOT NULL AND dob_l = dob_r",
        "within_1": "NOT (dob_l IS NOT NULL AND dob_r IS NOT NULL) "
        "AND greatest(0, by_lo_r - by_hi_l, by_lo_l - by_hi_r) <= 1",
        "within_3": "NOT (dob_l IS NOT NULL AND dob_r IS NOT NULL) "
        "AND greatest(0, by_lo_r - by_hi_l, by_lo_l - by_hi_r) <= 3",
    },
    "city": {
        "exact": "city_l = city_r AND state_l = state_r",
        "same_county": "state_l = state_r AND list_has_any(counties_l, counties_r)",
        "same_state": "state_l = state_r",
    },
    "case_number": {"exact": "case_no_l = case_no_r"},
}
_NULL_COLUMN = {
    "surname": "surname",
    "given_name": "given_name",
    "middle": "middle",
    "dob": "by_lo",
    "city": "city",
    "case_number": "case_no",
}


@dataclass(frozen=True)
class Settings:
    """A parameter set: m and u for every level of every comparison."""

    name: str
    kind: str  # 'published_priors' | 'labeled_pairs'
    params: dict[str, Any]

    @property
    def provisional(self) -> bool:
        return self.kind != "labeled_pairs"

    def mu(self, comparison: str, level: str) -> tuple[float, float]:
        entry = self.params["comparisons"][comparison]["levels"][level]
        return float(entry["m"]), float(entry["u"])


def _level(comparison: str, level: str, settings: Settings) -> Any:
    m, u = settings.mu(comparison, level)
    built: Any
    if level == "else" or level == "conflict":
        built = cll.ElseLevel()
    else:
        built = cll.CustomLevel(_SQL[comparison][level], label_for_charts=f"{comparison}:{level}")
    kwargs: dict[str, Any] = {"m_probability": m, "u_probability": u}
    column = TF_COLUMNS.get(comparison)
    if column and level == "exact":
        entry = settings.params["comparisons"][comparison]
        kwargs.update(
            tf_adjustment_column=column,
            tf_adjustment_weight=float(entry.get("tf_weight", 0.5)),
            tf_minimum_u_value=float(entry.get("tf_min_u", 1e-4)),
        )
    return built.configure(**kwargs)


def build_settings(settings: Settings) -> SettingsCreator:
    comparisons: list[Any] = []
    for comparison in COMPARISONS:
        levels: list[Any] = [cll.NullLevel(_NULL_COLUMN[comparison])]
        levels += [_level(comparison, lvl, settings) for lvl in LEVELS[comparison]]
        comparisons.append(
            cl.CustomComparison(output_column_name=comparison, comparison_levels=levels)
        )
    return SettingsCreator(
        link_type="link_only",
        unique_id_column_name="uid",
        comparisons=comparisons,
        blocking_rules_to_generate_predictions=[block_on("pair_id")],
        retain_intermediate_calculation_columns=True,
        retain_matching_columns=True,
    )


PAIR_COLUMNS = (
    "uid", "pair_id", "surname", "surname_dm", "given_name", "given_forms", "middle", "dob",
    "by_lo", "by_hi", "city", "counties", "state", "case_no",
)  # fmt: skip


def _frame(pairs: list[dict[str, Any]], side: str) -> pd.DataFrame:
    rows = []
    for pair in pairs:
        person = pair[side]
        rows.append({"uid": f"{side}{pair['pair_id']}", "pair_id": pair["pair_id"], **person})
    frame = pd.DataFrame(rows, columns=list(PAIR_COLUMNS))
    for column in ("by_lo", "by_hi"):
        frame[column] = frame[column].astype("Int64")
    for column in ("surname", "given_name", "middle", "dob", "city", "state", "case_no"):
        frame[column] = frame[column].astype("object").where(frame[column].notna(), None)
    return frame


def term_frequencies(values: list[str]) -> pd.DataFrame:
    counts = pd.Series([v for v in values if v]).value_counts()
    total = float(counts.sum()) or 1.0
    return pd.DataFrame({"value": counts.index, "tf": (counts / total).to_numpy()})


def waterfall_row(settings: Settings, record: dict[str, Any], *, cap: bool) -> dict[str, Any]:
    """One pair's per-comparison evidence from a Splink prediction row."""
    steps: list[dict[str, Any]] = []
    bits = 0.0
    for comparison in COMPARISONS:
        gamma = int(record[f"gamma_{comparison}"])
        levels = LEVELS[comparison]
        if gamma < 0:
            level, m, u, bf = NULL_LEVEL, None, None, 1.0
        else:
            level = levels[len(levels) - 1 - gamma]
            m, u = settings.mu(comparison, level)
            bf = float(record[f"bf_{comparison}"])
        tf_raw = record.get(f"bf_tf_adj_{comparison}")
        tf_raw = (
            1.0
            if tf_raw is None or (isinstance(tf_raw, float) and math.isnan(tf_raw))
            else float(tf_raw)
        )
        tf_used = min(tf_raw, 1.0) if cap else tf_raw
        weight = math.log2(bf * tf_used) if bf > 0 else -50.0
        bits += weight
        steps.append(
            {
                "comparison": comparison,
                "level": level,
                "m": m,
                "u": u,
                "bayes_factor": bf,
                "tf_adjustment_raw": tf_raw,
                "tf_adjustment_used": tf_used,
                "log2_weight": weight,
                "left": _shown(record, comparison, "l"),
                "right": _shown(record, comparison, "r"),
            }
        )
    return {"evidence_bits": bits, "steps": steps}


def _shown(record: dict[str, Any], comparison: str, side: str) -> Any:
    column = {"dob": "dob", "case_number": "case_no"}.get(comparison, comparison)
    value = record.get(f"{column}_{side}")
    if isinstance(value, float) and math.isnan(value):
        return None
    if comparison == "dob" and value is None:
        lo, hi = record.get(f"by_lo_{side}"), record.get(f"by_hi_{side}")
        if lo is None or hi is None or pd.isna(lo) or pd.isna(hi):
            return None
        return f"born {int(lo)}-{int(hi)}"
    return value


def score(
    pairs: list[dict[str, Any]],
    settings: Settings,
    *,
    tf: dict[str, pd.DataFrame],
    cap: bool = True,
) -> dict[int, dict[str, Any]]:
    """``{pair_id: waterfall}``. Each pair is ``{"pair_id", "left": {...}, "right": {...}}``."""
    if not pairs:
        return {}
    left, right = _frame(pairs, "left"), _frame(pairs, "right")
    linker = Linker([left, right], build_settings(settings), db_api=DuckDBAPI())
    for comparison, column in TF_COLUMNS.items():
        table = tf[comparison].rename(columns={"value": column, "tf": f"tf_{column}"})
        linker.table_management.register_term_frequency_lookup(table, column)
    frame = linker.inference.predict().as_pandas_dataframe()
    out: dict[int, dict[str, Any]] = {}
    for record in frame.to_dict("records"):
        out[int(record["pair_id_l"])] = waterfall_row(settings, record, cap=cap)
    return out


def settings_json(settings: Settings) -> str:
    return json.dumps(settings.params, sort_keys=True, separators=(",", ":"))

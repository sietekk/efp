"""The parameter-stability chart: Splink's own ``parameter_estimate_comparisons`` chart.

Written to ``reports/identity/`` for whichever parameter module ran. With the published priors it
shows the one set of values (there is nothing to compare yet, and the report says so); with labeled
pairs it shows the published value beside 200 bootstrap fits of the labels, which is the stability
the spec asks to see.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from splink.internals.charts import parameter_estimate_comparisons

from efp.identity.model import COMPARISONS, LEVELS, Settings

REPORT_DIR = Path(__file__).resolve().parents[2] / "reports" / "identity"


def records(estimates: dict[str, Settings]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for description, settings in estimates.items():
        for order, comparison in enumerate(COMPARISONS):
            levels = LEVELS[comparison]
            for index, level in enumerate(levels):
                for which in ("m", "u"):
                    p = float(settings.params["comparisons"][comparison]["levels"][level][which])
                    out.append(
                        {
                            "m_or_u": which,
                            "estimated_probability": p,
                            "estimate_description": description,
                            "estimated_probability_as_log_odds": (
                                math.log2(p / (1 - p)) if 0 < p < 1 else None
                            ),
                            "sql_condition": level,
                            "comparison_level_label": level,
                            "comparison_vector_value": len(levels) - 1 - index,
                            "comparison_name": comparison,
                            "comparison_sort_order": order,
                            "col_header": "Estimated m probability"
                            if which == "m"
                            else "Estimated u probability",
                        }
                    )
    return out


def write(
    module: str, estimates: dict[str, Settings], directory: Path | None = None
) -> tuple[Path, Path]:
    directory = directory or REPORT_DIR  # looked up now, so a test can point it elsewhere
    directory.mkdir(parents=True, exist_ok=True)
    data = records(estimates)
    stem = f"parameter_estimates_{module.rsplit('.', 1)[-1]}"
    chart = parameter_estimate_comparisons(data, as_dict=False)
    html, spec = directory / f"{stem}.html", directory / f"{stem}.json"
    chart.save(str(html))
    spec.write_text(json.dumps(parameter_estimate_comparisons(data, as_dict=True), indent=1))
    return html, spec

"""Plain-text report for ``efp ingest``: counts only, never names or row text."""

from __future__ import annotations

from efp.ingest.loader import Result

BAND_ORDER = ("before 2000", "2000-09", "2010-16", "2017-25", "after 2025")

HEADLINES = {
    "ingested": "INGESTED",
    "noop": "NO-OP: already ingested, and the workbook, window and every row hash match",
    "checked": "CHECK ONLY: nothing was written",
}


def render(result: Result) -> str:
    prepared = result.prepared
    summary = prepared.summary
    lines = [
        HEADLINES[result.status],
        f"  workbook sha256 : {prepared.sha256}",
        f"  sheet           : {prepared.data.sheet_name!r}, header on sheet row "
        f"{prepared.data.header_row}",
        f"  rows loaded     : {summary.total_rows:,}",
        f"  in the window   : {summary.in_window:,}   (in_scope = 1)",
        f"  non-empty Source: {summary.source_nonempty:,}",
        "",
        "rows by year band and case type:",
    ]
    case_types = sorted({c for _, c in summary.by_band_and_case_type})
    width = max([len("case type"), *(len(c) for c in case_types)])
    present = [b for b in BAND_ORDER if any(k[0] == b for k in summary.by_band_and_case_type)]
    lines.append(
        f"  {'case type':<{width}}  " + "  ".join(f"{b:>11}" for b in present) + "      total"
    )
    for case_type in case_types:
        counts = [summary.by_band_and_case_type.get((b, case_type), 0) for b in present]
        lines.append(
            f"  {case_type:<{width}}  "
            + "  ".join(f"{n:>11,}" for n in counts)
            + f"  {sum(counts):>9,}"
        )
    sample = prepared.sample
    lines += [
        "",
        f"frozen sample: {sample.size} of {sample.requested_size} requested, seed {sample.seed}, "
        f"sample_sha256 {sample.sample_sha256[:16]}...  [sample file: {result.sample_file}]",
        "  strata (year band / jurisdiction / fraud type): population -> sampled",
    ]
    for (band, jurisdiction, fraud), population, sampled in sample.strata:
        lines.append(f"    {band:<8} {jurisdiction:<8} {fraud:<24} {population:>5,} -> {sampled}")
    return "\n".join(lines)

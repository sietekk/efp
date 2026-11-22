# Build 06: Link resolution

Spec: section 4 (resolve_links component and its contract), section 6 (Heritage shorteners), section 5 (Wayback CDX, Common Crawl CDX), section 1 (F2). Task list: D3. Needs: 04, 05. Branch: `build/06-resolve-links`. Report: `reports/06-resolve-links.md`.

Read first: `CLAUDE.md`, `build/README.md`, `docs/spec.md` section 4 ("resolve_links" row and contract) and section 6 "Heritage shorteners", the `links` table from `reports/02-schema.md`, `docs/plan.md` "What the first pass hands back" (link survival is the first of the nine numbers), `reports/05-capture.md`.

Stay in plan mode until I approve. Propose the URL parser for the Source cell, the resolution ladder, the classification rules and the domain report. Wait.

## What to build

1. **Parse the Source cell.** `rows.source_raw` holds zero or more URLs in free text (the April 2026 export has 1,322 rows with at least one). Extract every URL (including bare `herit.ag/...` and `bit.ly/...` forms without a scheme), de-duplicate per row, and write one `links` row per URL with `url_raw`. Rows with no URL get a `links` row with `status = 'unresolved'` and `expansion_method = 'unresolved'` and a note, so the count of link-less rows (expected 159) is in the data.
2. **Resolution ladder, per URL, through `ctx.capture` only** (registered as modules `resolve.live`, `resolve.bitly_info`, `resolve.wayback_short`, `resolve.wayback_target`, `resolve.commoncrawl` under `[stages.resolve_links]`, policy `first_hit`; a module that cannot run, such as Common Crawl being down, is skipped with the reason and the ladder continues):
   1. Live: follow redirects (capped at 10, 10-second timeout, project User-Agent); record the final URL and capture the final body. Method `live`.
   2. If live fails or returns a dead page (4xx/5xx, a parked domain, a shortener error page): query the Wayback CDX for the **shortener URL itself** (the Internet Archive frequently captured the redirect); if a 3xx capture exists, take its `Location` as the expansion. Method `wayback_short`.
   3. Then the CDX for the target URL (the expansion, if known) and fetch the closest capture to the row's year. Method `wayback_target`.
   4. Then Common Crawl CDX as a second chance for the target.
   5. Bitly's `+` info page only when live resolution of a bit.ly link fails and before the Wayback step for it.
   6. Otherwise `unresolved`.
3. **Classification.** `links.status`: `live` (final page fetched live with 200 and a non-trivial body), `archived` (only an archived copy exists), `dead` (expansion known, no live or archived body), `unresolved` (expansion unknown). Every fetched body is captured regardless of usefulness; relevance is decided downstream.
4. **Text.** For each captured HTML body, extract main text with `trafilatura` and the publication date with `htmldate`, store the text as its own capture (`tool = 'trafilatura'`, with the parent `capture_id` in `headers_json`) so later stages read text without re-parsing. PDFs are left to build 10.
5. **Stage semantics.** `run` marks the row `done` when every URL has a terminal status; `failed` only on an internal error. `input_hash` is the hash of `source_raw`. Rows with no URL are `done` with `summary_json.no_source = true`.
6. **The domain table.** `efp status --links` and `reports/first-pass/domains.md`: source domains (of the expanded URL) ranked by row count with live / archived / dead counts, and the shortener share. The plan uses this table to set the adapter order for builds 08 and 09.
7. **Link survival number.** Of rows with a source URL: live, archived only, dead, unresolved, as counts and percentages, overall and by year band. Expected link rot is 20–40%; report the measured figure.

## Tests

- URL extraction from messy Source cells (multiple URLs, trailing punctuation, missing scheme, `herit.ag` and `bit.ly` forms).
- Each ladder step with cassettes: live 200; live redirect chain; shortener 3xx only in CDX; target in CDX; nothing anywhere.
- Classification table; no-URL rows; idempotence (second run is a no-op); a `--reset` run re-reads captures and does not re-fetch.
- Domain report sums match `links` counts.

## Done when

- `uv run efp run resolve_links --sample` then `--dry-run` for the full set show the numbers; the full run completes (detached if needed) with every row with a source URL holding a capture or a logged archive attempt, and every capture holding a hash and timestamp.
- `reports/first-pass/domains.md` exists and the report quotes link survival overall and by year band.

## Guardrails that bite here

All fetches through the capture layer; budgets apply per domain, including `web.archive.org`.

Finish with `reports/06-resolve-links.md` and a pull request. Do not merge. Review checkpoint: the domain table and link survival.

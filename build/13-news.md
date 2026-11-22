# Build 13: News retrieval

Spec: section 4 (news component), section 5 (GDELT DOC 2.0, Media Cloud v4, Wayback CDX), section 1 (F5), section 6 (licensed sources are reached only through the manual import path). Task list: D7. Needs: 06, 12. Branch: `build/13-news`. Report: `reports/13-news.md`.

**Google Programmable Search is withdrawn.** Since January 20, 2026 a new engine cannot search the whole web (50 domains at most), and Google's Custom Search JSON API ends on January 1, 2027. In its place the optional discovery module is the Brave Search API (official API, `site:` operators, `freshness` date ranges such as `2009-01-01to2011-12-31`, 20 results a page, 10 pages a query, prepaid with $5 of credit a month). In this build replace the `news.google_cse` module with `news.brave`, the `first_pass.google_search_enabled` key with `brave_search_enabled`, `search.google_cse_daily_cap` with `search.brave_monthly_cap = 1000`, and the `GOOGLE_CSE_KEY`/`GOOGLE_CSE_ID` variables with `BRAVE_SEARCH_KEY`, updating `efp/config.py`, `config.example.toml`, `config.toml`, `.env.example`, `BUILD.txt` and `RUNBOOK.md` in the same PR, and remove every remaining Google Programmable Search reference under `build/` and `docs/plan.md`'s allowlist row. The module stays off by default; try it on the frozen sample first and report what it finds for pre-2017 rows before anyone relies on it.

Read first: `CLAUDE.md`, `build/README.md`, `docs/spec.md` section 4 "news" row and section 5 rows for GDELT, Media Cloud and Wayback CDX, the `articles` table from `reports/02-schema.md`, `docs/plan.md` allowlist rows for GDELT and Media Cloud and the "Excluded" paragraph (NewsBank, Newspapers.com, ProQuest, Westlaw), `reports/06-resolve-links.md` (the domain table), `reports/12-identity.md` (the gate).

Stay in plan mode until I approve. Propose the query builder, the source order, the per-era strategy, the article capture and extraction path, and the NewsBank queue format. Wait.

## What to build

1. **Gate and scope.** The stage runs only for rows whose identity outcome is `confirmed` or `needs_review` (the news gate from spec section 2); unresolved rows are `skipped` with `gate_reason = identity_unresolved`. Predecessor: `identity`.
2. **Query builder.** From the row and its identifiers: name variants (full, surname plus given, aliases, nickname forms from build 12's table), the state and county or city, charge terms by fraud type (absentee ballot, false registration, felon voting, duplicate voting, vote buying, petition fraud, non-citizen voting, impersonation), and a date window from the case year −1 to +3. Deterministic and unit-tested; the same query strings are what the manual queue hands me.
3. **Sources as modules** (`news.gdelt`, `news.mediacloud`, `news.wayback_outlets`, `news.brave` under `[stages.news]`, policy `all`; a source with no key, no coverage for the row's era, or a cap reached is skipped with the reason, and the row completes on whatever the others found). In default order, each through the capture layer with its own budget:
   - **GDELT DOC 2.0** (`api.gdeltproject.org/api/v2/doc/doc`, no key, undocumented limit; budget one request per 5 seconds; coverage 2017-01-01 onward; 250 results max per query) via `gdeltdoc`. Only for rows whose window reaches 2017 or later.
   - **Media Cloud v4** (`MEDIACLOUD_KEY`; metadata and URLs only; coverage before 2017 is partial) for all rows; the hit rate by year is a number in the report.
   - **Wayback CDX by outlet.** For the local and state outlets in build 06's domain table that serve the row's state, query the CDX for the outlet's domain in the date window with the name terms, and fetch matching archived pages. This is the main pre-2017 path on the open web.
   - **Brave Search API**, only if `first_pass.brave_search_enabled` and `BRAVE_SEARCH_KEY` exist, capped at `search.brave_monthly_cap`, for pre-2017 discovery: `site:` restricted to the row's state outlets where the domain table has them, `freshness` set to the row's date window; log every query and the result URLs in `query_json`; fetch result pages through the capture layer like any other URL.
   Article URLs from any source are fetched (live, then Wayback) through `ctx.capture`, text extracted with `trafilatura` and the date with `htmldate`, and written to `articles` (`outlet`, `published_date`, `date_method`, `extractor` and version, `source_kind = 'automated'`, `query_json`).
4. **Relevance, minimal and recorded.** An article is linked to a row when the captured text contains a name variant and either a charge term or the row's state and year; weaker matches are stored as captures but not linked (`summary_json` counts them). Relevance for coding is decided by extraction (build 16), not here.
5. **The manual queue.** `queues/newsbank.csv`: `row_id`, name, state, county, year, the suggested search string (NewsBank syntax: quoted name variants, place, charge terms, date range), and why the row is queued (pre-2017 with no linked article; or linked articles only from national outlets). Newspapers.com and Westlaw get the same columns in `queues/licensed_other.csv` for rows where the primary record is missing and the case predates online coverage.
6. **Numbers.** News reach by era (rows with at least one linked article for 2000–09, 2010–16, 2017–25), hit rate by source and by year, articles per row distribution, and the size of the NewsBank queue. The plan expects pre-2017 to be thin (994 rows, 67%, are pre-2017); that is a finding to report, not a failure.

## Tests

- Query builder determinism and coverage of name variants and charge terms per fraud type.
- Cassettes for GDELT (including an empty result and the 250 cap), Media Cloud, a CDX outlet query; budget assertions.
- Relevance linking rules on synthetic texts; date extraction fallbacks; `source_kind` always `automated` here.
- Gate behavior for each identity outcome.

## Done when

- `uv run efp run news --sample` then the full run (detached) give every gated-in row a logged attempt per source; `efp status` reports news reach by era and hit rate by source and year.
- `queues/newsbank.csv` exists with a specific search string per row; I can take a line from it into the library database as is.

## Guardrails that bite here

Nothing scraped from NewsBank, Newspapers.com, ProQuest, Westlaw or any site whose terms forbid it; those rows go to the queue. Every fetch through the capture layer with per-domain budgets. Brave search, if enabled, stays under the monthly cap and never queries a private individual's name together with terms unrelated to the case.

Finish with `reports/13-news.md` and a pull request. Do not merge.

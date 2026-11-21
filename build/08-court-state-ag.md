# Build 08: State AG and DA press-release harvester

Spec: section 6 (state AG and DA press-release harvester, the `AGScraper` protocol), section 4 (court sub-strategy 2), section 11 (contract tests on cassettes). Task list: D4. Needs: 07. Branch: `build/08-court-state-ag`. Report: `reports/08-court-state-ag.md`.

Read first: `CLAUDE.md`, `build/README.md`, `docs/spec.md` section 6 "State AG and DA press-release harvester", the `state_adapters` table from `reports/02-schema.md`, `reports/06-resolve-links.md` (the domain table), `reports/07-court-federal.md` (the sub-strategy interface).

Stay in plan mode until I approve. Propose the harvester framework (modeled on biglocalnews/warn-scraper: one module per state, shared cache and retry, a registry), the ten state modules and what each site offers (search, archive, RSS, or nothing), and the contract-test plan. Wait.

## What to build

1. **Framework.** `efp/court/adapters/ag/__init__.py` with the registry and the protocol from the spec, verbatim; the whole harvester is the `court.state_ag` module in the court chain, and inside it each state module is swappable: a state with no module, or whose module is `broken` or `manual`, is skipped with the reason and the court chain continues to the next module.

   ```python
   class AGScraper(Protocol):
       state: str
       def search(self, name: str, year: int, window: int = 2) -> list[Release]: ...
       def fetch(self, release: Release) -> Capture: ...
   @dataclass(frozen=True)
   class Release:
       url: str; title: str; date: date | None; office: str  # 'AG' | 'DA:<county>'
       snippet: str
   ```

   `fetch` goes through `ctx.capture`. The framework handles robots.txt (via the capture layer), per-site rate budgets, pagination, retry, and a per-state cache of release indexes so a state's index is fetched once per run, not once per row. Every module declares how it searches: `search_form`, `archive_listing`, `rss`, `sitemap`, or `none`.
2. **Ten state modules first, by row count:** MN (138 rows), TX (113), FL (89), NC (82), CA (70), WI (68), OH (63), KY (62), IN (59), IL (54). For each: the state attorney general's press-release index and, where the state prosecutes election cases locally, the district or county attorneys that appear in the domain table (Texas and California especially). A state whose AG site has no usable search or archive is recorded in `state_adapters` as `mode = 'manual'` with the reason, not worked around; where the live index is gone, the Wayback copy of the release index is the fallback.
3. **Matching.** A release is a hit when it names the subject (surname and given name or a known alias, allowing the common press-release forms) and an election charge, within the year window. Hits are captured, written as `court_records` with `source_kind` `state_ag_release` or `da_release`, and run through the same identifier extraction as build 07.
4. **Coverage matrix.** `state_adapters` rows for every state touched (`kind = 'ag'`, `portal`, `adapter_module`, `mode`, `reason`, `last_verified`), and `efp status --coverage` rendering `docs/coverage_matrix.md`: per state, AG module present and mode, rows in the state, rows with a located record by strategy. The remaining forty states are listed as `none` with their row counts so the order of future work is visible.
5. **Contract tests.** Every module ships a recorded cassette (`tests/cassettes/ag/<state>/...`) for its index page and one release, plus a live smoke test marked `live` that `efp selftest --live` runs; a module whose live smoke fails is reported as `broken` in the coverage matrix, not silently skipped. Cassettes must not contain real subjects' names from the dataset: record against releases unrelated to the dataset or redact.

## Tests

- Framework: registry, cache, pagination and retry with cassettes; robots disallow yields a `manual` record with reason.
- Each state module against its cassette: index parsing, release parsing, date handling, the matching rule on synthetic names.
- Coverage matrix rendering sums to the dataset's state counts.

## Done when

- `uv run efp run court --sample` and the full run (detached) record an attempt for every in-scope row in the ten states; `efp status` shows located-record rates by state and strategy.
- `docs/coverage_matrix.md` is generated and lists the ten states' modes with reasons for every `manual`.
- All ten cassettes pass in CI; the live smoke results are in the report with the date.

## Guardrails that bite here

Public web, robots.txt honored, budgets per site (default 1 request per 2 seconds; many state sites have no published limit). No CAPTCHA solving. A site that blocks automated access or forbids it in its terms is `manual` with the reason, listed in the report as a gated source with its row count.

Finish with `reports/08-court-state-ag.md` and a pull request. Do not merge.

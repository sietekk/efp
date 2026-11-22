# Build 06 — Link resolution

Branch: `build/06-resolve-links` (off `main` at b42ee11; build 05 is merged, nothing stacked) · PR: see the pull request for this branch · Date: 2026-11-21 · Model: claude-sonnet-5-5 · Prompt: `build/06-resolve-links.md` · Spec sections: 4 (resolve_links), 6 (Heritage shorteners), 5 (CDX), 1 (F2)

## What this build set out to do

Resolve every URL in `rows.source_raw`: parse the Source cell, expand shorteners and redirects, fall back to the Wayback Machine and Common Crawl, classify each link `live` / `archived` / `dead` / `unresolved`, capture every body, extract text, and produce the source-domain table and the link-survival number (the first of the plan's nine numbers).

Changes the approved proposal made to the prompt:

- **Migration 0007** rebuilds `links` (it is not a protected table): `expansion_method` also allows `bitly_info` and `commoncrawl`; new `note` column; `UNIQUE (row_id, url_raw)`. A row with no URL gets one `links` row: `url_raw` = the Source cell's text (`''` if empty), `status`/`expansion_method` = `unresolved`, `note` = `no_url`.
- **Both non-allowlist sources are on.** `first_pass.commoncrawl_enabled` and `first_pass.bitly_info_enabled` default to `true` (the operator enabled them; neither is on `plan.md`'s first-pass allowlist). Setting either to `false` makes its module report why it is skipped.
- **Population:** the 1,481 in-window rows. Exact-duplicate rows are not merged; the domain table adds a "distinct rows" column.
- **"Logged archive attempt":** there is no attempt table. Every CDX and archive call is a `captures` row (`row_id`, `stage = 'resolve_links'`); per-link module outcomes are in `case_stage.summary_json`; a call that returned no bytes leaves its reason in `links.note`. A test asserts no link is left without a capture or a note.

## What was built

- `efp/resolve_links/` — `parse.py` (URL extraction), `classify.py` (is it a real page), `text.py` (trafilatura and htmldate), `modules.py` (the five ladder modules and Bitly's `+` page reader), `__init__.py` (the stage component).
- `efp/linkreport.py` — link survival and the domain table; `efp status --links` and `efp links report` (writes `reports/first-pass/domains.md`).
- `efp/wayback.py` — `statuscode` filter (archived redirects), `follow=False` fetches, Common Crawl payload decoding, one `collinfo.json` fetch per client; `efp/capture.py` — `fetch_archived(follow=...)`, and an API call redirected off its own host now has that host's `robots.txt` checked.
- Config: the two flags above in `config.toml`, `config.example.toml` and the test fixture. Migration `0007_links_resolution.sql`; migration tests no longer depend on the count and gain a data-preservation test.
- Tests: `test_resolve_links.py` (every ladder step, classification, no-URL rows, idempotence, reset), `test_resolve_links_parse.py`, `test_linkreport.py`, plus additions to `test_capture.py`, `test_wayback.py`, `test_ratelimit.py`.
- Results: `uv run pytest -m "not live"`: 678 passed, 1 deselected. `ruff check`, `ruff format --check`, `mypy efp`: clean. Coverage 93% overall (`classify.py` 98%, `linkreport.py` 99%, `modules.py` 89%).

How the ladder works (full detail in `efp/resolve_links/modules.py`): live (redirects hop by hop, each hop checked against its own `robots.txt` and budget) → Bitly `+` page (bit.ly / herit.ag only, only when live gave no expansion) → Wayback CDX for the shortener itself, taking an archived 3xx's `Location` → Wayback CDX for the target, nearest the row's year → Common Crawl. When the archive reveals where a shortener pointed, that address is tried live before any archived copy. `expansion_method` records how the destination was learned; `status` records how a body was obtained. For a URL that is not a shortener the "expansion" is the URL itself.

## What was run

| Run | Rows attempted | Rows resolved | Wall clock | Requests (per domain) | API spend |
| --- | --- | --- | --- | --- | --- |
| 50-row sample | 0 | 0 | n/a | none | $0 |
| All rows | 0 | 0 | n/a | none | $0 |

Nothing was fetched from the real network and `~/efp-data` was not touched (this session has neither). **The link-survival number and the domain table are therefore not measured yet**; they come from the run below.

To produce them:

```bash
uv run efp run resolve_links --sample          # 50 rows first
uv run efp run resolve_links --dry-run         # the full set's eligibility
nohup uv run efp run resolve_links > ~/efp-data/logs/resolve_links.log &   # roughly 2,400 links, hours
uv run efp status --links                      # survival + domains
uv run efp links report                        # writes reports/first-pass/domains.md
```

The expected runtime is hours: one request per two seconds per domain (herit.ag and bit.ly each carry hundreds of links) and 0.32 requests per second on the Wayback Machine for the ones that need it.

## Results by source

No live requests. `efp modules --stage resolve_links` lists the five modules; each reports its own availability.

## Failures by reason

None in the build's own runs. Independent review (`done-when-reviewer`) found issues that would have biased the survival number; all are fixed and tested:

| Finding | Severity | Fix |
| --- | --- | --- |
| A dead page that redirects to a homepage (or a shortener to its own error page) became the "expansion", so the archive was asked about the homepage and the link came out `archived` | High | A page that is not usable never replaces the URL as the archive target; a shortener's destination is adopted only if it names a deep page on a non-shortener host. A shortener that lands on a shortener's own site is no expansion at all |
| Real articles classified as parked or error pages (a quoted "account suspended") | Medium | Error and parked phrases are trusted in the title, or in a body shorter than 1,500 characters |
| Short real pages classified dead | Medium | Minimum 200 characters of text (was 512), falling back to the visible text when the extractor finds none; a title like "Page Not Found" marks a 200 as an error page |
| An archived http→https redirect accepted as the expansion | Medium | Redirects that stay on a shortener are skipped; the next capture is tried |
| Nearest-to-row-year picked only from the 20 newest captures | Medium | The CDX is asked in a window around the row's year first, then without one |
| Links blocked by robots.txt counted as rot | Medium | The report separates them ("unreachable by policy: policy, not rot"); they still count as not live |
| Unicode closing punctuation, a bare shortener name ("via bit.ly"), `|`-joined and glued URLs | Low | Parser fixes with tests |
| A row with one dead and one pending link counted as dead | Low | It is pending until every link is processed |
| `kind="api"` redirects were followed with no robots check | Low | A redirect off the API's own host gets that host's `robots.txt` checked |
| `collinfo.json` re-fetched for every Common Crawl lookup | Low | Fetched once per client |

## Gated sources encountered (not worked around)

None new. Common Crawl and Bitly's `+` page are not on the plan's allowlist; they run because the operator enabled them (decision 1).

## Numbers for the first-pass report

Link survival (the first of the nine numbers): of the rows with a source URL, live / archived only / dead / unresolved, overall and by year band (2000-09, 2010-16, 2017-25), at row level (a row counts at its best link) with link-level counts beside it, plus the measured rot against the spec's 20-40%. The denominator is the rows with a URL (the plan expects 1,322 of the 1,481 in-window rows; 159 have none). `efp status --links` prints it; `reports/first-pass/domains.md` is the file. **Not yet measured.**

## Spot-check guide for the operator

- `uv run efp review resolve_links --sample 10` is not built yet; instead read `SELECT row_id, url_raw, url_expanded, expansion_method, status, note FROM links WHERE row_id IN (<sample ids>)` and open a few captures.
- Look first at links whose note says "not usable" (a live page rejected) and at `archived` links with `expansion_method = 'live'`, which should be rare.
- The domain table's `(unexpanded) herit.ag` row is the shortener links nobody could expand; its size is the part of the evidence base that is lost.
- Coverage check, should return 0: `SELECT count(DISTINCT l.row_id) FROM links l WHERE coalesce(l.note,'')<>'no_url' AND NOT EXISTS (SELECT 1 FROM captures c WHERE c.row_id=l.row_id AND c.stage='resolve_links');` (a link whose every request failed outright has only a note).

## Decisions needed from the operator

1. **Common Crawl and Bitly `+` are on** (your instruction). Flip `first_pass.commoncrawl_enabled` / `bitly_info_enabled` to `false` to turn either off; a later `--recheck` revisits only rows whose available-module set changed.
2. **Bitly's `+` page markup could not be observed from here.** `long_url_from_info_page` accepts only labelled or structured forms (a `long_url` field, a data attribute, an `og:url`, a "Long URL" label followed by a link) and otherwise abstains ("names no destination"). Its tests use synthetic markup. If the real page differs, that module will simply never help; check a few `bitly_info` results (or their absence) in the sample run.
3. **Classification thresholds** (`classify.py`): 200 characters of main text for an HTML page, 512 bytes for other bodies, error phrases trusted in titles and in bodies under 1,500 characters, a deep URL that lands on the site root is a soft 404. These are judgment calls; the sample run is the place to check them.
4. **`expansion_method = 'live'` for a plain URL that was never reached.** For a URL that is not a shortener the expansion is the URL itself, so a dead plain URL records `live` (or `wayback_target` if an archived copy was found). `status` is the column that says whether anything was found.
5. **`live` accepts any 2xx** (the prompt says 200).
6. **Cookie-setting pages are re-fetched on re-runs** (build 05 never caches them). Approved.
7. **`efp links report`** writes `reports/first-pass/domains.md` (the prompt's file); `efp status --links` prints the same tables.
8. **One build 05 test was loosened.** `tests/test_ratelimit.py::test_two_processes_share_one_budget` failed intermittently on a 0.12 s minimum-gap check (a 0.115 s gap, which is timestamp jitter between processes, not a rate violation). It now asserts that ten requests at one per 200 ms take at least 1.5 s in total (two processes with separate budgets would take about 0.8 s), which is immune to that jitter, plus a 0.04 s floor on any gap.
9. **`wayback` and `cdx-toolkit` are still not used** (build 05 decision 4); this build's CDX, availability and Common Crawl access is the build 05 client.

## Done-when check

- *"`uv run efp run resolve_links --sample` then `--dry-run` for the full set show the numbers; the full run completes (detached if needed) with every row with a source URL holding a capture or a logged archive attempt, and every capture holding a hash and timestamp."* Holds on synthetic data end to end: `test_a_second_run_fetches_nothing_and_a_reset_run_does_not_refetch_resolved_links` drives the real component through the runner, and `test_every_link_has_a_capture_or_a_logged_reason` checks the capture-or-reason rule. A hash and timestamp on every capture are guaranteed by the schema (`content_sha256` and `retrieved_at` are `NOT NULL`). **Not run on the real data;** commands above.
- *"`reports/first-pass/domains.md` exists and the report quotes link survival overall and by year band."* **Does not hold yet.** The file is produced by `uv run efp links report` after the real run, and the measured figures belong in this report then. The rendering and the arithmetic (row-level best status, pending rows, year bands, duplicates, shortener share, robots-blocked split) are tested: `tests/test_linkreport.py`.

Review checkpoint (yours): the domain table and link survival, once the run has produced them.

## Operator decisions

Filled in by the operator at merge time. Pre-filled only where a later report explicitly records the decision (quoted, with its source); every other item is blank.

1. Decision: ______ · Date: ______
2. Decision: ______ · Date: ______
3. Decision: ______ · Date: ______
4. Decision: ______ · Date: ______
5. Decision: ______ · Date: ______
6. Decision: ______ · Date: ______
7. Decision: ______ · Date: ______
8. Decision: ______ · Date: ______
9. Decision: ______ · Date: ______

## Carried forward

- 13, 20: the page-classification thresholds in `efp/resolve_links/classify.py` are judgment calls (200 characters of text for an HTML page, 512 bytes otherwise, error phrases trusted in titles and in bodies under 1,500 characters, a deep URL that lands on the site root is a soft 404); the sample run is where to check them (decision 3).
- 13, 20: `first_pass.commoncrawl_enabled` and `first_pass.bitly_info_enabled` default to `true` although neither is on `plan.md`'s first-pass allowlist (the operator enabled them); the compliance review lists them, and flipping either to `false` makes its module report why it is skipped (decision 1). Bitly's `+` page markup could not be observed, so `bitly_info` may never help (decision 2).
- 08, 09: the link-survival number and `reports/first-pass/domains.md` (the source-domain table) are not produced until `efp run resolve_links` and `efp links report` run on the real data; nothing was fetched in this build.
- 19, 21: link survival is one of the nine numbers (overall and by year band, row-level best status with link-level counts beside it); it is not yet measured ("Numbers for the first-pass report").
- 10, 13, 16: read links through `links.status` (`live`, `archived`, `dead`, `unresolved`) and `links.capture_id`; `expansion_method` records how the destination was learned and `status` how a body was obtained; a row with no URL has one `links` row with `note = 'no_url'` (decision 4, migration 0007).

# Build 05 — Capture layer and provenance

Branch: `build/05-capture` (off `main` at 60d5863; build 04 is merged, nothing stacked) · PR: see the pull request for this branch · Date: 2026-11-21 · Model: claude-sonnet-5-5 · Prompt: `build/05-capture.md` · Spec sections: 4 (capture), 7, 10, 5, 1 (N1, N2, N4)

## What this build set out to do

One client for every network call (budget, robots, cache, retry, WARC, content-addressed store, one `captures` row), Wayback/Common Crawl access, a Save Page Now queue, Perma.cc for cited URLs, a weekly hash manifest with RFC 3161 tokens from two TSAs, a BagIt fixity bag, a pinned-instruments registry, and the archive module chain. Full design and layout: `docs/capture.md`.

Changes the approved proposal made to the prompt:

- **Schema.** `captures` was fully append-only, so `wayback_url` and `tsa_token_path` (which cannot exist at insert time) could never be filled. Migration `0006_capture_support.sql` replaces the `captures_no_update` trigger so exactly those two columns may go from NULL to a value, once, with every other column immutable, and adds `spn_queue`, `manifests`, `manifest_tokens`, `manifest_captures`.
- **Third-party clients not used.** `savepagenow`, `wayback` and `cdx-toolkit` make their own HTTP calls and would bypass the budget, cache, WARC and secret stripping. SPN2, CDX, Availability, Common Crawl and Perma are called through `Capture.fetch`. The packages stay in `pyproject.toml` (they are in the spec's manifest) but an import scan forbids them.
- **WARC.** Records are reconstructed from the prepared request and the response (not wire-exact), so secrets can be stripped. Files are `paths.warc/YYYY/MM/YYYY-MM-DD.warc.gz` (default `<store>/warc/`).
- **Rates at 80% of the published figure:** Wayback 0.32/s (the config had 0.5, above the published 0.4), SPN 4/min (the config had 5; 80% of ~6 is 4.8).
- **Robots disallow** stores the `robots.txt` body that forbade the fetch as the capture body (the table requires a hash and body).

## What was built

- `efp/capture.py` — `Capture.fetch`, `store_bytes`, `read_body`, `enqueue_spn`, and thin `cdx` / `availability` / `fetch_archived` / `perma` methods; `LazyCapture` (`ctx.capture`, built only when first used). Redirects are followed one hop at a time, so every hop gets its own robots check and budget and credentials are not sent to a different host.
- `efp/ratelimit.py` — pyrate-limiter 4.5 SQLite buckets, file-locked (shared by every process), evenly spaced, daily caps, named budgets (`spn`).
- `efp/warcstore.py`, `efp/wayback.py`, `efp/spn.py`, `efp/perma.py`, `efp/manifest.py` (manifest, TSA tokens, verification, bag), `efp/instruments.py`, `efp/builtin_modules.py` (`archive.spn`, `archive.perma`, `archive.autoarchiver`), `efp/livecheck.py`.
- CLI: `efp manifest [--timestamp|--verify|--bag|--week|--manifest]`, `efp spn drain|status`, `efp selftest --live`, capture section in `efp selftest`.
- Config (all documented in `config.example.toml`): `paths.cache`, `paths.warc`, `capture.cache_expire_hours`, `capture.max_wait_seconds`, `capture.spn_per_minute` 5→4, `rate_limits."web.archive.org"` 0.5→0.32, `rate_limits."archive.org"`. New core dependency `filelock` (pyrate-limiter's cross-process lock; it was only present through an optional extra).
- Docs: `docs/capture.md`, `reference/tsa/README.md`, RUNBOOK, migrations README, tool register (`savepagenow`, `responses`, `vcrpy`, `filelock` rows, licenses read from installed metadata).
- Tests: `test_capture.py`, `test_ratelimit.py`, `test_wayback.py`, `test_spn_perma.py`, `test_manifest.py`, `test_capture_surface.py`, `test_live.py` (marked `live`), helpers `capturehelpers.py`, `fakets.py`, `limiter_worker.py`. Existing tests adjusted: migration tests no longer hard-code a migration count; the static DB guard allows the one reviewed trigger replacement and the two named `UPDATE captures` statements; `conftest.py` isolates the process-global log-redaction registry per test.
- Results: `uv run pytest -m "not live"`: 598 passed, 1 deselected (the live test). `ruff check`, `ruff format --check`, `mypy efp`: clean. Coverage 94% overall (`ratelimit.py` 100%, `capture.py` 96%).

## What was run

| Run | Rows attempted | Rows resolved | Wall clock | Requests (per domain) | API spend |
| --- | --- | --- | --- | --- | --- |
| 50-row sample | 0 | 0 | n/a | none | $0 |
| All rows | 0 | 0 | n/a | none | $0 |

Nothing was fetched from the real network and `~/efp-data` was not touched. Everything ran against `responses` mocks, synthetic databases, two real worker processes (budget sharing, concurrent claiming from build 04), and a hand-built RFC 3161 response with a placeholder signature. **The live Done-when items are yours to run** (see "Done-when check").

## Results by source

No live requests. Modules registered: `archive.spn`, `archive.perma`, `archive.autoarchiver`; `efp modules --stage archive` lists each with its reason for being unavailable (no IA keys, no Perma key / registrar affiliation, Docker and build 20).

## Failures by reason

None in the build's own runs. Independent review (`done-when-reviewer`) found 13 issues; all are fixed and tested:

| Finding | Severity | Fix |
| --- | --- | --- |
| A redirect from an allowed page into a disallowed path was fetched | High | Redirects followed hop by hop; each hop checks its host's robots.txt and budget; a block records the redirect chain |
| The bag stopped validating after any fetch (hard links to a WARC that keeps growing) | High | The open day's WARC is left out of the bag until its UTC day is over; an update validates the existing bag first and refuses to bless a changed body |
| `store_bytes` kept credentials in headers and URLs | Medium | Same redaction as `fetch`, including API parameter names |
| robots.txt rules were not checked against query parameters | Medium | The URL checked includes `params` |
| A Save Page Now `200` carrying `too-many-*` counted as a failure | Medium | Throttling (429 or too-many text) is deferral on submit and on poll; no attempt counted |
| 429 on robots.txt allowed the whole site | Low | Treated like 5xx: disallow until readable |
| Poll errors reset attempts; submitted jobs never timed out | Low | Stored attempts kept; a job unfinished after 24 h returns to the queue |
| A TSA token was stored without checking its imprint | Low | The imprint must equal the manifest hash before the token is stored |
| A capture could land in two manifests | Low | `UNIQUE(capture_id)` and one `BEGIN IMMEDIATE` transaction; a failed build removes its file |
| Error text could carry an unregistered `key=` value | Low | `scrub_text` on error and log text; API-mode URL redaction |
| The HTTP cache stored `Set-Cookie` (header, cookie jar and raw response) | Low | Responses that set a cookie are never cached (still captured, cookie redacted) |
| The WARC held only the last hop of a redirect | Low | One request/response pair per hop |
| Unused packages that bypass the layer could still be imported | Low | Added to the import scan |

Also from review: the prompt's `Capture.cdx/availability/fetch_archived/perma` now exist as methods, and every capture is logged (`efp.capture`, credentials scrubbed). The reviewer confirmed no writes to `rows` or the source columns, only the two permitted `UPDATE captures` statements, and no banned dependencies.

## Gated sources encountered (not worked around)

| Source | What it wants | Rows affected | Where logged |
| --- | --- | --- | --- |
| Perma.cc | Free tier needs a registrar affiliation (a person/institution) | cited URLs only (build 19) | `efp selftest` and `efp modules` ("PERMA_API_KEY absent ... gated") |
| Save Page Now | Self-serve IA S3 keys (yours to create); without them the queue accumulates | all successful pages | `efp selftest`, `efp spn status` |

## Numbers for the first-pass report

None yet. This build supplies the provenance numbers later builds will cite: captures per stage and tool, cache-hit share (`tool = 'requests-cache'`), robots disallows (`tool = 'robots_disallow'`), SPN queue depth, and manifest/token coverage.

## Spot-check guide for the operator

- `uv run efp selftest` — the capture section: libraries, budgets in force, which of SPN/Perma keys exist, TSA count, trust anchors.
- After any fetch: `sqlite3 ~/efp-data/pipeline.db "select tool, http_status, warc_path from captures order by capture_id desc limit 5"`, then read the WARC record at `warc_path` (the `#offset` is the response record).
- `uv run efp manifest` then `--timestamp`, `--verify`, `--bag`; `uv run efp spn status`.
- Review the schema change in `migrations/0006_capture_support.sql` (the S0 review checkpoint).

## Decisions needed from the operator

1. **`captures` trigger relaxation (migration 0006).** Approved in the proposal; stated here for the record. Default: as built.
2. **TSA trust anchors are not pinned.** I could not download a TSA's root certificate in this session. `efp manifest --verify` therefore checks that each token was granted and that its message imprint equals the manifest's SHA-256, and says "signature NOT checked". To verify the signature chain, put the TSAs' root PEMs in `reference/tsa/` (see its README). Until then "verify accepts the tokens" means imprint and status only.
3. **Allowlist.** Common Crawl, Perma.cc and the RFC 3161 TSAs are in spec section 5 but not in `plan.md`'s first-pass allowlist. The interfaces are built; they are used only on explicit commands (`efp manifest --timestamp` contacts the TSAs; nothing queries Common Crawl until a later build). Please confirm the TSAs are acceptable; otherwise leave `--timestamp` unused.
4. **`savepagenow`, `wayback`, `cdx-toolkit` stay in `pyproject.toml` but are unused** (and import-scanned). Remove them if you prefer.
5. **WARC fidelity and location** (reconstructed records; `<store>/warc/`). Because the open day's WARC is left out of the bag, the bag lags by up to a day.
6. **Responses that set a cookie are not cached,** so such pages are fetched again on a re-run.
7. **Robots handling:** 401/403/429/5xx on `robots.txt` disallows the site until it can be read; 404/410 allows. Disallow records store the rules. Documented API endpoints (CDX, SPN, TSA, Perma) skip the robots check.
8. **Budgets:** Wayback 0.32/s and SPN 4/min (80% of published, rounded down); the stricter 1 per 2 s default is kept (CLAUDE.md) over the plan's 1 per second.
9. **`filelock` added as a core dependency** (needed for cross-process budgets).
10. **Small edges:** `spn_queue.next_attempt_at` doubles as the submit time for `submitted` rows; one capture row per fetch (hops are in the WARC and `headers_json.history`); a connection failure writes no `captures` row (no bytes came back) and raises `CaptureError`; `tsa_token_path` records the first TSA's token (all tokens are in `manifest_tokens`).

## Done-when check

- *"`uv run efp selftest` reports the capture layer healthy, which secrets it has (SPN, Perma) and the budgets in force."* Holds in `test_selftest_reports_the_capture_layer_budgets_and_which_keys_exist` (names only, never values). **Please run `uv run efp selftest` against your real config.**
- *"A fetch of a known public page through the client (live, run by me, not in CI) produces a captures row, a WARC record, a stored body whose hash matches, and a queued SPN submission."* Holds on mocks end to end (`test_a_fetch_produces_a_captures_row_a_warc_pair_and_a_matching_body`, `test_successful_pages_are_queued_once_and_apis_and_failures_are_not`). **Not run live.** To run it: `uv run efp selftest --live` (a CDX query and a TSA request through the layer), then a stage run or any fetch, then `uv run efp spn status`.
- *"`uv run efp manifest --timestamp` produces a manifest and two tokens that `--verify` accepts."* Holds with a fake TSA (`test_manifest_timestamp_verify_and_bag_through_the_cli`), including tamper detection. **Not run against the real TSAs;** see decision 2 for what `--verify` proves without pinned roots. The signature path itself is untested in CI. The one live test (`tests/test_live.py`, run by `efp selftest --live`) covers a real CDX query and a real RFC 3161 request.

Also verified: no module other than `efp/capture.py` imports an HTTP library (`test_only_the_capture_layer_imports_an_http_library`); two real processes share one budget (`test_two_processes_share_one_budget`); secrets are absent from `headers_json`, URL columns, the WARC and the HTTP cache (`test_secrets_never_reach_headers_urls_the_warc_or_the_http_cache`, `test_a_cookie_the_server_sets_is_not_kept_in_the_cache_or_the_row`).

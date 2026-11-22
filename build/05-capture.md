# Build 05: Capture layer and provenance

Spec: section 4 (capture component), section 7 (provenance and integrity, capture policy, derivation chain), section 10 (rate limiting), section 5 (Wayback CDX and Availability, Save Page Now 2, Perma.cc, RFC 3161 TSAs), section 1 (N1, N2, N4). Task list: D2. Needs: 02, 04. Branch: `build/05-capture`. Report: `reports/05-capture.md`.

Read first: `CLAUDE.md`, `build/README.md`, `docs/spec.md` sections 7 and 10 in full and the `captures` table in section 3, `docs/tool-register.md` entries for warcio, wayback, cdx-toolkit, savepagenow, rfc3161-client, bagit, auto-archiver, Browsertrix, `reports/04-runner.md`.

Stay in plan mode until I approve. Propose the capture client's API, the rate-limiter and cache layout, the WARC writing strategy, the manifest format, and which parts are optional behind missing keys. Wait.

## What to build

1. **One client for every network call.** `efp/capture.py` exposes `Capture.fetch(url, *, row_id, stage, method="GET", headers=None, allow_redirects=True, timeout=10) -> CaptureRecord` and `Capture.store_bytes(data, *, row_id, stage, url_requested, tool, ...)` for bodies that arrive another way (downloads, manual imports). Nothing else in the codebase may import `requests` directly; a test enforces that with an import scan. `ctx.capture` is this client.
2. **What a fetch does, in order:** check the per-domain budget (pyrate-limiter 4.x with its SQLite backend under `paths.data_dir`, one bucket per domain at the `[rate_limits]` figure, default 1 request per 2 seconds, so budgets survive restarts); send the request through a `requests-cache` session (SQLite backend, honoring `Cache-Control`) with the project `User-Agent` from `[project]`; retry with jitter via `stamina` on 429/5xx/connection errors; write the response as a WARC 1.1 record pair (request and response) with `warcio` into a per-day WARC file under `paths.store`; hash the body (SHA-256), write it to `store_path(sha256)` if not already present, and insert one `captures` row (`url_requested`, `url_final`, `retrieved_at` UTC ISO-8601, `http_status`, `content_type`, `content_sha256`, `body_path`, `headers_json`, `warc_path`, `tool`, `tool_version`). A cached response still produces a captures row that says so (`tool = 'requests-cache'`), because "what was fetched when" must be answerable. Capture before judging: relevance is never decided in this layer.
3. **Wayback.** `Capture.cdx(url, from_year=None, to_year=None, match="exact", limit=...)` and `Capture.availability(url, timestamp=None)` using the `wayback` client and `cdx-toolkit`, rate-limited at about 0.4 requests per second; `Capture.fetch_archived(cdx_row)` fetches the archived body through the same path with `tool = 'wayback'` and the original URL recorded as `url_requested`. Common Crawl CDX gets the same interface as a second chance.
4. **Save Page Now 2.** A background queue (a table `spn_queue` or a JSON-lines file under `paths.data_dir`) that submits every resolved URL with `savepagenow` using `IA_ACCESS`/`IA_SECRET`, at most ~6 per minute, records the returned Wayback URL into `captures.wayback_url`, and treats throttling as deferral, never as an error. If the keys are absent the queue accumulates and `selftest` says so.
5. **Perma.cc** behind `PERMA_API_KEY`: `Capture.perma(url)` for URLs cited in the paper, called only by export (build 19). Without a key it is a documented no-op; the registrar affiliation is a gated item and is only noted.
6. **Trusted time.** `efp manifest [--timestamp]`: lists every `captures.content_sha256` added since the last manifest (or in the ISO week), writes `manifests/<date>.txt`, hashes it, and with `--timestamp` requests RFC 3161 tokens from two independent public TSAs (`rfc3161-client`; TSA URLs in `config.toml`), storing the tokens beside the manifest and the path in `captures.tsa_token_path` for the rows the manifest covers. `efp manifest --verify` re-checks a stored token.
7. **Fixity.** `efp manifest --bag` builds or updates a BagIt (RFC 8493) bag over `paths.store` with SHA-256 and SHA-512 manifests (`bagit` 1.9) under `paths.bags`, and validates it; this is what later confirms a backup is complete. RO-Crate and Auto Archiver are build 20; Browsertrix (Docker) is build 09's assisted mode and is not required here.
8. **Pinned instruments.** A small registry `efp/instruments.py` that hashes any file the pipeline depends on (allowlist.json, codebook.yaml, prompt files, Splink settings, WMN data, gold set) and returns the hash for writers to record; builds 12, 15, 16 and 18 call it.
9. **Modules.** Third-party archival is a chain under `[stages.archive]` with policy `all`: `archive.spn` (needs IA keys), `archive.perma` (needs a key; cited URLs only), `archive.autoarchiver` (needs Docker; build 20). An unavailable archiver is skipped with its reason and the local WARC remains the evidentiary copy; nothing about a fetch depends on any archiver succeeding.
10. **Configuration surface.** Everything above reads from `config.toml`: `[rate_limits]` per domain, `[project].user_agent`, timeouts, cache and WARC directories under `paths.data_dir`, TSA URLs, SPN settings. Document each key in `config.example.toml`.

## Tests

- No live network in the suite: `responses` or `vcrpy` cassettes for every path, including redirects, 429 then success, cache hit.
- WARC round trip: a captured response can be read back with `warcio` and matches the stored body hash.
- Store path, deduplication (the same body fetched twice yields one file and two captures rows), and the import-scan test for stray `requests` usage.
- Rate limiter: a burst past the budget blocks for the expected interval (use a tiny budget in the test config).
- Manifest and BagIt: build, verify, detect a modified body.
- One `live` test for a CDX query and one for an RFC 3161 request, run only by `efp selftest --live`.

## Done when

- `uv run efp selftest` reports the capture layer healthy, which secrets it has (SPN, Perma) and the budgets in force.
- A fetch of a known public page through the client (live, run by me, not in CI) produces a captures row, a WARC record, a stored body whose hash matches, and a queued SPN submission.
- `uv run efp manifest --timestamp` produces a manifest and two tokens that `--verify` accepts.

## Guardrails that bite here

Every network call goes through this layer, is rate-limited at 80% of a published limit or 1 request per 2 seconds, and is logged. No browser automation against a site whose terms forbid it. Secrets never enter `captures.headers_json` (strip `Authorization` and any key parameter before storing).

Finish with `reports/05-capture.md` and a pull request. Do not merge.

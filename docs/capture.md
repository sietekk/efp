# The capture layer (build 05)

Everything the pipeline fetches goes through `efp.capture.Capture`. Nothing else may import an HTTP
library (`tests/test_capture_surface.py` scans for it). This page says what is stored and where.

## One fetch, in order

1. **Robots.** For pages (`kind="page"`), `robots.txt` is fetched once per domain per
   `capture.robots_cache_hours` and recorded as its own capture (`tool = 'robots_txt'`). A disallow
   writes a `captures` row with `http_status = NULL`, `tool = 'robots_disallow'`, and the
   `robots.txt` body that forbade it; the page is never requested. A `robots.txt` that cannot be
   read (401, 403, 5xx) is treated as disallowing everything until it can. A 404 means no rules.
   Documented API endpoints (`kind="api"`: Wayback CDX and Availability, SPN, Perma, the TSAs) skip
   the robots check; they are still budgeted, cached, logged and recorded.
2. **Budget.** `efp.ratelimit.DomainLimiter`: one SQLite bucket per domain under `paths.cache`
   (file-locked, so every `efp` process shares it and a restart does not reset it). A budget of
   `rps` is one request every `1/rps` seconds, never a burst. `rate_limits.daily_caps` (0 = switched off: no request is made) adds a per-day
   limit. The budget is taken immediately before each *network* hop: a cache hit costs nothing and a
   redirect to another domain uses that domain's budget. A fetch that cannot get its budget within
   `capture.max_wait_seconds` raises `CaptureError`.
3. **Send.** `requests-cache` session (SQLite, `paths.cache/http_cache.sqlite`, honoring
   `Cache-Control`, default freshness `capture.cache_expire_hours`) with the project `User-Agent`.
   429, 5xx and connection errors are retried with jitter (`stamina`, `rate_limits.retry_attempts`);
   `Retry-After` is honored. A final 429/5xx is still captured: relevance is never judged here.
4. **Record.** WARC pair, store write, one `captures` row. A cached answer gets a row with
   `tool = 'requests-cache'` and the original capture's `warc_path`, and no second WARC record.

## Where things live

| What | Where |
| --- | --- |
| Bodies | `paths.store/<aa>/<sha256>` (content-addressed; `captures.body_path` = `store/<aa>/<sha256>`) |
| WARC 1.1 | `paths.warc/YYYY/MM/YYYY-MM-DD.warc.gz`, one file per UTC day, inside the store so the bag covers it |
| `captures.warc_path` | `YYYY/MM/YYYY-MM-DD.warc.gz#<byte offset of the response record>`, relative to `paths.warc` |
| HTTP cache, rate-limit state | `paths.cache` (`http_cache.sqlite`, `ratelimit.sqlite`) |
| Manifests and tokens | `paths.manifests/<date>.txt`, `<date>.txt.<tsa host>.tsr` |
| BagIt bag | `paths.bags/store/` (a tree of hard links to the store, so the store never moves) |

## WARC fidelity

Records are **reconstructed** from the prepared request and the response object; they are not the
wire bytes. `warcio.capture_http` records the wire request, which would put an `Authorization`
header or an API key into the capture store. The response payload is the decoded body (the
`Content-Encoding`, `Transfer-Encoding` and `Content-Length` headers are replaced by the decoded
length). `WARC-Payload-Digest` is `sha256:<base32>` and `EFP-Content-SHA256` is the same digest in the
hex form `captures.content_sha256` holds, so a body and its record can be matched without conversion.

## Secrets

`Authorization`, `Cookie`, `X-API-Key` and similar headers, and credential-looking query parameters
(`api_key`, `access_token`, ...; for API calls also `key`, `token`, `auth`, `secret`, `signature`),
are replaced with `REDACTED` before anything is stored: `headers_json`, the URL columns, the WARC and
the HTTP cache. Any registered secret value is also scrubbed from error text. A test checks the WARC
and the cache database byte for byte.

## Third-party archives

Save Page Now: every successful page is queued in `spn_queue` at fetch time (once per URL);
`efp spn drain` submits within `capture.spn_per_minute` (80% of the published ~6/min) and records
the Wayback URL in `captures.wayback_url`. A 429 is deferral, never an error. Without `IA_ACCESS` and
`IA_SECRET` the queue accumulates. Perma.cc is called only by export for cited URLs and is a no-op
without `PERMA_API_KEY`. The local WARC is the evidentiary copy; no fetch depends on an archiver.

## Trusted time

`efp manifest` lists every capture not yet in a manifest (`manifest_captures`): one
`<sha256>  <body_path>` line per distinct body, sorted, under a header that chains to the previous
manifest's hash. `--timestamp` gets an RFC 3161 token from each TSA in `capture.tsa_urls`;
`--verify` re-checks them; `--bag` builds or updates the bag and validates it. See
`reference/tsa/README.md` for what `--verify` can and cannot prove without pinned TSA roots.

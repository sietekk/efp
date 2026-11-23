# Build 11 — Reference datasets and loaders

Branch: `build/11-reference-data` (off `main`; build 10 is merged, nothing stacked) · PR: see the pull request for this branch · Date: 2026-11-23 · Model: claude-sonnet-5-5 · Prompt: `build/11-reference-data.md` · Spec sections: 5 (bulk datasets, the API rows), 8 (population prior), 12 (license register)

## What this build set out to do

Load the reference datasets once into the `ref_*` tables with provenance (`ref_sources`: URL, time, SHA-256, license, attribution), unify the candidate sources in `ref_candidates_all` for build 12, add enrichment-only API clients (OpenFEC, Congress.gov, OpenStates) and a Ballotpedia public-page lookup, and report the state of each dataset with `efp selftest --reference`.

Changes the approved plan made to the prompt:

- **Census and SSA are gated, not loaded.** Both reject automated downloads (Census returns a "Request Rejected" page, SSA HTTP 403). You chose to change how requests look; I built a narrowly scoped version (decision 1) and ran it, and it did not get past either host. Those three files are imported by hand (`efp reference import`).
- **`ref_sources` and `ref_candidates_all` are tables created by migration 0011** with the nine reserved names. `ref_candidates_all` is a rebuilt table (indexed on the surname), not a view, because build 12 blocks on it.
- **The loaders are driven by `efp reference ...` and listed as the `[stages.reference]` chain**; `reference` is a command chain, not a case stage. Each dataset is a `Module` whose `available` says why it cannot run.
- `DATA_GOV_KEY` in the plan is not a secret name the loader knows; the clients use `FEC_API_KEY`, `CONGRESS_API_KEY` and `OPENSTATES_KEY`.

## What was built

- `migrations/0011_reference.sql`: `ref_sources`, `ref_surname_freq`, `ref_given_freq`, `ref_population`, `ref_dime`, `ref_fec_candidates`, `ref_ftm`, `ref_local`, `ref_congress`, `ref_state_leg`, `*_current` views, `ref_candidates_all`; `efp/dbcheck.py` lists them.
- `efp/reference/`: `base.py` (the `Dataset` framework: `download`, `load`, `verify`, provenance, byte-identical no-op, gating), `files.py` (download through the capture layer, magic-byte checks, the host-scoped User-Agent retry, manual import), `population.py` (Census surnames with the floor rule, SSA national and by state, Census state population by five-year band), `candidates.py` (FEC, congress-legislators, OpenStates, Harvard local, DIME, FollowTheMoney), `wmn.py`, `index.py`, `names.py` (the one use of `nameparser` 3.x), `ops.py`, `checks.py`, `api/` (`openfec`, `congress`, `openstates`), `ballotpedia.py`.
- `efp/capture.py`: `Capture.resolve_url` (HEAD, follows redirects, reports size), `extra_headers` on `Capture.download` (a manifest records which headers were overridden), and `CaptureRedirect` so a download can be re-asked from each hop.
- CLI: `efp reference status|download|load|verify|import|index`, and `efp selftest --reference`.
- Config (documented in `config.example.toml`): `[reference]` (`max_gb`, `download_user_agent`, `download_user_agent_hosts`, the source URLs, FEC cycles, `harvard_osf_node`, `dime_url`, `ftm_url`), `[stages.reference]`, rate limits for `api.congress.gov` (1.1/s) and `v3.openstates.org` (0.5/s); `pyyaml` and `lxml` declared as dependencies.
- Docs: `docs/sources.md` (every loaded file with URL, vintage, size, rows, SHA-256, license and the attribution sentences), new `docs/license_register.md`, `RUNBOOK.md`.
- Tests: `tests/test_reference.py` and `tests/test_reference_network.py` (58 tests) with synthetic files in each dataset's real format (`tests/reference_helpers.py`).
- Results: `uv run pytest -m "not live"`: 1155 passed. `ruff check`, `ruff format --check`, `mypy efp`: clean.

## What was run

Run on the operator's machine against `~/efp-data` (the real database, migrated to 0011 by the first command):

| Run | Files attempted | Files loaded | Wall clock | Requests | API spend |
| --- | --- | --- | --- | --- | --- |
| `efp reference download` (all) | 23 | 20 | minutes | one per file plus HEAD sizes, redirects followed | none |
| `efp reference load` | 20 | 20 | about 1 minute | none | none |
| Census and SSA with the project identity | 3 | 0 | seconds | refused | none |
| Census and SSA again with a browser-style `User-Agent` (decision 1) | 3 | 0 | seconds | refused | none |

Loaded (current), with counts (full hashes are in `docs/sources.md`):

| Dataset | Rows | Size |
| --- | ---: | ---: |
| `census_population` (51 jurisdictions x 18 bands x 4 years) | 3,672 | 0.8 MB |
| `fec_candidates` (14 cycles, 2000-2026: 4,529 / 3,944 / 3,814 / 3,704 / 4,072 / 5,126 / 5,628 / 5,536 / 7,396 / 7,371 / 7,758 / 8,578 / 9,796 / 8,947) | 86,199 (41,978 candidates once cycles are collapsed) | 3.2 MB |
| `congress` (current 539, historical 12,231) | 12,770 | 10 MB |
| `openstates` (legislature and retired) | 21,889 | 20.3 MB |
| `harvard_local` (candidate-by-election rows) | 126,599 | 26.7 MB |
| `wmn` (sites) | 691 | 0.25 MB |

`ref_candidates_all`: **203,235** candidates: congress 12,770, FEC 41,978 (collapsed to one per candidate and office), Harvard local 126,598, OpenStates 21,889, DIME 0, FollowTheMoney 0. `efp selftest --reference`: 30 checks, 0 failed, including five known values recomputed from the real data (Connecticut's 2023 population, 51 jurisdictions, FEC `P80001571` party REP in 2016, `P80000722` DEM in 2020, bioguide `B000944`'s given name); DIME, FollowTheMoney, Census surnames and SSA are reported as not loaded.

## Results by source

| Source | Files attempted | Loaded | Gated | Notes |
| --- | --- | --- | --- | --- |
| Census state population | 1 | 1 | 0 | columns are `POPEST<year>_CIV` (civilian population), which the loader accepts |
| FEC `cn<yy>.zip` | 14 | 14 | 0 | the URL redirects to the FEC's S3 bucket |
| congress-legislators | 2 | 2 | 0 | |
| OpenStates people | 1 | 1 | 0 | 20.3 MB zip |
| Harvard local (OSF) | 1 | 1 | 0 | found by walking the OSF folders (`Replication/ledb_candidatelevel.csv`) |
| WhatsMyName | 1 | 1 | 0 | pinned and registered as the `wmn_data` instrument |
| Census 2010 surnames | 1 | 0 | 1 | "Request Rejected" page |
| SSA national, SSA by state | 2 | 0 | 2 | HTTP 403 |
| DIME v4.0 | 0 | 0 | 1 | no URL configured; may be very large and sits behind Stanford's repository page |
| FollowTheMoney | 0 | 0 | 1 | no export URL; the site is unmaintained and its API needs an account key. **Not mirrored** |

## Failures by reason

| Reason | Rows | Example | Action taken |
| --- | --- | --- | --- |
| Host returned a web page instead of a ZIP | 1 file | Census `names.zip` | gated, file kept as `.rejected`, never loaded; import by hand |
| HTTP 403 | 2 files | SSA `names.zip`, `namesbystate.zip` | gated; import by hand |
| OSF API read timeout (transient) | 2 attempts | OSF folder listing | retried; loaded on the third try |
| Real layouts differing from the first reading | 2 | Census population `_CIV` columns, Harvard columns | loader aliases added; the gating path worked as designed and the files were reloaded |

## Gated sources encountered (not worked around)

| Source | What it wants | Rows affected | Where logged |
| --- | --- | --- | --- |
| Census 2010 surnames | a browser (its filter refused the capture layer, with and without a browser-style `User-Agent`) | population prior input `ref_surname_freq`: 0 rows until imported | `ref_sources` (status `gated`, reason) |
| SSA national and by state | a browser (HTTP 403) | `ref_given_freq`: 0 rows until imported | `ref_sources` |
| DIME v4.0 | the Stanford repository download page and, perhaps, a form; size unknown | `ref_dime`: 0 | `ref_sources` / `efp reference status` |
| FollowTheMoney | an account key or a file you already have | `ref_ftm`: 0 | `ref_sources` / `efp reference status` |

## Numbers for the first-pass report

- **Candidate universe:** 203,235 candidates in `ref_candidates_all` (above). Years covered: FEC 1978 to 2026 (most from 1990), Harvard local 1989-2021, OpenStates mostly 2000 onward (current and retired legislators), congress 1789-present. Without DIME and FollowTheMoney, **state candidates who never held a seat are mostly absent** (the OpenStates repo holds legislators, not losing candidates) and local candidates before 1989 or after 2021 are absent.
- **Where it is thin** (candidates in the state, all sources; the full table is below): North Dakota, DC, Wyoming, South Dakota and Vermont have under 900 each; every state has data from FEC, congress, state legislators and local elections (no state lacks local data).
- **Population prior:** state-by-age populations are loaded (51 jurisdictions, 2020-2023). The surname and given-name tables are empty until you import the three files, so build 12's prior has **no `p(surname)` or `p(given name)` yet**; it falls back to the published priors (provisional) until then.

### Coverage by state (candidates in `ref_candidates_all`, thinnest first)

| State | All | FEC | Congress | State leg | Local | Years (FEC/state/local) |
|---|---:|---:|---:|---:|---:|---|
| ND | 599 | 85 | 48 | 331 | 135 | 1986-2030 |
| DC | 625 | 90 | 3 | 34 | 498 | 1990-2035 |
| WY | 648 | 159 | 44 | 259 | 186 | 1988-2034 |
| SD | 765 | 104 | 54 | 399 | 208 | 1986-2028 |
| VT | 878 | 153 | 119 | 532 | 74 | 1985-2030 |
| DE | 894 | 136 | 101 | 141 | 516 | 1990-2030 |
| NE | 894 | 249 | 134 | 156 | 355 | 1989-2030 |
| AK | 897 | 248 | 21 | 170 | 458 | 1987-2028 |
| WV | 903 | 242 | 127 | 418 | 116 | 1985-2030 |
| ID | 991 | 225 | 63 | 314 | 389 | 1986-2028 |
| MT | 997 | 203 | 58 | 444 | 292 | 1989-2030 |
| MS | 1,183 | 341 | 162 | 377 | 303 | 1980-2035 |
| RI | 1,238 | 164 | 113 | 259 | 702 | 1985-2030 |
| AR | 1,259 | 271 | 123 | 380 | 485 | 1986-2028 |
| ME | 1,308 | 219 | 179 | 618 | 292 | 1992-2030 |
| OK | 1,631 | 471 | 102 | 405 | 653 | 1984-2030 |
| NV | 1,762 | 583 | 62 | 230 | 887 | 1989-2034 |
| NM | 1,815 | 275 | 63 | 285 | 1,192 | 1988-2030 |
| OR | 1,860 | 500 | 102 | 259 | 999 | 1990-2035 |
| UT | 1,872 | 535 | 63 | 285 | 989 | 1988-2034 |
| AL | 1,904 | 459 | 221 | 281 | 943 | 1966-2035 |
| KS | 1,951 | 328 | 150 | 488 | 985 | 1989-2028 |
| HI | 1,957 | 320 | 29 | 188 | 1,420 | 1990-2030 |
| CT | 2,400 | 362 | 247 | 458 | 1,333 | 1981-2030 |
| IA | 2,409 | 345 | 212 | 405 | 1,447 | 1989-2032 |
| KY | 2,611 | 465 | 382 | 338 | 1,426 | 1989-2028 |
| NH | 2,669 | 303 | 186 | 1,451 | 729 | 1980-2028 |
| AZ | 2,713 | 931 | 65 | 343 | 1,374 | 1984-2030 |
| LA | 2,790 | 549 | 207 | 374 | 1,660 | 1986-2028 |
| SC | 2,901 | 584 | 263 | 371 | 1,683 | 1976-2030 |
| MN | 3,302 | 726 | 173 | 559 | 1,844 | 1988-2034 |
| WA | 3,364 | 797 | 111 | 381 | 2,075 | 1989-2030 |
| MO | 3,481 | 814 | 349 | 596 | 1,722 | 1980-2030 |
| IN | 3,518 | 870 | 358 | 340 | 1,950 | 1981-2030 |
| WI | 3,595 | 496 | 210 | 365 | 2,524 | 1987-2030 |
| CO | 3,647 | 837 | 107 | 350 | 2,353 | 1989-2034 |
| MD | 3,927 | 970 | 320 | 459 | 2,178 | 1983-2035 |
| MA | 4,094 | 489 | 438 | 432 | 2,735 | 1983-2035 |
| TN | 4,096 | 927 | 314 | 300 | 2,555 | 1979-2035 |
| VA | 4,163 | 803 | 452 | 377 | 2,531 | 1980-2034 |
| GA | 4,416 | 988 | 338 | 565 | 2,525 | 1985-2035 |
| NJ | 5,319 | 1,105 | 380 | 334 | 3,500 | 1986-2035 |
| PA | 5,588 | 1,061 | 1,079 | 614 | 2,834 | 1989-2030 |
| OH | 5,975 | 1,172 | 702 | 413 | 3,688 | 1989-2032 |
| NC | 6,119 | 1,067 | 381 | 528 | 4,143 | 1989-2033 |
| FL | 8,185 | 2,124 | 200 | 592 | 5,269 | 1988-2035 |
| MI | 8,610 | 1,102 | 317 | 479 | 6,712 | 1978-2031 |
| IL | 9,879 | 1,277 | 512 | 564 | 7,526 | 1989-2035 |
| TX | 11,090 | 2,714 | 315 | 527 | 7,534 | 1969-2035 |
| NY | 13,545 | 1,747 | 1,511 | 525 | 9,762 | 1981-2035 |
| CA | 32,359 | 3,428 | 439 | 583 | 27,909 | 1982-2035 |

### Candidates by source and five-year period (a candidate counts in every period of their span)

| Source | 1980-1984 | 1985-1989 | 1990-1994 | 1995-1999 | 2000-2004 | 2005-2009 | 2010-2014 | 2015-2019 | 2020-2024 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| congress | 739 | 707 | 734 | 776 | 674 | 743 | 765 | 784 | 720 |
| fec | 11 | 29 | 486 | 1,303 | 6,312 | 4,870 | 8,432 | 8,157 | 13,345 |
| state_leg | 36 | 64 | 144 | 229 | 508 | 2,220 | 7,301 | 7,468 | 11,128 |
| harvard_local | 0 | 955 | 7,584 | 13,894 | 20,839 | 21,530 | 27,066 | 26,015 | 8,715 |

(FEC rows with an implausible election year, such as 2990 in the source files, and open-ended terms are NULL in the index and are left out here.)

## Review findings (done-when-reviewer) and what was done

| Finding | Action |
| --- | --- |
| A refresh or a refused re-download could delete or overwrite a good raw file, and `verify` passed when the file was missing | a download goes to `<file>.new` and replaces the file only after it passes the leading-bytes check; a refusal never touches the old copy; `verify` fails when the file is missing; tests |
| The browser `User-Agent` could reach an unlisted host through a redirect, and was retried after any error | it is attached only to an address whose host is listed, and only after a 401, 403 or 406 or a web page; tests |
| `resolve_url` HEADs were budgeted but not logged | each hop is logged; test |
| The `wmn_data` instrument was redirected only inside the process that loaded the file | set at CLI startup from `paths.bulk`; test (the carried-forward line is now true) |
| The selftest recomputed no known value on real data and the index check was optional | known values for the population, FEC and congress data; the index check is required once any candidate source is loaded and fails when the index is stale; `efp reference load` rebuilds the index; tests |
| `nameparser>=2.3` but the code uses the 3.x API, and an `except Exception` hid an ImportError | floor `>=3.1`, the ImportError propagates (test); the tool register notes it |
| The `[stages.reference]` chain was ignored | datasets are selected from the configured chain; test |
| Harvard's `ref_sources.url` was NULL | the URL the capture used (query removed) is recorded |
| The selftest opened the database read-write; an OpenStates zip was never closed; two places said FollowTheMoney was mirrored; docs title and a possibly real bioguide ID in the tests | read-only open; the zip is closed; wording fixed; title and synthetic IDs |
| The `reference.max_gb` cap was checked only against a HEAD's Content-Length, so a refused HEAD, a chunked answer or a redirect with no length let a download stream with no limit | `Capture.download` takes `max_bytes`: a response announcing more is refused before a byte is written and a stream that passes the cap is abandoned and its partial file deleted; `files.fetch` passes the cap; tests (HEAD refused, chunked, announced) |
| FEC `cn.txt` was split with `str.splitlines()`, which also breaks on U+0085 and similar characters that latin-1 makes from stray bytes, silently dropping a split record | split on newline only, `\r` stripped, and the skipped malformed lines are counted in `ref_sources.notes_json` (`skipped_malformed_lines`, first line numbers); test. Checked on the real files: the old logic and the new one both give 86,199 records, so nothing was lost |
| **Found while backfilling the Harvard URL:** the OSF download redirects to a presigned object-store URL and its `X-Amz-Signature` was stored unredacted in one capture (id 57) | presigned-URL credential parameters are now redacted by `redact_url`; test. The one already-stored row is append-only and cannot be edited: its signature expired within 60 seconds of 2026-11-23 04:18 UTC |

## Spot-check guide for the operator

- `efp reference status`, then `efp selftest --reference`.
- Open `docs/sources.md`'s hash table and compare one file's SHA-256 with `shasum -a 256 ~/efp-data/bulk/<dataset>/<file>`.
- `SELECT * FROM ref_candidates_all WHERE surname_key = 'SMITH' AND state = 'CT' LIMIT 20;` and check that office, party and years look right for a few names you know.
- Harvard party values are estimates: `SELECT DISTINCT party_imputed FROM ref_candidates_all WHERE source = 'harvard_local'` must be only `1`.

## Decisions needed from the operator

1. **The `User-Agent` override for Census and SSA (a policy change you made).** `reference.download_user_agent` (default empty) lets a download from a listed host that was refused be tried once more with a browser-style `User-Agent`; nothing else changes, no challenge is solved, and the capture records that it was used. It was tried on 2026-11-23 and did not get past either host. I did not go further (other headers, TLS impersonation, browser automation): that goes beyond what you approved and beyond the earlier "no bot-check evasion" instruction. Default: off.
2. **Import the three files by hand** (Census `names.zip`, SSA `names.zip` and `namesbystate.zip`) with `efp reference import ...`. Until then there is no population prior from these sources.
3. **DIME and FollowTheMoney.** Give a download URL (or a file) for each, or accept that the candidate index lacks them. FollowTheMoney's mirror, which the plan asked for in this build, was **not made**: nothing public was reachable without a key or account.
4. **The surname floor rule.** A surname absent from the Census file gets half the smallest listed proportion (count 100 over the file's population), recorded in `ref_sources.notes_json` with the rule's name. Default: that rule.
5. **Harvard party is always `party_imputed = 1`.** The database's only party column is its own estimate (`pid_est`), so every value is flagged; none is asserted. Default: as stated.
6. **FEC election-year typos and open-ended terms.** Years outside 1789-2035 (the FEC files hold some, such as 2990; OpenStates uses 2100 for a term with no end) are NULL in `ref_candidates_all`; a NULL last year means open-ended or unknown. The source tables keep what was published.
7. **The OpenStates repo gives legislators only**, not state candidates who lost; Harvard local covers local offices only. State candidates are thin until DIME or FollowTheMoney is loaded.

## Operator decisions

1. Decision: ______ · Date: ______
2. Decision: ______ · Date: ______
3. Decision: ______ · Date: ______
4. Decision: ______ · Date: ______
5. Decision: ______ · Date: ______
6. Decision: ______ · Date: ______
7. Decision: ______ · Date: ______

## Carried forward

- 12: match against `ref_candidates_all` (blocking on `surname_key`, `state`); `source`, `source_id` identify the record in its source (FEC candidate ID, bioguide ID, OpenStates person ID, DIME `bonica_rid`, Harvard `ledb_candid`); `party` is an identity signal only and `party_imputed = 1` means an estimate that must never be asserted; nicknames are build 12's table.
- 12: the population prior reads `ref_surname_freq_current`, `ref_given_freq_current` and `ref_population_current`; the surname floor and its rule are in `ref_sources.notes_json` (dataset `census_surnames`) and must be reported; until the Census and SSA files are imported those tables are empty and the prior must be provisional.
- 12, 19: enrich a confirmed match with `efp.reference.api.{openfec,congress,openstates}.enrich(ctx, ConfirmedMatch(...))` (IDs only, never a name); each raises `ApiUnavailable` for a missing key or a refusal, so call it inside a handler; `ballotpedia.lookup(ctx, name, state)` is the public-page check for a public figure.
- 15: the pinned WhatsMyName file is `~/efp-data/bulk/wmn/wmn-data.json`; `efp.reference.wmn.pin(config)` gives its hash and, in the CLI, the `wmn_data` instrument is pointed at it at startup (a script that does not go through the CLI must call `efp.instruments.override("wmn_data", str(wmn_path(config)))` or use `pin(config)`); record the hash in `captures.site_data_sha256`. CC BY-SA 4.0: attribute Micah Hoffman; a derived published allowlist is share-alike.
- 18: party fields in the `ref_*` tables are identity signals for public figures, never evidence of motive.
- 20: `docs/license_register.md` has the data rows; complete it.
- All: read the reference tables through the `*_current` views (a changed file is a new load and the old rows stay with `current = 0`); `efp reference index` must be re-run after any load; a load of a byte-identical file is a no-op.
- All: `efp reference load` rebuilds `ref_candidates_all` when a candidate dataset loaded or the index is stale; `efp selftest --reference` fails on a stale index.
- All: `Capture.download` still refuses redirects (it raises `CaptureRedirect` with the location); `Capture.resolve_url` gives the final address and size first.

## Done-when check

- "All reference tables are loaded on my machine with counts in the report, FollowTheMoney is mirrored, and `efp selftest --reference` passes." — **Partly holds.** Loaded: Census population, FEC, congress, OpenStates, Harvard local, WhatsMyName (counts above); `efp selftest --reference` passes (25 checks, 0 failed). **Not loaded:** Census surnames, SSA national and by state (refused by their hosts; import by hand), DIME and FollowTheMoney (no URL or key). **FollowTheMoney is not mirrored.** Decisions 2 and 3.
- "`ref_candidates_all` exists with per-source counts, and the report states coverage by state and year for the candidate universe." — **Holds** (203,235 candidates; per-source counts and the by-state table above; years by source in "Numbers for the first-pass report").
- "`docs/sources.md` is complete with hashes." — **Holds for every loaded file** (20 rows with SHA-256); the four gated datasets are listed with what to do.

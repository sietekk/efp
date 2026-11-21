# Build 15: Social — handle-only pivot

The IRB determination is in hand (November 21, 2026), so this stage is in the first pass. Before running it, the operator records the determination in `config.toml` under `[irb]` (`cleared = true`, `determination`, `date`, `reference`, `scope_notes`); the stage's `available()` returns `unavailable("IRB fields not recorded in config.toml")` until those fields are filled, and the build completes either way. Read `[irb].scope_notes` first: any condition the IRB attached (public posts only, no contact with subjects, retention limits) is a hard rule for this build and goes into the report.

Spec: section 4 (social component and contract), section 1 (F7: handle or email only; reject real-name input at the interface), section 5 (Bluesky public AppView), section 7 (pinned instruments), section 12 (compliance: Reddit for Researchers, Meta Content Library, X paid tier or not at all; WhatsMyName share-alike). Task list: D10. Needs: 12, 13. Session: S5. Branch: `build/15-social`. Report: `reports/15-social.md`.

Read first: `CLAUDE.md`, `build/README.md`, `docs/spec.md` section 4 "social" row and contract, `docs/research-plan.md` on the social stage (its limits are the research design's, not only the engineering's), the `social_accounts` table, `reports/12-identity.md`, `reports/13-news.md`.

Stay in plan mode until I approve. Propose the type-level guard, the allowlist contents and categories, the engine order, the self-test calibration capture, and what "platform retrieval via authorized routes only" means per platform. Wait.

## What to build

1. **The interface forbids names.** `efp/social/__init__.py:run` accepts `Handle | Email` values taken from `identifiers` (`kind in ('handle','email')`) and nothing else; a real name cannot be passed because the type does not admit one, and a test asserts that a string that looks like a person's name is rejected at the boundary. Handles enter `identifiers` only from captured records (a court filing or an article naming the account), never from a search.
2. **Gate.** `news` done, identity `confirmed` (not review band), at least one handle or email identifier, `first_pass.social_enabled = true` and `[irb].cleared = true`; otherwise `skipped` with the reason. The plan expects this stage to apply to a small minority of rows; report the count.
3. **Allowlist.** `allowlist.json`, pre-registered: platform entries limited to categories where political speech occurs, each with the retrieval route allowed (public API, approved research program, none). Hash recorded through `instruments` and written to `social_accounts.allowlist_sha256` and `allowlist_version`. Any published filtered list derived from WhatsMyName data is CC BY-SA 4.0 and attributes Micah Hoffman.
4. **Engines as modules** (`social.enumerate.naminter`, `social.enumerate.maigret` under `[stages.social]`, policy `all`, Naminter first; `social.retrieve.bluesky`, `social.retrieve.reddit_r4r`, `social.retrieve.meta_content_library`, `social.retrieve.x_api`, policy `all`, where the last three are gated and report themselves unavailable). Naminter first (its result retains status code, headers and raw body, and its status enum separates MISSING from UNKNOWN), Maigret second with `ranked_sites_dict(names=allowlist)` and auto-update disabled; both read only the pinned WMN data from build 11. Both tools' known-username self-tests run at stage start and their results are stored as a calibration capture (`captures.site_data_sha256` set). Raw responses are retained as captures.
5. **Platform retrieval via authorized routes only.** Bluesky through the public AppView (`public.api.bsky.app`, 3,000 per 5 minutes per IP) with `atproto`; Reddit only through Reddit for Researchers and Meta only through the Content Library, both of which are applications and therefore gated (record the gate; do not apply); X only on a paid tier or not at all (decision pending; off). No scraping of any platform; no login-walled page is ever fetched.
6. **Outputs.** `social_accounts` rows (`platform`, `handle`, `attribution_basis` = the capture that named the account, allowlist version and hash, the capture that surfaced the profile) and captured public posts within the case window as captures for build 16's extraction. Nothing is stored for a row whose account attribution rests on anything other than a captured record.

## Tests

- Type-level rejection of names; gate outcomes; allowlist hash recorded; engine order and self-test capture with cassettes; Bluesky client budget.
- No code path constructs a search from `rows.name` (assert by import and call-graph scan).

## Done when

- The gate admits only confirmed rows with captured handles; the run on them produces `social_accounts` rows with attribution and captures, and the report states how many rows the stage touched, which platforms were reachable, and which remain gated (Reddit, Meta, X) with the rows affected.
- The IRB determination's fields are echoed into the methods appendix section this build adds (`docs/methods_appendix.md`, "Human subjects"), and `efp selftest --compliance` fails if the social stage is enabled with `[irb].cleared` false.

## Guardrails that bite here

Account discovery by name is out of scope by design, not by configuration. Approved research programs are gated sources. Everything retrieved is public, captured, and stays in `~/efp-data/`.

Finish with `reports/15-social.md` and a pull request. Do not merge.

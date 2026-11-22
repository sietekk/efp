# Maintenance 02: Remove robots.txt and politeness policies

Branch: `maint/02-remove-robots` (stacked on `build/07-court-federal`; PR #9 is unmerged) · Date: 2026-11-22 · Decision: the operator's, deliberate (a project policy and research decision).

## What changed

The project no longer reads or honors `robots.txt`, and no longer carries politeness rules beyond rate limiting.

- **Capture layer** (`efp/capture.py`): the `robots.txt` fetch, its per-domain cache, the disallow check on every hop and on downloads, and the `robots_txt` / `robots_disallow` capture kinds are gone. `kind="page"` now only means "queue for Save Page Now"; `kind="api"` means "documented endpoint, not queued". The adapter is `_BudgetAdapter`.
- **Config**: `rate_limits.honor_robots_txt` and `capture.robots_cache_hours` are removed (strict loader: a config that still has them now fails to load; delete the two lines from any local copy). The `project.user_agent` rule that it must carry the contact address is dropped; the value is free-form, still balanced-parenthesis checked.
- **Link resolution and report**: the "unreachable by policy" state and the report's "policy, not rot" paragraph are removed; a page that cannot be fetched is dead or unresolved like any other.
- **Court stage**: DOJ release fetches no longer special-case a robots block.
- **Selftest** no longer prints a robots line.
- **Tests**: the robots-specific tests are deleted (disallow, caching, status handling, redirect and query-string rules, downloads, link report, link resolution, DOJ fallback); mocks of `robots.txt` responses are removed from the rest. Suite: 890 passed.
- **Docs and prompts**: `CLAUDE.md`, `docs/plan.md`, `docs/spec.md`, `docs/capture.md`, `docs/sources.md`, `RUNBOOK.md`, the build prompts and `.claude/agents/done-when-reviewer.md` no longer mention robots.txt, politeness, or the descriptive-User-Agent rule. Reports of earlier builds are history and are left as written.

## What stays

- Per-domain rate budgets (80% of a published limit, 1 request per 2 seconds otherwise), daily caps (0 = switched off), `Retry-After` and retry with jitter, the response cache, WARC capture, secrets redaction.
- Sources whose terms forbid automation are still not scraped; they go through the manual import path and are listed as gated.

## Consequences to know

- Rows already in a database keep their old `robots_disallow` captures (captures are append-only); nothing creates new ones.
- A site that refuses a request now shows up as an HTTP error capture (for example 403), not as a policy skip; the court and link stages treat it as a failed fetch.
- Build 08 (state AG harvester) stacks on this branch.

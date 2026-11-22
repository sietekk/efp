# Report m01: Remove IRB

Branch `maint/remove-irb` off `main` (bd4294e). Pull request: https://github.com/sietekk/election-fraud-pipeline/pull/5. `build/m01-remove-irb.md` was already on `main` (PR #4), so this commit does not touch it.

## Files changed

| File | Removed or changed |
|---|---|
| `efp/config.py` | `Irb` section class and the `irb` field on the root model |
| `efp/selftest.py` | the `"irb cleared"` check |
| `tests/test_config.py` | the `.irb.cleared` assertion; the test still loads `config.example.toml`. The schema/example key-parity test is untouched and passes. |
| `tests/fixtures/config.valid.toml` | `[irb]` block |
| `config.toml`, `config.example.toml` | `[irb]` block and comments; `social_enabled` comment is now `# D10 runs in the first pass` |
| `CLAUDE.md` | read-order items 4 and 6 trimmed; social guardrail reduced to handle-only wording |
| `RUNBOOK.md` | build 15 bullet |
| `BUILD.txt` | `[irb]` line in step 4; commit message is `Config: contact, model` |
| `build/README.md` | row 15 last cell is `—`; the "IRB determination is in hand" sentence |
| `build/04-runner.md` | `irb.cleared` and `irb_not_recorded` from the social gate and the guardrails list |
| `build/15-social.md` | opening paragraph; `[irb].cleared` in the gate item; final Done-when bullet |
| `build/20-integrity-compliance.md` | `[irb].cleared` clause replaced with "the social stage runs only when `first_pass.social_enabled` is true" |
| `docs/plan.md` | amendment replaced by the one-line version; Stage 4 "Deferred" bullet; `4. IRB` row (nothing renumbered) |
| `docs/spec.md` | section 12 IRB compliance point; "IRB path and who files" checkbox; see decision below |
| `docs/tasks.md` | IRB status task; `needs IRB` fragments on Reddit and Meta tasks; D10 retitled "D10. `social` — after news"; `A IRB` in D10 `needs`; `A IRB answer` and its `GATE →` in E6 |
| `docs/research-plan.md` | **IRB.** paragraph and the IRB checklist item |
| `docs/summary.md` | one note added under the header note; no other text edited |

Left alone as instructed: `reports/*.md`, the Reddit row of `docs/tool-register.md`, git history.

## Decisions needed from the operator

- `docs/spec.md` line 599 (people-search open decision) said lookups on private individuals "raise IRB questions". The prompt's list did not name it, but the acceptance grep would flag it. Default taken: "IRB questions" became "human-subjects questions"; the rest of the line is unchanged.
- `docs/summary.md` line 100 (decision 6, people-search) contains "IRB" in text kept as sent, so it shows up in the grep alongside lines 77 and 98, which the prompt names as kept.

## `git grep -n -i irb` on the branch

```
build/m01-remove-irb.md:1:# Maintenance 01: Remove IRB
build/m01-remove-irb.md:3:No IRB determination is required for this project at this time. Remove every IRB gat
build/m01-remove-irb.md:5:Start with `git grep -n -i "irb"` on `main` and work from that list; it is the accep
build/m01-remove-irb.md:11:- `efp/config.py`: delete the `Irb` section class and the `irb` field on the root s
build/m01-remove-irb.md:12:- `efp/selftest.py`: delete the `"irb cleared"` check. No other check replaces it.
build/m01-remove-irb.md:13:- `tests/test_config.py`: delete the assertion on `.irb.cleared`. `tests/fixtures/c
build/m01-remove-irb.md:17:- `config.example.toml` and `config.toml`: delete the `[irb]` block with its commen
build/m01-remove-irb.md:23:- `BUILD.txt`: in step 4, delete the `[irb]` line; the commit message line becomes 
build/m01-remove-irb.md:24:- `build/README.md`: row 15's last cell becomes `—`; in the "Two rules every prompt
build/m01-remove-irb.md:25:- `build/04-runner.md`: in the gates item, the social gate requires `news` done, id
build/m01-remove-irb.md:26:- `build/15-social.md`: delete the opening paragraph about the determination and `[
build/m01-remove-irb.md:27:- `build/20-integrity-compliance.md`: in the compliance-checks item, replace the cl
build/m01-remove-irb.md:31:- `docs/plan.md`: replace the November 21 amendment blockquote with one line: "> **
build/m01-remove-irb.md:32:- `docs/spec.md`: delete the **IRB** compliance point in section 12 and the "IRB pa
build/m01-remove-irb.md:33:- `docs/tasks.md`: delete the **IRB status** task and its `GATE →`; in the Reddit f
build/m01-remove-irb.md:34:- `docs/research-plan.md`: delete the **IRB.** paragraph and the IRB checklist item
build/m01-remove-irb.md:38:- `docs/summary.md` is the record of what Dr. Treistman received; do not edit its t
build/m01-remove-irb.md:45:- `git grep -n -i "irb"` on the branch returns matches only in `docs/summary.md` (t
build/m01-remove-irb.md:46:- `uv run pytest -m "not live"`, `uv run ruff check .`, `uv run ruff format --check
build/m01-remove-irb.md:50:Write `reports/m01-remove-irb.md`: the list of files changed with what was removed 
build/m01-remove-irb.md:52:Open the pull request with the title `Remove IRB` and a body that is exactly the tw
docs/summary.md:7:IRB: resolved November 21, 2026; no determination is required at this time. The guidance par
docs/summary.md:77:One item I need your guidance on: whether this project has an IRB determination. Research o
docs/summary.md:98:4. Has the project been through IRB, and if not, who files? Is this even needed?
docs/summary.md:100:6. Should identity confirmation be allowed to use a people-search service where public rec
docs/tool-register.md:113:| Reddit for Researchers | — | Free, approved | Active | The only authorised researc
reports/01-scaffold.md:17:- **CLI.** All ten commands registered. `selftest` reports versions, config validity
reports/01-scaffold.md:62:5. **`[irb]` holds placeholders.** `cleared = true`, but `reference = "test"`, `scop
```

Matches are only in `build/m01-remove-irb.md`, `docs/summary.md`, `docs/tool-register.md` (Reddit row) and `reports/01-scaffold.md`.

## Tests

- `uv run pytest -m "not live"`: 447 passed, 0 failed (via test-runner).
- `uv run ruff check .`: passed. `uv run ruff format --check .`: passed (66 files). `uv run mypy efp`: no issues in 38 files.
- `uv run efp selftest`: runs, 0 lines mentioning irb.
- Unknown-key check, with a copy of `config.toml` plus an `[irb]` table:

```
EFP_CONFIG=<scratch>/config.irb.toml uv run efp selftest --strict   # exit 1
[MISSING] config                  invalid configuration in <scratch>/config.irb.toml:
  irb: Extra inputs are not permitted
```

Without `--strict`, `efp selftest` reports the same `[MISSING] config` line but exits 0; only `--strict` returns non-zero. That is existing behavior and was not changed.

## Pull request

`gh pr view --json title,body`:

```
{"body":"Remove IRB","title":"Remove IRB"}
```

The `done-when-reviewer` subagent was not run: the change is mechanical and its acceptance test is the grep above.

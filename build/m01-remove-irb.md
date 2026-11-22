# Maintenance 01: Remove IRB

Remove every IRB gate, setting, check and instruction from the repository, so nothing in code, configuration, prompts or operating documents refers to an IRB. The social stage (build 15) stays in the first pass; its handle-only rules are unchanged. This is mechanical: no plan-mode proposal needed. Branch: `maint/remove-irb`.

Start with `git grep -n -i "irb"` on `main` and work from that list; it is the acceptance test at the end.

## Remove entirely

**Code and tests**

- `efp/config.py`: delete the `Irb` section class and the `irb` field on the root settings model. The loader is strict, so after this an `[irb]` table in any config file is a hard error; that is the intended behavior.
- `efp/selftest.py`: delete the `"irb cleared"` check. No other check replaces it.
- `tests/test_config.py`: delete the assertion on `.irb.cleared`. `tests/fixtures/config.valid.toml`: delete the `[irb]` block. Keep the test that asserts the config schema and `config.example.toml` have the same keys; it must pass after both sides lose the block.

**Configuration**

- `config.example.toml` and `config.toml`: delete the `[irb]` block with its comment lines. On `first_pass.social_enabled`, change the trailing comment to `# D10 runs in the first pass`.

**Operating documents and prompts**

- `CLAUDE.md`: item 4 of the read order loses the clause after "D1 through D12" (end the sentence there); item 6 loses the sentence about the social-stage condition; the "Social stage (build 15)" guardrail bullet becomes: "Social stage (build 15): handles and emails enter only from captured records; nothing is ever looked up by a person's name."
- `RUNBOOK.md`: the build 15 bullet becomes "Build 15 (social) is in the first pass (session S5)."
- `BUILD.txt`: in step 4, delete the `[irb]` line; the commit message line becomes `git commit -m "Config: contact, model"`.
- `build/README.md`: row 15's last cell becomes `—`; in the "Two rules every prompt obeys" paragraph, delete the sentence that begins "The IRB determination is in hand".
- `build/04-runner.md`: in the gates item, the social gate requires `news` done, identity `confirmed`, at least one `identifiers.kind IN ('handle','email')`, and `first_pass.social_enabled` (gate reason `social_disabled`); drop `irb.cleared` and `irb_not_recorded` here and in the guardrails paragraph's list of gate reasons.
- `build/15-social.md`: delete the opening paragraph about the determination and `[irb]`; in the gate item, drop `and [irb].cleared = true`; delete the final "Done when" bullet about echoing the determination into the methods appendix and the `selftest --compliance` IRB failure. The methods appendix keeps its data-handling statements (public records only, handle-only input, redaction); none of them depend on an IRB.
- `build/20-integrity-compliance.md`: in the compliance-checks item, replace the clause about `[irb].cleared` with "the social stage runs only when `first_pass.social_enabled` is true".

**Source documents under `docs/`** (repository copies; edit them, do not rewrite them)

- `docs/plan.md`: replace the November 21 amendment blockquote with one line: "> **Amendment, November 21, 2026.** Stage 4 (public social media; task list D10; build prompt 15) is in the first pass, in session S5 after news; its design is unchanged." Delete the Stage 4 bullet under "Deferred, and why". Delete the `4. IRB` row from the open-decisions table and renumber nothing (the numbers are the Summary's).
- `docs/spec.md`: delete the **IRB** compliance point in section 12 and the "IRB path and who files" checkbox in the open decisions.
- `docs/tasks.md`: delete the **IRB status** task and its `GATE →`; in the Reddit for Researchers and Meta Content Library tasks, delete `needs IRB determination;` / `needs IRB;`; retitle D10 to "D10. `social` — after news"; in D10's `needs` line and in E6, delete `A IRB` / `A IRB answer` and the `GATE →` that depends on it.
- `docs/research-plan.md`: delete the **IRB.** paragraph and the IRB checklist item.

## Leave as they are

- `docs/summary.md` is the record of what Dr. Treistman received; do not edit its text. Add one line directly under its existing header note: "IRB: resolved November 21, 2026; no determination is required at this time. The guidance paragraph and decision 4 below are kept as sent."
- `reports/*.md` are history; do not edit them.
- `docs/tool-register.md`: the Reddit for Researchers row states that program's own requirement; it is a fact about Reddit, not about this project. Leave it.
- Git history is not rewritten.

## Done when

- `git grep -n -i "irb"` on the branch returns matches only in `docs/summary.md` (the original lines plus the one new note), `reports/*.md`, the Reddit row of `docs/tool-register.md`, and this prompt file. Paste that output into the report.
- `uv run pytest -m "not live"`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy efp` pass; `uv run efp selftest` runs and prints no line mentioning irb; `uv run efp selftest` against a temporary config that still contains `[irb]` fails with the unknown-key error (show the command and the first line of the error in the report).

## Report and pull request

Write `reports/m01-remove-irb.md`: the list of files changed with what was removed from each, the `git grep` output, and the test results. Include this prompt file (`build/m01-remove-irb.md`) in the commit.

Open the pull request with the title `Remove IRB` and a body that is exactly the two words `Remove IRB`: no summary, no checklist, no footer, no attribution or "generated with" line. Confirm with `gh pr view --json title,body` and paste the result into the report. Do not merge.

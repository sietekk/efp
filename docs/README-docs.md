# docs/

Claude Code reads everything in this folder at the start of each build, in the order `CLAUDE.md` gives.

| File | Status | Source |
| --- | --- | --- |
| `plan.md` | in the kit | First-Pass Build Plan (Claude doc, https://claude.ai/artifact/KWA19wJNcrvVMAxaoHUS4q) |
| `spec.md` | in the kit | Google Doc "Election Fraud Pipeline Technical Specification", October 27, 2026 |
| `summary.md` | in the kit | Google Doc "Election Fraud Pipeline Technical Solution - Summary", as sent to Dr. Treistman |
| `figures/` | in the kit | The three diagrams referenced by `spec.md` and `summary.md` |
| `tasks.md` | **export before the first build** | Election Fraud Pipeline Task List (https://claude.ai/artifact/EkuRo8Njb2LFCgaqXUDtFD) → export as Markdown |
| `research-plan.md` | **export before the first build** | Election Fraud Ideology Coding: Research Plan, main tab (https://claude.ai/artifact/DCgdMWZKAheQMYTpkE6w26) → export as Markdown |
| `tool-register.md` | **export before the first build** | The Research Plan's "Tool register" tab → export as Markdown |
| `coverage_matrix.md` | generated in builds 08, 09 and 19 | — |
| `first-pass-notes.md` | written in build 21 | — |

If one of the three exports is missing when a build starts, Claude Code is instructed to say so in the report and continue without inventing its contents; the builds that depend most on them are 01 (tool register), 12 (research plan: identity rules) and 18 (research plan: the codebook and the thirteen columns).

Keep these as copies. The Google Docs and Claude docs remain the editable originals; when one changes, re-export it here and commit, so the repository records which version each build read.

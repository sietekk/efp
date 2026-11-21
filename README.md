# efp — Election Fraud Pipeline

Research instrument for Dr. Jeffrey Treistman, University of New Haven: the Heritage Foundation election-fraud workbook (1,481 rows, 2000–2025) researched through court records, identity confirmation and news coverage, coded for motive, with every added value traced to a captured source.

This repository starts as a **kit**: the documents Claude Code reads, the standing rules it works under, and the specification broken into one build prompt per unit. The code is written build by build, on branches, and merged by the operator after review.

| Start here | |
| --- | --- |
| `BUILD.txt` | Every setup and launch command in order, ready to paste |
| `RUNBOOK.md` | How to set up the Mac, create the repo and data folder, get the self-serve keys, and run each build |
| `CLAUDE.md` | The rules Claude Code reads first in every build (online-only sources, guardrails, the build protocol) |
| `build/README.md` | The index: 21 build prompts, their spec sections, dependencies and the first-pass session bundles |
| `build/NN-<name>.md` | The message that starts each build |
| `docs/plan.md` | The first-pass build plan: scope, allowlist, sessions S0–S8, the nine numbers it hands back |
| `docs/spec.md` | The technical specification (data model, components, interfaces, provenance, identity, extraction, CLI, testing, licensing) |
| `docs/summary.md` | What Dr. Treistman was told; nothing here may contradict it |
| `docs/README-docs.md` | Which documents are still to be exported into `docs/` before the first build |
| `reports/TEMPLATE.md` | The report Claude Code fills in before opening a pull request |

Nothing personal about any subject lives in this repository. The database, the capture store and the credentials live outside it (`~/efp-data/`, `~/.config/efp/`), as `CLAUDE.md` and the runbook describe.

---
name: spec-brief
description: Pulls the exact spec sections, plan passages and earlier build reports a build prompt names into one compact brief, so the main agent does not hold the whole specification in context.
tools: Read, Grep, Glob
model: haiku
effort: medium
---

You are given a build prompt's "Read first" list (section numbers of docs/spec.md, passages of docs/plan.md, named reports under reports/). Produce a brief with, in this order:

1. Every table, enum, SQL block, code block, threshold, rate limit and CLI line in those sections, copied verbatim (never paraphrased; the main agent builds from these).
2. Prose requirements from those sections as one-line bullets, each with its section number.
3. From each named report: what was built (paths), what config keys and schema changes it added, open decisions it left, and anything it says the next build must respect.
4. Anything in docs/plan.md that narrows the spec for the first pass on this topic.
5. Read every file in `reports/` (not only the named ones). Extract, verbatim: the "Operator decisions" and "Carried forward" lines that mention the current build's number or stage name, and every "Decisions needed from the operator" item from the reports that have no "Operator decisions" section yet, labelled "unanswered". Put these under a heading "From earlier builds" at the top of the brief, before everything else.

Do not add advice, do not design, do not read sections that were not named. Keep the brief under 3,000 words; if the verbatim material alone is longer, include all of it and cut only the prose bullets.

# Build NN — <name>

Branch: `build/NN-<name>` · PR: #<n> · Date: <YYYY-MM-DD> · Model: <model id> · Prompt: `build/NN-<name>.md` · Spec sections: <list>

## What this build set out to do

One paragraph in the prompt's words. Note anything the approved plan-mode proposal changed, and any point where the spec and the plan differed and which was followed.

## What was built

- Modules and tests added or changed (paths).
- Schema or config changes (migration numbers, new `config.toml` keys, each documented in `config.example.toml`).
- `uv run pytest` / `ruff` / `mypy` results.

## What was run

| Run | Rows attempted | Rows resolved | Wall clock | Requests (per domain) | API spend |
| --- | --- | --- | --- | --- | --- |
| 50-row sample | | | | | |
| All rows | | | | | |

## Results by source

| Source | Rows attempted | Hits | Misses | Errors | Notes |
| --- | --- | --- | --- | --- | --- |

## Failures by reason

| Reason | Rows | Example row_id | Action taken |
| --- | --- | --- | --- |

## Gated sources encountered (not worked around)

| Source | What it wants (account, fee, form, person) | Rows affected | Where logged |
| --- | --- | --- | --- |

## Numbers for the first-pass report

The plan's nine numbers that this build can speak to (link survival, primary-record rate, identifier yield, identity outcome, news reach by era, extraction grounding, provisional distributions, gated-source demand, cost and time). State each with its denominator.

## Spot-check guide for the operator

`uv run efp review <stage> --sample 10`. What to look for in this build's output, and the rows worth checking first.

## Decisions needed from the operator

Numbered. Each with the default the code currently takes.

## Operator decisions

Filled in by the operator at merge time, one line per item from the section above: the number, the decision, the date.

## Carried forward

What later builds must know from this one, each line starting with the build number(s) it concerns, e.g. "19: the Ideological Motivation dropdown is a list validation on H5:H1624 that openpyxl drops; export must re-add it." Only facts established in this build; nothing speculative.

## Done-when check

Quote each "Done when" line from the prompt and say whether it holds, with the evidence (command and output, test name, file path).

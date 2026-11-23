# F3 audit sample

Each confirmed row below is to be re-derived by hand from its captures. Print the
evidence with `efp identity explain <row_id>`, open the captures it lists, decide, and
record the verdict with `efp review identity --audit` (outcome `audit_correct` or
`audit_wrong`, with a rationale).
Then `efp identity audit rate` gives the false-confirmation rate by stratum.

This file holds row ids only. The names and the verbatim spans are in the data directory
(`exports/identity/f3_audit_full.md`) and never leave it.

| audit | row | draw | stratum | was in review band | weight | verdict |
| --- | --- | --- | --- | --- | --- | --- |

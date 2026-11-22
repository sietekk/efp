# Row hash: `rows.row_sha256`

Implementation: `efp/rowhash.py` (one function used by ingest, export and `efp selftest --db`).
Build 19's export verifies against this rule, so changing it is a schema-level change.

## Rule

```
row_sha256 = SHA-256( utf8( cell_1 ␟ cell_2 ␟ … ␟ cell_9 ) )      ␟ = U+001F (unit separator)
```

The nine cells are, in this fixed logical order (not sheet order):

| # | Header | `rows` column |
| - | --- | --- |
| 1 | Year | `year` |
| 2 | State | `state` |
| 3 | Name | `name` |
| 4 | Case Type | `case_type` |
| 5 | Fraud Type | `fraud_type` |
| 6 | Details | `details` |
| 7 | Source | `source_raw` |
| 8 | Ideological Motivation | `ideological_motivation` |
| 9 | Ideological Source | `ideological_source` |

Each cell is the value **exactly as openpyxl returns it** (`load_workbook(read_only=True,
data_only=True)`), turned into text like this. The hash therefore depends on how openpyxl decodes
cells: `uv.lock` pins the exact version, and ingest records the workbook's own SHA-256 beside the
rows, so a decoding change shows up as a verification failure rather than a silent drift:

| openpyxl value | Contributes |
| --- | --- |
| `None` | the empty string |
| `str` | the string itself, unchanged (never stripped, case-folded or normalised); `""` is the empty string |
| `int` | `U+001E` + `int:` + `repr(value)`, e.g. `␞int:2016` |
| `float` | `U+001E` + `float:` + `repr(value)` |
| `bool` | `U+001E` + `bool:` + `repr(value)` |
| `datetime` / `date` / `time` | `U+001E` + `datetime:` / `date:` / `time:` + ISO-8601 |
| `timedelta` | `U+001E` + `timedelta:` + `repr(value)` (openpyxl returns it for `[h]:mm:ss` cells) |
| anything else | an error; ingest stops |

A cell holding a **formula** is refused at ingest: openpyxl discards cached values when it saves, so
such a cell could never survive the F1 round trip.

## Consequences (deliberate)

- **Types matter.** A Year cell read as the number `2016` and one read as the text `"2016"` hash
  differently. That is correct: export must reproduce the original cell, not an equivalent one.
- **`None` and `""` are the same.** An empty cell and a cell holding the empty string hash alike.
- **Whitespace matters.** A trailing space, a non-breaking space or a changed newline changes the hash.
- **No reformatting.** Numbers are never rounded, padded or locale-formatted.
- **Reserved characters.** `U+001E` and `U+001F` may not occur inside a text cell (they are the type
  tag and the joiner); a workbook containing them is rejected loudly. This keeps the encoding
  unambiguous: two different rows cannot produce the same byte string.
- Text must be valid Unicode (a lone surrogate is rejected).

## Alignment

`row_id` is the 1-based position among the data rows, in sheet order. The workbook record
(`source_workbook`) stores the header row, so the sheet row of any `row_id` is
`header_row + row_id`. Rows are contiguous: a blank row inside the data is rejected at ingest
(trailing blank rows are ignored).

## What `rows` stores besides the hash

The text columns hold the cell's string verbatim (a non-string cell is stored as its canonical text
above, without the tag), and `year` is the integer parsed from the Year cell. These typed columns
are convenient for queries but are **not** sufficient to rebuild the original cell (`2016` versus
`"2016"`); the hash, checked against the original workbook, is what proves the source columns are
unchanged.

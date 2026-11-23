# Documents: text, OCR and page provenance (build 10)

How the `documents` stage turns a court PDF into text that an extraction span can be traced back
through, to a page and a box on the original. Spec F6; the capture policy "original bytes always
survive OCR" is spec section 7.

## What goes in

Every PDF the court stage captured for a row, found through `court_records_current`: a record's own
capture (a PIN report), the filings a RECAP docket lists, the capture ids in a state docket's
`basis_json.document_captures`, and any capture with `manual_source` (build 14's import). A capture
counts as a PDF when its body starts with `%PDF-` in the first kilobyte. The captured PDF in the
store is only ever read.

## The ladder, per page

Policy `ladder` in `[stages.documents]`; the bar is `documents.ocr_quality_threshold` (0.85). The
first candidate that meets the bar ends the page; otherwise every rung runs and the highest-scoring
candidate is kept (a tie goes to the earlier rung).

| Rung | Module | Method stored | Notes |
|---|---|---|---|
| 1 | `documents.pdfplumber` | `native` | words with boxes from the text layer. A page with no text and no ink is `blank` (not scored, not flagged) |
| 2 | `documents.docling` | `docling` | layout and structure, once per PDF, OCR switched off. Its JSON is stored; its text items are candidates for pages rung 1 missed. Boxes are item-level |
| 3 | `documents.ocrmypdf` | `tesseract` | OCRmyPDF `--skip-text` on a copy, once per PDF; words and boxes read from its text layer. Tesseract's confidence is not exposed, so the score omits that part |
| 4 | `documents.paddleocr` | `paddleocr` | the page image through PaddleOCR; word boxes are the recognised line's box shared out by character count |
| 5 | `documents.claude_transcribe` | `model` | the page image, to the model. Always flagged. Box is the whole page |

A module that is not installed, has no key, is over budget or is switched off is skipped with its
reason in `documents.methods_json`, and the next one runs. A page no rung could read is `failed`
(flagged). A page with an existing poor-quality text layer is not re-OCR'd by `--skip-text`; it
falls to PaddleOCR and the model, which read the page image.

**Model transcription.** `llm.transcription_model`, the system prompt `prompts/transcribe_page.md`
(its SHA-256 is in `llm_calls.prompt_sha256`), the page image at `documents.render_dpi` shrunk to
1568 px. `claude-sonnet-5-5` rejects `temperature` (and API version 2026-10-21 rejects it on every
model), so none is sent, `llm_calls.temperature` is NULL with the reason in `temperature_note`, and
thinking is turned off with `between_tools` at low effort. `llm.send_temperature = true` sends the
configured temperature for a model that accepts it. The call goes through `Capture.llm_message`
(rate-limited on `api.anthropic.com`, request and response stored as a capture, the image
replaced by its hash). A call is made only if this month's logged spend plus the call's worst-case
cost is within `llm.max_monthly_usd`; prices are `llm.price_*_per_mtok`.

## The page score, in [0, 1]

`efp/documents/quality.py`, `SCORE_VERSION = 1`. A weighted mean of the parts that can be measured,
renormalised over those present.

| Part | Weight | Measures | 0 at | 1 at |
|---|---:|---|---:|---:|
| dictionary | 0.40 | share of lowercase words of 3+ letters in `reference/wordlist.txt.gz` (capitalised words skipped as names unless the page has under 5 lowercase words) | 0.40 | 0.90 |
| charset | 0.30 | share of letters, digits, white space and ordinary punctuation, less replacement and control characters and runs of 4+ symbols, times the share of letters and digits | 0.50 | 0.95 |
| lines | 0.15 | share of lines with 2+ words or 10+ characters, less the excess of one-short-token lines over 30%. Absent under 3 lines | 0.30 | 0.90 |
| confidence | 0.15 | the engine's mean word confidence (PaddleOCR only) | 0.50 | 0.95 |

A page under 40 characters is scored on charset alone and marked `short`. A page with no text scores
0. The word list is `words_alpha.txt` from dwyl/english-words (public domain) plus
`reference/wordlist_legal.txt`; see `reference/wordlist.SOURCE.md`. It is pinned as the `wordlist`
instrument.

`documents.quality_json` holds, per page: method, score, its parts, flags, `short`, character count
and the page's span in the canonical text. `quality_min` is the lowest scored page (blank and failed
pages have no score).

## Flags

A page is flagged when it is below the threshold, when a model transcribed it, or when it has no
text (`failed`, `not_read` past `documents.max_pages`). `document_pages.flag_reasons` lists why. A
database CHECK makes a `model` or `failed` page impossible to store unflagged.
**`document_pages_extractable`** is the only view extraction (build 16) may read: current,
unflagged, not blank, scored. Flagged pages are in `document_pages_current` with their flag and
their text in the canonical text.

## The canonical text (version 1)

`efp/documents/canonical.py`, `CANONICAL_VERSION = 1` (`documents.canonical_version`).

* Pages in order joined by `\f`, no trailing one.
* Lines within a page, in reading order, joined by `\n`; a page with no words is the empty string.
* Words within a line joined by one space.
* Each word NFC-normalised, with zero-width characters (U+200B, U+200C, U+200D, U+2060, U+FEFF) and
  all internal whitespace removed; an empty word is dropped and so is an empty line.
* Offsets are Python string indices (code points). `text_sha256` is the SHA-256 of the UTF-8 text.

A change to any rule is a version bump; hashes are comparable only within one version.

## Storage layout

All under `paths.store`, by content hash, each as a capture (`captures` row, `stage = documents`,
`parent_capture_id` in `headers_json`):

| What | `captures.tool` | Referenced by |
|---|---|---|
| canonical text (`text/plain`) | `documents` | `documents.text_capture_id` |
| word map (JSON) | `documents_layout` | `documents.layout_capture_id` |
| post-OCR PDF | `ocrmypdf` | `documents.post_ocr_sha256` |
| Docling JSON (`export_to_dict`) | `docling` | `documents.docling_path` (the store path) |
| model request and response | `llm` | `llm_calls.capture_id` |

The word map is `{"canonical_version", "text_sha256", "pages": [{page, method, width, height,
char_start, char_end}], "units": [{page, bbox: [x0, top, x1, bottom], char_start, char_end, method,
bbox_source}]}`. Boxes are PDF points, origin top-left. `bbox_source` is `word`, `item` (a Docling
item), `line_split` (PaddleOCR) or `page` (a model transcription: the page's box). Resolving a span
is `canonical.resolve(layout, start, end)`: a binary search over `char_end`, returning one entry per
page with the union box of the words the span touches.

`documents.pre_ocr_sha256` is always the captured PDF's hash (the evidentiary one);
`post_ocr_sha256` is set only when the Tesseract text layer was used. A re-run that produces the
same text and page results adds nothing; otherwise it adds a new row and sets the old one
`current = 0`.

## Tables (migration 0010)

`documents` gains `text_capture_id`, `layout_capture_id`, `canonical_version`, `methods_json`,
`current`, and is append-only (only `current` 1 to 0 may change). `document_pages` is append-only.
`llm_calls` is append-only and is the ledger the monthly cap reads (build 16 writes to it too).

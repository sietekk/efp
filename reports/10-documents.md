# Build 10 — Documents: text, OCR and page provenance

Branch: `build/10-documents` (off `main`; builds 07 to 09 and both maintenance PRs are merged, nothing stacked) · PR: see the pull request for this branch · Date: 2026-11-22 · Model: claude-sonnet-5-5 · Prompt: `build/10-documents.md` · Spec sections: 1 (F6), 4 (documents), 7 (original bytes survive OCR), 10 (OCR ladder, model fallback), 12 (no PyMuPDF)

## What this build set out to do

Turn every PDF the court stage captured into text with page, bounding-box and character-span provenance: born-digital text with pdfplumber, Docling layout, OCRmyPDF (Tesseract), PaddleOCR, and Claude transcription for pages that fail all of those, with a quality score per page, a flag on every page the pipeline should not trust, the original bytes kept, and the canonical text stored as a capture.

Changes the approved plan made to the prompt, and two answers you gave at planning:

- **Temperature is never sent, by construction.** CLAUDE.md said temperature 0; API version 2026-10-21 rejects `temperature` (HTTP 400, confirmed live). `Capture.llm_message` and `llm.log_call` have no temperature parameter, the `send_temperature` flag is gone, `llm_calls.temperature` is always NULL with the reason, and CLAUDE.md now says so. Every call goes through `efp.llm.call_model`; a test enforces that no path can send one and pins the SDK's API version so an upgrade forces a re-check.
- **Docling and PaddleOCR download their own weights** (Hugging Face, Baidu) on first use, outside the capture layer: not logged, not rate-limited, not hash-pinned. You chose this at planning.
- **The documents ladder runs per page.** `run_chain` runs once per row, so the component calls it once per page (the same registry, `available` and `applies` records, policy `ladder`, with the quality bar as `ocr_quality_threshold`).
- **Docling is a separate `layout` extra** (it pulls PyTorch); the `documents` extra gained `anthropic`.
- **The model reads only pages OCR failed on** (tightened after review): see decision 4.
- **The word list is dwyl/english-words** (public domain), not SCOWL (SCOWL has no static download), plus a small legal list.

## What was built

- `efp/llm.py` (request shape, `call_model`, cost, the cap), `efp/documents/`: `__init__.py` (the component: finds a row's PDFs, runs the ladder per page, writes `documents`, `document_pages` and the derived captures, `input_hash` and the `:budget` deferral suffix), `modules.py` (the five rungs), `ladder.py` (per-PDF and per-page state), `pdfio.py` (words with boxes, page images, blank detection; pdfplumber and pypdfium2 only), `quality.py` (the page score), `canonical.py` (canonical text, word map, `resolve`), `docling_parse.py` (Docling JSON to lines of words), (model spend moved to `efp/llm.py`).
- `efp/capture.py`: `Capture.llm_message(...)`: one model call through the capture layer (takes the `api.anthropic.com` budget; the request, with images replaced by their hash, and the response are stored as captures; no key is stored) and `LlmResult`.
- `migrations/0010_documents.sql`: `documents` gains `text_capture_id`, `layout_capture_id`, `canonical_version`, `methods_json`, `current` and becomes append-only (only `current` 1 to 0 may change); new append-only `document_pages` (with a CHECK that a `model` or `failed` page cannot be unflagged) and `llm_calls`; views `document_pages_current` and `document_pages_extractable`. `efp/dbcheck.py` lists the new objects.
- `efp/status.py`: a Documents section (pages by method, documents by lowest page quality, flagged pages, unreadable PDFs, this month's transcription spend), in the Markdown and the JSON.
- `prompts/transcribe_page.md` (the transcription system prompt; hashed per call), `reference/wordlist.txt.gz` with `wordlist.SOURCE.md` and `wordlist_legal.txt`, registered as the `wordlist` instrument.
- Config (documented in `config.example.toml`): `documents.ocr_language`, `render_dpi`, `max_pages`, `ocr_timeout_seconds`, `transcribe_max_tokens`; `llm.send_temperature`, `price_input_per_mtok`, `price_output_per_mtok`.
- CI installs the `documents` extra and `tesseract-ocr` / `ghostscript`, so the OCR test runs there. `pyproject.toml`: `layout` extra, mypy overrides for the optional packages; `uv.lock` re-locked.
- Docs: `docs/documents.md` (the score, the canonical text, the storage layout), `RUNBOOK.md`.
- Tests: `tests/test_documents.py`, `tests/test_documents_model.py` and `tests/test_documents_review.py` (94 tests), `tests/pdfgen.py` (synthetic PDFs: born-digital, image-only, degraded, blank, mixed), `tests/documents_helpers.py`.
- Results: `uv run pytest -m "not live"`: 1093 passed. `ruff check`, `ruff format --check`, `mypy efp`: clean.

## Review findings (done-when-reviewer) and what was done

| Finding | Action |
| --- | --- |
| **`efp run documents --recheck` crashed**: `input_hash` had the wrong signature for the runner (`new_hash(row, ctx)`), so the `:budget` revisit path could never work | `input_hash(row, ctx, registry)` computes the row's PDFs itself; test `test_recheck_through_the_runner_is_a_noop_until_a_deferred_row_comes_due` drives the stage through the runner (no-op, then a `:budget` row is revisited) |
| **The model could read pages that never went through OCR** (no OCR installed, or a born-digital page with a low score such as a ledger of digits) | `documents.claude_transcribe` applies only when an OCR engine ran on the page, every installed engine in the chain has, and the page has no text layer. Tests: not called with no OCR, not called for a digital low-score page, waits for every installed engine |
| The evidentiary hash was copied from the capture row, not checked, and tools read the stored file in place | each PDF is read through `read_verified` (a mismatch is `unreadable`, the file untouched) and every tool works on a scratch copy; tests for a tampered body and for the copy |
| Only Python kept a below-threshold page out of extraction | an INSERT trigger refuses an unflagged page below the threshold recorded in the document's `quality_json`; the extraction view applies it too |
| A failed write could leave a partial document, and nothing kept one current row per capture | the `documents` / `document_pages` writes and the flip of the old row are one transaction (rolled back on error); a unique index allows one current row per capture; tests for both |
| Hidden SDK retries under one budget slot and one log row | `max_retries=0`: one call is one request, one slot, one row |
| The cap rested on a 2,500-token image estimate | 3,600 (a square 1,568 px page is about 3,300 tokens); cost test uses the constant |
| Faint scans were recorded as `blank` | the cut is 240 of 255 at 100 dpi; a pale scan is read or flagged, never `blank` (test with a pale-grey page) |
| The status spend line counted every model call | it counts `purpose = transcribe_page`; the cap still counts every purpose |
| **Found while fixing these: importing `ocrmypdf` broke pdfplumber for the rest of the process** (it monkeypatches pdfminer.six, so a standard-font PDF read as `(cid:72)(cid:101)...` and, because `cid` is a word, scored 0.98) | OCRmyPDF now runs as a subprocess and is never imported; the score treats `(cid:N)` text as unreadable; test runs OCR then reads a digital PDF in the same process. (The reviewer's single failing run in the full suite was this bug.) |
| The monthly cap could undercount: a timeout or dropped connection after the request went out was logged at $0, and an answer that could not be recorded was not logged at all | `Capture.llm_message` raises `LlmCallError` (billed or possibly billed) for both; the failure is logged at the real tokens when known, else the worst-case cost (`llm.failure_cost`); a plain HTTP error answer stays $0. Same in the live check. Tests for each |
| A transient unreadable result (a store or disk error, a parser error) replaced a good current document and the row was not retried | an earlier good text stays current (`kept_previous_text` in the summary); a store or disk error also gives the hash an `:unreadable` suffix so `--recheck` retries it, a corrupt PDF does not; tests |
| `call_model` could lose the cost of a billed call if the `llm_calls` write failed (and its docstring said it never raises) | the write is retried; if it still fails the call goes to a fallback ledger (`llm_unlogged.jsonl` in `paths.logs`), the answer is returned intact with `log_error`, the cap counts the ledger, and the next call moves it into `llm_calls`; docstring corrected; tests |
| A scan with a stamp in its text layer (the ECF header on a RECAP scan) scored ~1.0 as native text, met the bar, was unflagged and in `document_pages_extractable` with the body missing (and `--skip-text` and the model rung both skipped it) | a page whose picture covers most of it but whose text layer is under 10% of it is a *partial text layer*: native and Docling scores are capped at 0.5 and flagged `native_text_partial`; OCRmyPDF runs a second `--force-ocr` pass on just those pages; the model may read them; tests with a synthetic stamped scan (detection, flagged when nothing else reads it, real Tesseract reads the body, the model is allowed) |
| OCRmyPDF's 300 s limit was per whole document, too short for a long scan (every page then fell through with no text), and `ocr_timeout_seconds` was not in the hash, so raising it would not retry those rows | the limit is `ocr_timeout_seconds` plus `ocr_seconds_per_page` (30) x pages read; both are in the hash; tests for the limit (it uses `max_pages`, not the PDF's length) and the hash |
| A PDF that dropped out of a row's inputs kept a `current = 1` documents row, so extraction would still read it | `retire_dropped` flips it to `current = 0` at the end of `process_row` (also when no PDF is left); `retired_documents` in the summary; tests |
| The monthly cap was checked only by `ClaudeTranscribe.available`, not inside `call_model` | `call_model` checks `room_for_call` first and returns an error at zero cost without calling the API; test |
| Docling ran in the stage's process, so one page hanging inside its layout model (its `document_timeout` is checked only between pages) could stall the whole batch | Docling runs as a subprocess (`efp.documents.docling_worker`) under `run_group`: soft limit `docling_timeout_seconds` (keeps the pages it finished), hard kill of the process group 120 s later, recorded as `docling hung and was killed`; the ladder carries on. Not seen hanging on real PDFs (none yet); this is a guard. Verified live in the isolated environment; tests for the command, the kill and a failing worker |
| Docling boxes are coarse, and with no page size the box is the extent from the page origin | noted in `docs/documents.md` (never use an `item` box for highlighting or exact span mapping); no code change |
| **The recurring temperature errors, removed at the root** (a flag, a body workaround, three call sites that each shaped, logged and priced a call their own way) | the flag, the workaround and the parameter are deleted; `efp/llm.py` owns request shape, logging, cost and every failure kind; the stage and the live check both call `call_model`; the hash uses `llm.request_signature` (model, thinking, effort, `transcribe_max_tokens`, temperature policy, prompt hash) while the fallback is on; an SDK `TypeError` or a failed error-store is a failed call or check, never a crash; tests pin all of it |
| **Found by the live run, now moot:** `send_temperature = true` crashed (`TypeError`: the installed SDK has no `temperature` argument) | the value is sent in the request body (`extra_body`); the API then answers HTTP 400 for this model, which is logged as a normal failed call; test |
| The OCR comment named the wrong input; the hash left out `render_dpi`, `ocr_language`, the model and the prompt; an HTTP-error model call was logged without its stored request | comment corrected (the input is the verified scratch copy); the four settings are in the hash (the model and prompt hash only while the fallback is on); the error carries the capture id (`LlmHttpError`) and `llm_calls.capture_id` points at it |
| `max_pages` did not bound Docling or OCRmyPDF, and Docling had no time limit | both stop at `max_pages` (Docling `page_range`, OCRmyPDF `--pages`), checked live; new `documents.docling_timeout_seconds` (default 900) uses Docling's `document_timeout` and keeps the pages it finished. Very long filings are supported by raising `max_pages`; the default is unchanged |
| One current document per capture was not scoped to the row, so two rows pointing at one capture would take turns | the lookup and the unique index are on (`row_id`, `capture_id`); test with two rows sharing a capture |
| `doc.close()` was outside a `finally` | closed in a `finally`; test |
| `:tools` suffix made every row with a flagged page revisited on each `--recheck` when an optional extra (Docling, PaddleOCR) was absent | the suffix is gone: which rungs can run is already in the hash, so installing a tool changes it; only `:budget` remains |
| `docling_always` was not in the hash | it is: turning it on revisits every row, on purpose (documented) |
| `between_tools` thinking value unverified against the API (now **verified live**, decision 7) | the installed SDK's types name it (test); `efp selftest --live` now sends one real transcription-shaped call (synthetic image, a fraction of a cent, logged in `llm_calls` as `selftest`) when the fallback is enabled and the key is set. **I did not run it**: say if you want it run |
| Page images of court documents go to api.anthropic.com, and the fallback defaulted to on | `llm_transcription_fallback = false` in `config.toml` and `config.example.toml`; a test pins it; decision 4 says why and how to opt in |
| Each PDF was reopened for every page (quadratic on long filings) | `pdfio.PdfReader` keeps one handle per document (and one for the OCR copy), page caches flushed; test counts opens |
| A short OCR page (under 40 characters) scored on charset alone could pass unflagged | flagged `short_unverified` for Tesseract and PaddleOCR (a short text-layer page is exact and is not) |
| A model reply cut off at `max_tokens` was taken as a full page | flagged `truncated` (the partial text is kept) |
| Docling ran only after a page fell past pdfplumber, so a born-digital document got no structure | new `documents.docling_always` (default false): true runs Docling over every PDF; test with a fake Docling |
| `anthropic` had no version floor | `anthropic>=2` in the `documents` extra |

Left as built, disclosed: a `blank` method exists in the schema beyond the prompt's list (a page with nothing on it is not a failure); PaddleOCR also runs when Tesseract is absent, not only when its score is low; `pdfgen.degraded` is used only by the demonstration corpus, because Tesseract's output on it varies by version.

## What was run

| Run | Rows attempted | Rows resolved | Wall clock | Requests (per domain) | API spend |
| --- | --- | --- | --- | --- | --- |
| 50-row sample | 0 | 0 | n/a | 0 | $0 |
| All rows | 0 | 0 | n/a | 0 | $0 |
| Synthetic corpus on a scratch database (5 rows: born-digital, image-only, degraded, mixed, corrupt) | 5 | 5 | seconds | 0 | $0 |
| Real Docling on a synthetic 2-page PDF (isolated environment) | 1 | 1 | 26 s (cold) | model weights from Hugging Face | $0 |

No row in the real database has a PDF yet: nothing has run on real data (build 07 and 09 found no filings here), so `efp run documents --sample` could not be run on real rows. The commands and the status report are in place; the numbers come from running them after the court stage has captured filings.

The synthetic corpus gave: born-digital pages `native` at 1.0; the image-only page `tesseract` at 1.0; the mixed document `native`, `tesseract`, `blank`; the degraded scan `failed` and flagged (Tesseract read nothing, no other rung installed), with the row hash suffixed `:tools`; the corrupt PDF `unreadable` with the row still `done`.

## Results by source

| Source | Rows attempted | Hits | Misses | Errors | Notes |
| --- | --- | --- | --- | --- | --- |
| `documents.pdfplumber` | 5 (synthetic) | 4 | 0 | 1 (corrupt PDF) | the only rung that ran for a born-digital page |
| `documents.docling` | 1 (synthetic, isolated env) | 1 | 0 | 0 | not installed in the default environment: skipped with its reason |
| `documents.ocrmypdf` | 3 (synthetic) | 2 | 1 | 0 | Tesseract present on this machine |
| `documents.paddleocr` | 0 | 0 | 0 | 0 | not installed; parser tested on synthetic output only |
| `documents.claude_transcribe` | 0 live | 0 | 0 | 0 | tested only against a stubbed client; no live call was made |

## Failures by reason

| Reason | Rows | Example row_id | Action taken |
| --- | --- | --- | --- |
| Corrupt or encrypted PDF | 1 (synthetic) | 5 | recorded as `unreadable` in `quality_json`; the row still ends `done` |
| No rung could read a page | 1 (synthetic) | 3 | page `failed`, flagged `no_text`; `--recheck` revisits it once a tool is installed (the available rungs are in the hash) |

## Gated sources encountered (not worked around)

None. Model transcription needs `ANTHROPIC_API_KEY` (self-serve) and is switched off without it.

## Numbers for the first-pass report

Not measurable yet: no real rows have PDFs. The status report gives, once they do: pages by method (native / docling / tesseract / paddleocr / model / failed / blank), documents by lowest page quality, flagged pages by reason, unreadable PDFs by reason, and this month's model transcription spend against the cap.

## Spot-check guide for the operator

- After the first real run, `efp status` (Documents section). A high share of `failed` or `model` pages means a tool is missing (the module records say which) or the scans are poor.
- Open ten flagged pages: `SELECT * FROM document_pages_current WHERE flagged = 1`, find the PDF through `capture_id`, and compare the page image with the text between `char_start` and `char_end` of the document's `text_capture_id` text. The score thresholds (below) are first-pass constants; this is how to check them.
- Spot-check three `model` pages against the image: model transcription errors are silent.

## Decisions needed from the operator

1. **The temperature rule (CLAUDE.md) cannot be met as written**: the API rejects it. Taken, per your instruction to solve it permanently: never send it, log NULL and the reason, and CLAUDE.md now says so. Say if you would rather the rule stand and a different provider or API version be used.
2. **Model weights come from the internet outside the capture layer** (raised again in review; your planning answer stands, so no change was made) (Docling from Hugging Face, PaddleOCR from Baidu's mirror): not logged, rate-limited or hash-pinned. Default taken (your planning answer): allowed. Say if the weights should instead be fetched once, hashed and pinned.
3. **The score constants are first-pass.** The weights, the mapping ranges and the 0.85 threshold are in `docs/documents.md` and `quality.py`; the word list is a general English list, not a legal one. Default: as documented. Calibrate on the first 50 real pages you spot-check.
4. **Model transcription is off by default** (`llm_transcription_fallback = false`). It sends page images of court documents (names, addresses, dockets) to api.anthropic.com, and CLAUDE.md keeps row-level identity data under `~/efp-data/` and says to take the option that sends less; the spec and plan do name this fallback, so it is built and one flag away. When on: only a page an installed OCR engine failed on that has no text layer, within `llm.max_monthly_usd` (150); with no OCR installed it never runs (pages `failed`, flagged, revisited by `--recheck` once an OCR engine is installed). Default taken: off. Say if you opt in, and whether it should then also run when no OCR engine is installed.
5. **A page with an existing poor text layer is not re-OCR'd** (`--skip-text`, as the prompt specifies): it goes to PaddleOCR and the model. Default: as specified.
6. **Docling is a separate extra and not in CI**; PaddleOCR (`ocr-fallback`) is untested live here (paddlepaddle is not in the lock). Default: both are skipped with a reason when absent.
7. **Checked live (you approved a run), 2026-11-22.** `efp.livecheck._model_live` sent one transcription-shaped call (synthetic image, scratch database, nothing from `~/efp-data`) and it succeeded: **`claude-sonnet-5-5` is a valid model id** (build 01 decision 6 is answered for the transcription model), `thinking: {"type": "between_tools"}` with `output_config.effort = "low"` and no temperature are accepted, 456 input and 13 output tokens, $0.001. A second call with `send_temperature = true` came back **HTTP 400 `temperature: Extra inputs are not permitted`**, which confirms decision 1 with evidence. The extraction model id in `llm.model` is the same string and was not exercised for extraction (build 16).

## Operator decisions

1. Decision: ______ · Date: ______
2. Decision: ______ · Date: ______
3. Decision: ______ · Date: ______
4. Decision: ______ · Date: ______
5. Decision: ______ · Date: ______
6. Decision: ______ · Date: ______
7. Decision: ______ · Date: ______

## Carried forward

- 16: read text only for pages in `document_pages_extractable` (current, unflagged, not blank). The canonical text (`documents.text_capture_id`) holds flagged pages too, so restrict a read to the `char_start`..`char_end` spans of extractable pages; a span resolves to pages and boxes with `canonical.resolve(layout, start, end)` over the capture at `documents.layout_capture_id`.
- 16: apply the threshold in force (`documents.ocr_quality_threshold`) as well as `document_pages_extractable`: the view holds a page to the threshold its document was read with, so a threshold raised since needs the new value or a `--recheck`.
- 16: every model call is a row in `llm_calls` (model id, prompt hash, tokens, cost) and counts against `llm.max_monthly_usd` through `efp.documents.llm.room_for_call`; `Capture.llm_message` is the call path (no temperature is sent for claude-sonnet-5-5; thinking is `between_tools`).
- 16: read the cap with `llm.spent_this_month(conn, config=config)` (it includes the fallback ledger); `CallResult.log_error` means the answer is valid but its ledger row is in `llm_unlogged.jsonl`.
- 16: make every model call with `efp.llm.call_model` (it shapes the request, never sends a temperature, logs every outcome, and prices a possibly billed failure at its tokens or the worst case); do not call `Capture.llm_message` directly. Failed ones are logged through `efp.llm.failure_cost` (an HTTP error answer is $0; `LlmCallError` is billed or possibly billed and is logged at its tokens or the worst case), so the cap never undercounts.
- 16: no SDK retries are made (`Capture.llm_message` is one request); a failed call is a logged row with `ok = 0` and the page is revisited by a later run.
- 16: a flagged page may be read only through `document_pages_current`, and its extraction must carry the flag.
- 14: a PDF captured with `manual_source` for a row is read by the documents stage with no further wiring.
- 12, 16: the docket evidence text from build 09 (`state_docket_row`) is not a PDF and is not read here.
- 10, 21: the canonical text rule is version 1; a change is a version bump recorded in `documents.canonical_version`, and `text_sha256` is comparable only within a version.
- 01: the `layout` extra holds Docling; any environment that runs the documents stage with layout needs `uv sync --extra documents --extra layout` and, for OCR, `tesseract` and `ghostscript` on the PATH.
- All: `documents.current`, `document_pages` and `llm_calls` are append-only; read documents through `document_pages_current` / `document_pages_extractable`, never the `documents` table alone.

## Done-when check

- "`uv run efp run documents --sample` then the full run produce text and page-level quality flags for every captured filing; `efp status` reports the method and quality distributions." — **Holds for the code, not measured on real rows.** The stage, the per-page flags and the status section are built and tested (`test_a_born_digital_pdf_is_read_by_pdfplumber_alone`, `test_an_image_only_pdf_is_ocrd_and_the_original_bytes_survive`, `test_status_reports_methods_quality_bands_flags_and_spend`) and were run over a synthetic corpus on a scratch database. No real row has a PDF, so no real sample or full run was possible here.
- "A test proves no page below the threshold can reach extraction unflagged." — **Holds.** `tests/test_documents.py::test_no_page_below_the_threshold_reaches_extraction_unflagged` over a mixed corpus (native, model, failed, below-threshold pages), plus the CHECK on `document_pages` (a `model` or `failed` page cannot be stored unflagged).
- "No PyMuPDF anywhere in the dependency tree (assert in a test over `uv.lock`)." — **Holds.** `tests/test_documents.py::test_pymupdf_is_nowhere_in_the_code_or_the_lockfile` (and the existing lockfile test in `tests/test_layout.py`).

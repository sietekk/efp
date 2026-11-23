# prompts/

`transcribe_page.md` (build 10) is the system prompt for model transcription of a page the OCR
ladder could not read; its SHA-256 is written to `llm_calls.prompt_sha256` on every call.

Reserved for the extraction prompt files (spec section 11): one file per extraction class
(`stated_motive.md`, `political_role.md`, `organization.md`, `beneficiary.md`, `identifier.md`),
written in build 16, under version control, and hashed per model call into `extractions.prompt_sha256`.

The Claude Code build prompts live in `build/`, not here, so that nothing in this folder is ever
mistaken for an instrument the pipeline hashes.

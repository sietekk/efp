# Runbook: building the pipeline with Claude Code

Everything below happens on your Mac, in bash (every snippet here and in `BUILD.txt` is bash; if a tool is not found after install, add `export PATH="$HOME/.local/bin:$PATH"` to `~/.bash_profile`). Budget an hour for setup, then one build prompt at a time. This folder is the repository's starting state; it lives at `~/dev/efp` (it started in `~/Documents`, an iCloud-synced folder, which is why it moved; see the README).

## 0. Prerequisites (one time)

```bash
# Homebrew packages the pipeline needs locally
brew install uv gh tesseract ghostscript qpdf
# Docker Desktop is optional (Browsertrix and Auto Archiver only); skip until build 09 or 20 asks for it.

# Claude Code
curl -fsSL https://claude.ai/install.sh | bash     # or: npm install -g @anthropic-ai/claude-code
claude --version
```

Sign in to GitHub once (`gh auth login`) and to Claude Code once (run `claude`, then `/login`). The exact command sequence for every step below, ready to paste, is in `BUILD.txt`.

## 1. Turn this folder into the repository

Keep the folder outside iCloud-synced locations (macOS Desktop and Documents): the sync marks
files in `.venv` hidden, which breaks the install, and creates conflict copies. The project lives
at `~/dev/efp`.

```bash
cd ~/dev/efp
git init -b main
git add -A
git commit -m "Kit: plan, spec, build prompts, CLAUDE.md"
gh repo create efp --private --source=. --push
```

Then on GitHub: Settings → Branches → Add branch protection rule for `main` → "Require a pull request before merging". Claude Code opens pull requests; you merge.

## 2. Data folder and secrets (outside the repo)

```bash
# FileVault must be on: System Settings → Privacy & Security → FileVault
mkdir -p ~/efp-data/{source,store,manifests,bags,bulk,exports,logs} ~/.config/efp
chmod 700 ~/.config/efp
cp .env.example ~/.config/efp/.env && chmod 600 ~/.config/efp/.env

# The Heritage workbook, byte-for-byte, with its hash
cp "/path/to/heritage-voter-fraud-map-search-20260406150111.xlsx" ~/efp-data/source/
(cd ~/efp-data/source && shasum -a 256 *.xlsx > MANIFEST.txt && cat MANIFEST.txt)
```

## 3. Self-serve keys (no person involved)

Paste each into `~/.config/efp/.env`:

| Variable | Where to get it |
| --- | --- |
| `COURTLISTENER_TOKEN` | courtlistener.com → create a free account → profile → API token |
| (no key) | Bluesky's public AppView needs none; Reddit, Meta and X remain gated (applications or paid) and are listed, not used |
| `FEC_API_KEY`, `CONGRESS_API_KEY` | api.data.gov/signup (one key serves both; paste it into both lines) |
| `OPENSTATES_KEY` | open.pluralpolicy.com → account → API key |
| `IA_ACCESS`, `IA_SECRET` | archive.org → account → "S3-like API keys" (archive.org/account/s3.php) |
| `MEDIACLOUD_KEY` | search.mediacloud.org → sign up → profile → API token |
| `ANTHROPIC_API_KEY` | console.anthropic.com → API keys |
| `GOOGLE_CSE_KEY`, `GOOGLE_CSE_ID` | optional; programmablesearchengine.google.com plus a Google Cloud API key |

Leave anything you skip blank. `efp selftest` reports which keys are present. `PACER_USER`/`PACER_PASS` and `PERMA_API_KEY` stay blank in the first pass.

Also copy `config.example.toml` to `config.toml` in the repo and put your UNH address on the two `YOUR-UNH-ADDRESS` lines (it goes in the `User-Agent` of every request, as the spec requires). `config.toml` holds no secrets and is committed.

## 4. Put the other three documents in `docs/`

`docs/plan.md`, `docs/spec.md`, `docs/summary.md` and the figures are here. Export these three from their artifacts (open the doc → export → Markdown; the links are in `docs/README-docs.md`) and save them as:

- `docs/tasks.md` ← Election Fraud Pipeline Task List
- `docs/research-plan.md` ← Election Fraud Ideology Coding: Research Plan (main tab)
- `docs/tool-register.md` ← the Research Plan's "Tool register" tab

Commit them to `main`. Claude Code reads all of `docs/`. Build 18 (coding) cannot run without the research plan; builds 01 and 12 lean on the tool register and the task list.

## 5. The first build

```bash
cd ~/dev/efp && git checkout main && git pull
claude --permission-mode plan
```

Paste the contents of `build/01-scaffold.md` as your first message. Claude Code reads `CLAUDE.md`, the index, the plan and the spec sections the prompt names, and proposes its approach. Read the proposal, then approve it (Claude Code switches out of plan mode to implement) or redirect it. When it finishes it will have:

- a branch `build/01-scaffold` pushed with a pull request,
- `reports/01-scaffold.md` filled in,
- `uv run pytest`, `ruff` and `mypy` passing.

Your review: read the report, run the "Done when" commands yourself, merge the PR.

Pick the model with `/model` if you want to change it. Plan mode toggles with Shift+Tab inside a session.

## 6. Every later build

```bash
cd ~/dev/efp && git checkout main && git pull
uv sync
claude --permission-mode plan
```

Paste `build/NN-<name>.md`. The order and the session bundles are in `build/README.md`; the "Needs" column says which reports must exist first.

- A long run (links, court record, news) keeps going after you close the terminal only if you start it detached: `nohup uv run efp run <stage> > ~/efp-data/logs/<stage>.log &`. Read progress with `uv run efp status` (add `--format json` for a machine-readable form) rather than watching the log.
  - Two runs of the same stage never claim the same row: each claims its batch in one transaction and marks it `running`. A run that is killed leaves its batch `running`; the next run of that stage resets rows older than `runner.stale_after_minutes` (120) to `pending` without counting the interrupted attempt, or `--recover` does it at once (use `--recover` only when no other run of that stage is alive, or its rows are processed twice).
  - `--dry-run` lists the rows a run would claim and why a stage has none (it names the predecessor). `--sample` limits a run to the frozen 50 rows. A row that failed three times stays `failed`; `--retry-failed` gives it one more attempt. `--reset` sets the selected rows back to `pending` (it never deletes a capture); `--recheck` revisits `done` rows and skips those whose inputs have not changed; `--force` reruns them regardless.
  - `uv run efp modules [--stage S]` lists every configured module and whether it can run right now; the unavailable ones, with their reasons, are the gated-source list for the report.
- Your spot-checks: `uv run efp review <stage> --sample 10` walks ten random rows with their captures.
- If a build stalls on a gated source, that is expected: it is listed in the report and the run continues on other rows.
- Nothing waits for you. Builds 12, 16, 17 and 21 each have a part that is yours (labeling 150 identity pairs, the 50-row extraction tally, double coding, the 100-row review); they build the tooling for it and run in provisional mode until you do it, then `efp identity refit`, `efp validate`, the fix cycle and a re-tag pick your input up and re-run only the rows it changes. If you want the extraction volume run held until you have looked at the sample, set `stages.extract.require_sample_approval = true` in `config.toml`.
- If you have not merged the previous PR when you start the next build, Claude Code stacks the new branch on it and says so; merge in order.
- Build 15 (social) is in the first pass (session S5).

## 7. Where things land

| What | Where |
| --- | --- |
| Build reports | `reports/NN-<name>.md` |
| First-pass numbers and queues | `reports/first-pass/`, `queues/` |
| Coverage matrix, methods appendix, license register, first-pass notes | `docs/` |
| Database, captures, manifests, bags, exports | `~/efp-data/` (never in the repo) |
| Logs | `~/efp-data/logs/` |

## 8. The same thing from the Claude desktop app

If you would rather not use a terminal: Claude desktop app → Code → open the `efp` folder → paste the build prompt. It is the same Claude Code with a window around it; `CLAUDE.md` and the prompts work unchanged.

## 9. Resetting

Everything in `~/efp-data/` is reproducible from the workbook and the captures. To rerun a stage from scratch: `uv run efp run <stage> --reset` (it re-reads captures from the store; it does not re-fetch). To start the whole thing over: delete `~/efp-data/pipeline.db` and run `efp ingest` again. The store is never deleted by the pipeline.

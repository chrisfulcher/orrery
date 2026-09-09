# CLAUDE.md

mentor is a free, open source, self-hosted business development intelligence tool for U.S. federal government contracting. Read `docs/DESIGN.md` before proposing anything; it is the source of truth and this file is its condensation. When they disagree, the design doc wins and this file gets fixed.

## Non-negotiables

- **Zero telemetry.** No outbound network calls except to SAM.gov, USAspending (awards, no key), and endpoints the user has configured. No analytics, crash reporting, or update checks, ever.
- **Bring your own keys.** Credentials live only in the user's configuration. Never log them, never persist them elsewhere, never send them anywhere but the service they belong to.
- **Public data and public knowledge only.** Nothing proprietary to any organization enters the repository, including in fixtures, tests, examples, and comments.
- **People only in their official public capacity,** from government sources. No adapter touches personal social media or profiles individuals beyond their public role.
- **Every stored fact carries provenance:** source, observation time, and confidence. A fact without a source is not stored.
- **No third-party code loading in v1.** The MCP server is the only extension surface. Do not add a plugin loader.
- **Self-hosted, one `docker compose` command** (`run` today; `up` once the ingestion daemon exists). Keep the install path readable by a security reviewer, and keep the build-from-source route working.

## Data rules

- ELT: keep the verbatim `raw_json`; typed columns are a re-runnable parse.
- The identity and ingestion layers are append-only. Nothing is deleted; a notice that disappears upstream gets `active = false` and its `last_seen_at` stops advancing.
- Every ingested row resolves to an entity by exact key or records an unresolved alias. Resolution is never silent and never destructive.
- `facts` rows are never updated in place; a correction is a new fact with a later `observed_at`.
- Derived layers (FTS, embeddings, trends) must be rebuildable from ingestion tables at any time.
- Every workspace table carries `user_id`, even while v1 is single-user.
- All timestamps are UTC.
- Every keyed SAM.gov API request (search pages, notice descriptions) is logged in `api_requests`, attributed to an `ingestion_runs` row, and counted against the daily quota; the daemon stops before the quota does. Attachment files are public URLs that need no key and are not counted, but fetching them is a prioritized, politely rate-limited queue, never a crawl.
- `notices.description` is plain text from every source. Attachment paths are stored relative to `MENTOR_DATA_DIR`. Fetch failures are recorded, never retried automatically.
- `embeddings` rows record the model that produced them and are rebuilt by deleting a model's rows and re-running `mentor embed`. Embedding and semantic search send text only to `MENTOR_EMBED_BASE_URL`; notice and attachment text is public, the user's query text is not, and the README says so.
- Bulk-extract rows never overwrite an API-sourced notice; they confirm it and fill an unfetched description. `notices.source_id` is the source that last wrote the typed columns. `active` is cleared only by a complete pass of the active extract, only within the configured NAICS slice.
- `contracts` rows are keyed by USAspending's award key and overwritten on re-sight; awards resolve to existing offices by office code only (never creating agencies or offices) and to contractors by UEI (created on first sight; the CAGE is set only while no other entity holds it). Vendor phone, fax, and compensated-officer columns are dropped before `raw_json` is stored.
- Registration attributes are `sam.*` facts on the contractor, never columns; `entity_registrations` gets a row only when the record changed, with every point-of-contact field blanked; an expired registration is a fact, not a deletion. Only the slice is read from the extract: known contractors, configured primary NAICS, and the user's own UEI.
- Chat models are reached only through `src/mentor/ai/` and only at the endpoints configured under `MENTOR_AI_*` (a local Ollama by default); the profile, pursuit notes, and public notice and attachment text are what a request carries, and the README says so. `assessments` rows are append-only and carry their provenance (slot, provider, model, prompt version, profile version, inputs hash, tokens). The MCP server never contacts a model or the network.
- `notice_summaries` rows carry their provenance (slot, provider, model, prompt version, inputs hash, tokens, raw answer), are never updated, record a failed validation as a row with a null `result` that is not retried under the same model and prompt version, and are rebuilt like `embeddings` by deleting a model's rows and re-running `mentor summarize`. The prompt carries the notice only, never the profile; the set-aside fit against the profile is derived at read time, never stored.
- Workspace rows are the user's own and may be updated and deleted, with these exceptions: `workspace_documents` (the profile and the workflow as TOML) are versioned and append-only, every save a new version; pursuits are never deleted (a dropped pursuit is closed `no-bid`, and may be reopened); a pursuit's stage changes only through a recorded decision with its rationale (gate go, no-go, hold; back; reopen; outcome), and `pursuit_events` is append-only; a workflow stage key in use by an open pursuit cannot be removed; government dates (response deadlines, the incumbent's period of performance) are read from linked notices and contracts at query time, never copied into tasks. The legacy `tracked_*` tables are frozen. Every workspace query filters by `user_id`.

## Workflow

- Plan before code. Non-trivial work starts with a plan that names the design doc section it serves, and the plan is reviewed before implementation.
- Small commits. Commit before any ambitious change; git is the undo button.
- Every feature ships with tests.
- Read every diff. Prefer the simpler design; "make it simpler" is the highest-value review comment.
- Sign off every commit (`git commit -s`) for the DCO.

## Stack

- Python 3.13, pinned in `.mise.toml` together with uv. Dependencies live in `pyproject.toml` with a committed `uv.lock`; add a dependency only in the commit that first uses it.
- Source layout `src/mentor/`, tests in `tests/`, CLI entry point `mentor` (typer).
- Interfaces: terminal UI with Textual (`mentor top`, `src/mentor/tui/`), CLI with `--json` on every command that prints data, MCP server (`src/mentor/mcp_server.py`, stdio, interface version 1, tools only added, nothing that spends quota), and versioned read-only SQL views named `v_*` (interface version 1, columns only added). All of them go through one query module (`src/mentor/query.py`); no front end reads the raw tables directly, and the raw tables are not a public interface.
- Long-running operations (ingest, fetch, extract, embed, summarize, assess, db) are jobs in `src/mentor/jobs.py`; the CLI and the app both run them through it, and adapters take `report` and `cancelled` hooks from `src/mentor/progress.py`. The app runs a job on its own connection in a thread; its own connection never leaves the event loop.
- Storage is stdlib `sqlite3`, no ORM. Schema changes are numbered SQL files in `src/mentor/migrations/`, applied in order by `mentor db migrate`, and mirrored in `docs/DESIGN.md` §8 in the same commit.
- Commands: `uv sync` (install), `uv run mentor` (CLI), `uv run pytest` (tests), `uv run ruff check . && uv run ruff format --check .` (lint and format). All four must pass before a commit.
- Container: `docker compose build` and `docker compose run --rm mentor <command>`. The Dockerfile installs only from the lockfile (`uv sync --frozen`), never pulls an application image from a registry, and runs as a non-root user with the store at `/data`.

## Repo hygiene

- No secrets. `.env` files are gitignored; keep them that way.
- `.env` is written only by `src/mentor/dotenv.py`: only `MENTOR_*` keys, comments and order preserved, atomic replace (in place on a bind mount), mode 0600, never logged. Real environment variables still override it and the app shows those fields as locked.
- No local databases or downloaded attachments committed.
- No employer-, customer-, or person-specific data anywhere. Synthetic or public sample data only.
- `docs/` holds the design. The rest of the layout is defined with the stack.

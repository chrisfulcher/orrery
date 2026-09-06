# CLAUDE.md

mentor is a free, open source, self-hosted business development intelligence tool for U.S. federal government contracting. Read `docs/DESIGN.md` before proposing anything; it is the source of truth and this file is its condensation. When they disagree, the design doc wins and this file gets fixed.

## Non-negotiables

- **Zero telemetry.** No outbound network calls except to SAM.gov and endpoints the user has configured. No analytics, crash reporting, or update checks, ever.
- **Bring your own keys.** Credentials live only in the user's configuration. Never log them, never persist them elsewhere, never send them anywhere but the service they belong to.
- **Public data and public knowledge only.** Nothing proprietary to any organization enters the repository, including in fixtures, tests, examples, and comments.
- **People only in their official public capacity,** from government sources. No adapter touches personal social media or profiles individuals beyond their public role.
- **Every stored fact carries provenance:** source, observation time, and confidence. A fact without a source is not stored.
- **No third-party code loading in v1.** The MCP server is the only extension surface. Do not add a plugin loader.
- **Self-hosted, one `docker compose up`.** Keep the install path readable by a security reviewer, and keep the build-from-source route working.

## Data rules

- ELT: keep the verbatim `raw_json`; typed columns are a re-runnable parse.
- The identity and ingestion layers are append-only. Nothing is deleted; a notice that disappears upstream gets `active = false` and its `last_seen_at` stops advancing.
- Every ingested row resolves to an entity by exact key or records an unresolved alias. Resolution is never silent and never destructive.
- `facts` rows are never updated in place; a correction is a new fact with a later `observed_at`.
- Derived layers (FTS, embeddings, trends) must be rebuildable from ingestion tables at any time.
- Every workspace table carries `user_id`, even while v1 is single-user.
- All timestamps are UTC.
- Every SAM.gov API request is attributed to an `ingestion_runs` row and counted against the daily quota. The daemon stops before the quota does. Attachment fetching is a prioritized queue, never a crawl.

## Workflow

- Plan before code. Non-trivial work starts with a plan that names the design doc section it serves, and the plan is reviewed before implementation.
- Small commits. Commit before any ambitious change; git is the undo button.
- Every feature ships with tests.
- Read every diff. Prefer the simpler design; "make it simpler" is the highest-value review comment.
- Sign off every commit (`git commit -s`) for the DCO.

## Stack

- Python 3.13, pinned in `.mise.toml` together with uv. Dependencies live in `pyproject.toml` with a committed `uv.lock`; add a dependency only in the commit that first uses it.
- Source layout `src/mentor/`, tests in `tests/`, CLI entry point `mentor` (typer).
- Storage is stdlib `sqlite3`, no ORM. Schema changes are numbered SQL files in `src/mentor/migrations/`, applied in order by `mentor db migrate`, and mirrored in `docs/DESIGN.md` §8 in the same commit.
- Commands: `uv sync` (install), `uv run mentor` (CLI), `uv run pytest` (tests), `uv run ruff check . && uv run ruff format --check .` (lint and format). All four must pass before a commit.

## Repo hygiene

- No secrets. `.env` files are gitignored; keep them that way.
- No local databases or downloaded attachments committed.
- No employer-, customer-, or person-specific data anywhere. Synthetic or public sample data only.
- `docs/` holds the design. The rest of the layout is defined with the stack.

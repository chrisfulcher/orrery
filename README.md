# mentor

*Working title.* Free, open source, self-hosted business development intelligence for U.S. federal government contracting.

## Status

Early, and usable from the command line. The first feature loop works: ingest a NAICS slice of SAM.gov notices, fetch their descriptions within the API quota and their attachments outside it, extract PDF text, and search across all of it, by keyword or by meaning through an embedding endpoint you choose. Saved searches, an opportunity pipeline with PWin history, and your company profile are in. The Dockerfile and compose file are new.

The terminal UI, `mentor top`, is the primary interface; the MCP server and the read-only SQL views are in. Not there yet: the second and third data sources (entity registrations, USAspending awards) and, with them, the incumbent and award history panels of the context view. The design lives in [`docs/DESIGN.md`](docs/DESIGN.md); §9 is the order of work.

## Why

Federal opportunity, award, and spending data is public. The tools that make it useful are priced for enterprise budgets, well beyond the reach of the small businesses that make up most of the market and that the government's own set-aside programs exist to serve.

mentor is free to self-host, free of telemetry, and community-owned. A free, self-hosted alternative is structurally hard to answer without giving up that pricing.

The ambition is larger than a better search over solicitations. mentor is built on an entity graph of agencies, offices, contractors, contracts, and officials, onto which opportunity data, award history, entity registrations, budgets, and the user's own company profile are all resolved, with every fact carrying its source. The product is the context view over that graph: an opportunity page that already knows the incumbent, the contract history, the contracting officer's other awards, and the agency's budget line.

## Principles

- **Shipped code stays free and forkable.** Guaranteed by a permissive license, not by a pricing page.
- **Self-hostable by design.** One `docker compose up` on a home server or a cheap cloud box.
- **Zero telemetry.** The software phones home to nobody.
- **Bring your own keys.** You supply your own SAM.gov API key and LLM credentials; the project never touches credentials, billing, or inference costs.
- **Model-agnostic AI.** Every AI feature talks to a configurable OpenAI-compatible endpoint, local or cloud, your choice.
- **No hosted service in v1.** No accounts, no uptime obligation.
- **Public data and public knowledge only.** Proprietary capture intelligence and any organization's internal process never enter the repository.
- **People appear only in their official public capacity,** from government sources. No scraping of personal social media, no profiling beyond the public role.
- **Every fact carries provenance.** Source, observation time, and confidence, or it is not stored.

## What v1 will do

SAM.gov already offers keyword search and email alerts over notice metadata. What it cannot do is search inside the attached solicitation documents, where the statements of work, Section L/M instructions, and amendments live. v1 does exactly that for a NAICS and agency slice you choose, within the SAM.gov API quota: ingest notices, fetch and extract their attachments, and offer full-text and semantic search across all of it. On top of that: saved searches, an opportunity pipeline with PWin history, your company profile, a full-screen terminal UI that also runs in a browser tab, and an MCP server so any AI agent can work over the store. The terminal UI is the primary interface, in the idiom of system monitors like btop. Every command also prints JSON, and the database exposes stable read-only views, so your own dashboards are a first-class way to use it; Datasette over the database file gives a point-and-click browser for free.

v1 deliberately does not include a hosted service, accounts, telemetry, third-party plugins, a bespoke web UI, a wholesale mirror of SAM.gov, or enrichment sources beyond SAM.gov. The graph tables exist from the first migration; the adapters that fill them from other sources come after.

## Before you start

**SAM.gov access.** The API quota depends on how your key was issued, and the difference is the difference between a working tool and a broken one:

1. Register your entity on SAM.gov and obtain a Unique Entity ID (UEI). Registration alone can take up to 10 business days, so start it first.
2. Obtain a role on that entity registration for your SAM.gov user account. The exact role name is still being confirmed and will be stated here once verified.
3. Request your public API key from your SAM.gov account profile.

A personal key with no role is limited to roughly 10 requests per day. A key backed by a role on an active entity registration gets roughly 1,000. Each search page and each notice description is one request; attachment files download without a key and do not count. Ten a day is enough to try a narrow slice, and a thousand is the budget the tool is designed around.

**An embedding endpoint** (optional; needed only for `mentor embed` and `mentor search --semantic`). Any OpenAI-compatible `/embeddings` endpoint works. The default is a local [Ollama](https://ollama.com): install it, run `ollama pull nomic-embed-text`, and the defaults (`MENTOR_EMBED_BASE_URL=http://localhost:11434/v1`, `MENTOR_EMBED_MODEL=nomic-embed-text`, no key) already point at it. For a cloud endpoint set the base URL, the model name, and `MENTOR_EMBED_API_KEY`.

What leaves your machine: `mentor embed` sends the text of your ingested notices and attachments (public SAM.gov data) to that endpoint, and `mentor search --semantic` sends your query text, which may reveal what you are pursuing. With the local default nothing leaves the machine. Nothing is ever sent anywhere else.

## Quickstart

Both paths start from a `.env` file. Copy the example and set `MENTOR_SAM_API_KEY` (see [Before you start](#before-you-start)) and `MENTOR_NAICS`; the other settings are documented in the file and default sensibly.

```
cp .env.example .env
```

### From source

Requires Python 3.13 and uv; `mise install` provides both from `.mise.toml`.

```
uv sync
uv run mentor db migrate
uv run mentor ingest notices      # yesterday's notices for your NAICS codes; one request per page
uv run mentor ingest bulk         # today's full extract (~250 MB, no key, no quota), your NAICS codes only
uv run mentor fetch --dry-run     # what would be fetched, and today's remaining budget
uv run mentor fetch               # descriptions within the budget, then attachments (free)
uv run mentor extract             # PDF text; spends no quota
uv run mentor embed               # optional: chunk and embed via your endpoint
uv run mentor search "statement of work"
uv run mentor search --semantic "on-site help desk staffing"
uv run mentor quota
```

### With Docker

The image is built from the repository's own `Dockerfile`, which installs only what `uv.lock` pins. Building fetches the `python:3.13-slim` base image and the locked packages from PyPI, nothing else. There is no daemon yet, so each command runs in a throwaway container; `docker compose up` is not the verb.

```
mkdir -p data                     # mounted at /data inside the container
docker compose build
docker compose run --rm mentor db migrate
docker compose run --rm mentor ingest notices
docker compose run --rm mentor ingest bulk
docker compose run --rm mentor fetch --dry-run
docker compose run --rm mentor fetch
docker compose run --rm mentor extract
docker compose run --rm mentor embed
docker compose run --rm mentor search "statement of work"
docker compose run --rm mentor search --semantic "on-site help desk staffing"
docker compose run --rm mentor quota
```

The container runs as uid 1000. If your user has a different uid, run `sudo chown 1000 data` once, or add `--user "$(id -u):$(id -g)"` to each `run`. The `data` directory is the whole store (`mentor.sqlite` plus `attachments/`), and the same directory works from source and from the container. An Ollama running on the host is reachable from the container as `host.docker.internal`; set `MENTOR_EMBED_BASE_URL=http://host.docker.internal:11434/v1` in `.env`.

`mentor ingest bulk` downloads the daily SAM.gov extract once per day into `data/extracts/`, keeps only your NAICS codes, and fills in descriptions the API has not fetched; `--archived 2025` ingests a fiscal year's archive for history. Every command that prints data takes `--json`. `mentor --help` and `mentor <command> --help` list the rest.

### Working the pipeline

```
uv run mentor searches add sdvosb-it --naics 541512 --set-aside SDVOSBC --deadline-days 30
uv run mentor searches run sdvosb-it
uv run mentor track NOTICE_ID --stage pursuing --pwin 40
uv run mentor pipeline
uv run mentor history NOTICE_ID
uv run mentor profile set --name "Example LLC" --naics 541512 --cert SB
```

A saved search is text plus filters (NAICS, set-aside, agency path prefix, deadline window); the same filters work on `mentor search`. Notices matching any saved search move to the front of `mentor fetch`. Every stage and PWin change is kept, so `history` shows the trajectory; nothing is untracked, a dropped pursuit is `--stage no-bid`.

## Terminal UI

```
uv run mentor top
```

`1` is the dashboard: quota against budget with a 30-day sparkline, the fetch queues, store activity, deadlines in the next 7 days, and your pipeline by stage. `2` is the opportunities table; `/` focuses the search box, Enter runs a keyword search over notice and attachment text, Escape returns to the table. Enter on any row opens the context view of that notice: agency chain, description, documents, and tracking. There, `t` tracks it or changes its stage, `a` opens the agency's entity view, `o` opens the SAM.gov page in your browser, and Escape goes back. `q` quits. The app draws in your terminal's own colours.

To use it from a browser tab instead, on the same machine or a home server:

```
uv sync --extra serve
uv run mentor top --serve          # then open http://localhost:8000
```

For a point-and-click table browser over the whole store, the read-only views (`v_notices`, `v_entities`, `v_pipeline`, `v_quota_daily`) are the stable interface: `uvx datasette data/mentor.sqlite`.

## Using mentor from an AI agent

`mentor mcp` serves the store over the Model Context Protocol on standard input and output; the client starts the process, so nothing listens on the network. Add it to Claude Code, Claude Desktop, or any MCP client:

```json
{"mcpServers": {"mentor": {"command": "uv", "args": ["run", "--directory", "/path/to/mentor", "mentor", "mcp"]}}}
```

Tools: `search` (keyword, with NAICS, set-aside, agency, and deadline filters), `notice`, `entity`, `upcoming`, `pipeline`, `track`, `history`, `saved_searches`, `run_saved_search`, `save_search`, `queue_status`, `quota`, and `profile`. No tool spends SAM.gov quota or contacts the network: an agent can read everything and edit your pipeline and saved searches, nothing else. The interface is version 1; tools and fields are only ever added.

## Contributing

Design discussion is the most useful contribution right now. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the ground rules, the Developer Certificate of Origin sign-off that every commit carries, and the workflow. This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).

## License

Apache License 2.0. See [`LICENSE`](LICENSE).

## Affiliation

mentor is a personal open source project. It is not affiliated with, endorsed by, or built for any employer or government agency. It is built from public data and publicly documented methodology only, and it does not accept contributions of proprietary capture intelligence, customer relationships, or any organization's internal process.

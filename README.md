# mentor

*Working title.* Free, open source, self-hosted business development intelligence for U.S. federal government contracting.

## Status

Pre-code. The design is complete and lives in [`docs/DESIGN.md`](docs/DESIGN.md); nothing in this repository runs yet. The first feature loop is described there in §9.

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

SAM.gov already offers keyword search and email alerts over notice metadata. What it cannot do is search inside the attached solicitation documents, where the statements of work, Section L/M instructions, and amendments live. v1 does exactly that for a NAICS and agency slice you choose, within the SAM.gov API quota: ingest notices, fetch and extract their attachments, and offer full-text and semantic search across all of it. On top of that: saved searches, an opportunity pipeline with PWin history, your company profile, a thin local web UI, and an MCP server so any AI agent can work over the store.

v1 deliberately does not include a hosted service, accounts, telemetry, third-party plugins, a wholesale mirror of SAM.gov, or enrichment sources beyond SAM.gov. The graph tables exist from the first migration; the adapters that fill them from other sources come after.

## Before you start

**SAM.gov access.** The API quota depends on how your key was issued, and the difference is the difference between a working tool and a broken one:

1. Register your entity on SAM.gov and obtain a Unique Entity ID (UEI). Registration alone can take up to 10 business days, so start it first.
2. Obtain a role on that entity registration for your SAM.gov user account. The exact role name is still being confirmed and will be stated here once verified.
3. Request your public API key from your SAM.gov account profile.

A personal key with no role is limited to roughly 10 requests per day. A key backed by a role on an active entity registration gets roughly 1,000. Every attachment download is one request, so the first number is unusable and the second is the budget the whole tool is designed around.

**A model endpoint.** Either a local runtime such as Ollama with a small model, or any OpenAI-compatible endpoint with your own key. Local models handle the high-volume work (classification, extraction, embeddings); a frontier model is optional for heavy reasoning.

## Contributing

Design discussion is the most useful contribution right now. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the ground rules, the Developer Certificate of Origin sign-off that every commit carries, and the workflow. This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).

## License

Apache License 2.0. See [`LICENSE`](LICENSE).

## Affiliation

mentor is a personal open source project. It is not affiliated with, endorsed by, or built for any employer or government agency. It is built from public data and publicly documented methodology only, and it does not accept contributions of proprietary capture intelligence, customer relationships, or any organization's internal process.

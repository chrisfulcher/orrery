# Project Design Reference
*Open source government contracting BD ecosystem — design elements, constraints, and decisions*
*Status: living document, pre-code. Last updated: 2026-09-04*

---

## 1. Mission

Build a free, open source ecosystem for U.S. federal business development intelligence —
aggregating, searching, and analyzing public government spending and opportunity data —
that structurally undercuts the pricing of incumbent tools (GovWin IQ at ~$13k–$119k/yr,
enterprise AI proposal suites) by being free to self-host and community-owned.

**This is a movement, not a competitor.** The disruption comes from being structurally
unanswerable: nobody can compete with free + self-hosted + community-owned without
destroying their own business model.

## 2. Core principles (the non-negotiables)

1. **Shipped code stays free and forkable.** Guaranteed by a permissive license,
   not by a pricing page.
2. **Self-hostable by design.** One `docker compose up` on a home server or cheap cloud box.
3. **Zero telemetry.** The software phones home to nobody. In a user base that works
   near sensitive programs, this is a headline feature.
4. **Bring your own keys.** Users supply their own SAM.gov API key and LLM credentials.
   The project never touches credentials, billing, or inference costs.
5. **Model-agnostic AI.** All AI features talk to a configurable OpenAI-compatible
   endpoint. Local (Ollama/Qwen-class) or frontier cloud (Claude/GPT) — user's choice.
6. **No hosted service in v1.** No accounts, no uptime obligation, no support burden
   while the project is young.
7. **Public data + public knowledge only.** FAR mechanics, Shipley-style methodology,
   protest case law: fair game. Proprietary capture intelligence, customer
   relationships, or any organization's internal process: never enters the repo.

## 3. Strategic decisions

- **License: Apache-2.0.** Permissive, with an explicit patent grant. Chosen so
  that corporate and defense-adjacent legal review does not block adoption, and so
  the project can be the substrate other tools build on. Copyleft (AGPL) was
  considered and rejected: it gives reciprocity, not exclusivity, and it deters
  exactly the organizations the project wants as users. Closed forks by others are
  an accepted cost; the maintainers and community are the moat, not the license.
- **Contributions under DCO.** Contributors sign off that they have the right to
  submit their work. No CLA: the core will never need relicensing, and copyright
  stays with the people who wrote the code.
- **No commercial plans.** No paid tier, hosted service, or revenue is planned.
  The architecture (public/private data boundary, versioned interfaces) leaves
  commercial add-ons *possible* without committing to them.
- **Engine first, in the open, self-hosted.** A hosted deployment of the same
  engine is a possible phase-two *deployment decision*, not a rewrite. The first
  year of work is identical in both futures.
- **Design for hosted from day one, without hosting.** Web UI, not just CLI.
  Multi-user-aware data model even while v1 runs single-user. Clean API boundary
  between engine and interface.
- **Wedge market:** small businesses chasing set-asides who will never pay Deltek
  five figures. Build for them.

## 4. Architecture shape

- **Small boring core:** a data daemon that ingests public sources into a local store.
  Small enough that a single part-time maintainer can keep it healthy.
- **MCP server from day one:** the whole store is exposed via MCP so any AI agent
  can sit on top. Goal: become the substrate the next wave of tools builds on.
  In v1 this is the *only* extension surface; the MCP interface is versioned from
  the first release.
- **Capability-scoped plugins in v2, not v1.** The core loads no third-party code
  in v1. When a plugin interface arrives, each plugin declares which tables it
  reads and writes and whether it needs network access; the core enforces the
  declaration and shows it at install time. The community-value areas — agency
  forecast scrapers, capture skills, compliance matrix generators, PWin models —
  arrive through that scoped interface or as MCP-driven agents, never as
  unreviewed code with access to a user's pipeline.
- **Storage: SQLite.** Single file, zero admin, sufficient for a single-team BD
  workbench. FTS5 for full-text search; sqlite-vec (or equivalent) for embeddings.
  Revisit only if hosted multi-tenant phase arrives.
- **Deployment: Docker Compose.** Install is one command. Installation path must be
  transparent and auditable (defense-adjacent IT will read before running).
- **AI task tiering:** high-volume grunt work (classification, extraction, tagging,
  summarization, embeddings) targets small local models; heavy reasoning (proposal
  analysis, Section L/M work, PWin judgment) targets frontier models via user's key.

## 5. Data sources (priority order)

1. **SAM.gov Get Opportunities API** — active solicitations, sources sought,
   presolicitations. The v1 source. Free key from open.gsa.gov.
2. **USAspending / FPDS** — historical awards and contract actions. Phase 2;
   powers trend analysis ("who wins what, where, at what value").
3. **Agency long-range forecasts** — scattered, non-standard agency sites.
   v2 territory: community scrapers via the capability-scoped plugin interface,
   or MCP-driven agents.
4. **Grants.gov, budget justifications (J-books), appropriations** — later phases;
   this layer replicates what incumbent analysts sell.

## 6. Development workflow

- **AI-assisted, human-steered.** Coding agents do the bulk of implementation;
  people supply design, taste, and domain judgment.
- **Plan before code, always.** Domain review happens at the plan level.
- **Small commits; git is the undo button.** Commit before every ambitious task.
- **Read every diff; ask until understood.** "Make it simpler" is the
  highest-value review comment.
- **Tests accompany every feature.** A green suite is the objective check that
  anyone can trust, regardless of experience.
- `CLAUDE.md` at repo root carries conventions and constraints (this document's
  principles, condensed) so that agents and contributors share the same rules.
  Specialized agents as needed: code reviewer, researcher, and a govcon domain
  expert.

## 7. Explicit non-goals (v1)

- No hosted service, accounts, or billing.
- No customer/tenant management modules.
- No telemetry or usage analytics of any kind.
- No fine-tuned models (skills + retrieval over stock models instead).
- No third-party code loading. The MCP server is the only extension surface.
- No features that require anyone to be on call.

## 8. Draft v1 schema (opportunities engine)

*Working draft; refine before first migration.*

**Ingestion layer (public data, append-only bias):**
- `notices` — one row per SAM.gov notice: `notice_id` (natural PK), solicitation
  number, title, notice_type, agency path (dept / sub-tier / office), naics_code,
  psc_code, set_aside_code, posted_at, response_deadline, place_of_performance,
  active flag, description (full text), `raw_json` (verbatim API payload).
- `notice_versions` — full snapshot per detected change; amendments are the norm
  in this domain, and deadline-change history is itself intelligence.
- `attachments` — files linked to a notice (solicitation docs, amendments);
  fetched lazily, stored on disk, path + hash + extracted text recorded.
- `agencies`, `naics_codes`, `psc_codes` — reference tables normalized out of
  notices for clean filtering and joins.

**Search layer (derived, rebuildable):**
- `notices_fts` — FTS5 virtual table over title + description + attachment text.
- `notice_embeddings` — vector per notice (and per attachment chunk) for semantic
  search; embedding model recorded per row so re-embedding is tractable.

**Workspace layer (user data — kept strictly separate from public data):**
- `users` — present from day one even though v1 is single-user.
- `saved_searches` — named query definitions (keywords, NAICS list, set-asides,
  agencies, deadline windows); the future alerting primitive.
- `tracked_opportunities` — user's pipeline: notice_id + stage (watching /
  pursuing / bid / no-bid / submitted / won / lost), pwin, notes, tags.
- `tags`, `tracked_opportunity_tags` — freeform organization.

**Design rules:**
- Raw + typed ("ELT") pattern: always keep `raw_json`; typed columns are a
  parse that can be re-run when the parser improves.
- All timestamps UTC. Natural keys from the source where stable (`notice_id`).
- Public/ingested tables are world-shareable; workspace tables are private —
  this boundary is what makes a future hosted deployment possible without a
  rewrite.
- Derived layers (FTS, embeddings, trends) must be rebuildable from ingestion
  tables at any time.

## 9. Sequencing snapshot

1. Local prerequisites: a SAM.gov API key and a local model runtime (e.g. Ollama).
2. Repo founding documents: README manifesto, LICENSE (Apache-2.0), DCO,
   CONTRIBUTING, this design doc, CLAUDE.md.
3. First feature loop: fetch yesterday's notices for a NAICS list → SQLite,
   planned / reviewed / tested / committed.
4. Search: FTS5, then embeddings + semantic search.
5. Saved searches + tracking (the workspace layer).
6. Web UI (thin, local) and MCP server.
7. Community era: capability-scoped plugin interface, first outside issues.
8. (Optional, distant) hosted deployment of the same engine.

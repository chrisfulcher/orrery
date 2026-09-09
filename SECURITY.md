# Security Policy

## Reporting a vulnerability

**Do not open a public issue for a vulnerability.**

Report it privately through GitHub security advisories:
[Report a vulnerability](https://github.com/chrisfulcher/mentor/security/advisories/new).
Only the maintainer can read a report filed there.

You will get an acknowledgement, then either a fix or a reasoned response, and
credit in the published advisory if you want it. If you would rather not be
credited, say so and you will not be.

## Supported versions

mentor is pre-release. `main` is the only supported version, and a fix lands
there. Once releases begin, this section will name the versions that receive
them.

## Where the security surface is

mentor is self-hosted and runs on the user's own machine against their own
data, so a report is most likely to concern one of these boundaries. They are
principles from [`docs/DESIGN.md`](docs/DESIGN.md) §2, not incidental
properties, and a way to break one is a vulnerability:

- **Credentials.** The user's SAM.gov API key and any model credentials live
  only in their configuration. `.env` is written by `src/mentor/dotenv.py`
  alone — `MENTOR_*` keys only, atomic replace, mode 0600 — and credentials are
  never logged, never persisted elsewhere, and never sent anywhere but the
  service they belong to.
- **Outbound network calls.** SAM.gov, USAspending, and the endpoints the user
  has configured. Nothing else, ever: no analytics, no crash reporting, no
  update checks. An outbound call to anywhere else is a bug of this class.
- **What leaves the machine for inference.** Embedding and semantic search send
  text only to `MENTOR_EMBED_BASE_URL`; chat models are reached only through
  `src/mentor/ai/` at the endpoints configured under `MENTOR_AI_*`. The MCP
  server never contacts a model or the network.
- **The user's own data.** The company profile, pursuit notes, and the store
  itself are private to the install. Anything that widens who can read them —
  including a path that writes them into a log, a fixture, or a request body —
  is in scope.
- **Untrusted input.** Notice text, attachment files, and bulk extracts come
  from the government but are not trusted: they are third-party documents that
  reach a parser, a database, a terminal, and a model prompt. Parser crashes
  are bugs; path traversal out of `MENTOR_DATA_DIR`, SQL injection, terminal
  escape injection, and prompt injection that reaches a credential or a write
  are vulnerabilities.
- **The install path.** The container runs as a non-root user, installs only
  from the committed lockfile, and pulls no application image from a registry.
  No third-party code loading exists in v1 and none should be reachable.

## Out of scope

- Vulnerabilities in SAM.gov, USAspending, or any endpoint the user configures.
  Report those to their operators.
- Anything that requires the attacker to already have write access to the
  install, its `.env`, or its data directory.
- The consequences of a user pointing `MENTOR_EMBED_BASE_URL` or `MENTOR_AI_*`
  at a service they do not trust. Choosing the endpoint is the user's, by
  design; the guarantee is that mentor sends there and nowhere else.

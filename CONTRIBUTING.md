# Contributing to orrery

## Status

The first feature loop is in (`docs/DESIGN.md` §9 step 3). Read the design doc first; issues that reference the section they respond to are the most useful contribution, along with reports from running the loop against your own NAICS slice. The design doc is the source of truth; if a proposal changes it, the pull request updates it.

## Ground rules

These follow from the principles in `docs/DESIGN.md` §2 and are not up for negotiation in a pull request:

- **Public data and public knowledge only.** FAR mechanics, published proposal methodology, and protest case law are welcome. Proprietary capture intelligence, customer relationships, pricing, and any organization's internal process are not, in code, fixtures, tests, issues, or discussion.
- **People appear only in their official public capacity,** from government sources such as award records, the Federal Register, and agency org charts. No adapter may scrape personal social media or profile individuals beyond their public role.
- **Every fact carries provenance.** Anything the system asserts about an entity records its source, observation time, and confidence. A fact without a source is not stored.
- **Zero telemetry.** No outbound network calls except to SAM.gov and the endpoints the user configures. No analytics, no crash reporting, no update checks.
- **No third-party code loading in v1.** The MCP server is the only extension surface until the capability-scoped plugin interface arrives.

## Developer Certificate of Origin

Every commit must be signed off:

```
git commit -s
```

The sign-off adds a `Signed-off-by:` line with your name and email and certifies that you agree to the [Developer Certificate of Origin 1.1](DCO): that you wrote the contribution or otherwise have the right to submit it under the project's license. There is no contributor license agreement, and copyright stays with you. Commits without a sign-off are not merged.

## Development

Python 3.13 and [uv](https://docs.astral.sh/uv/); `mise install` provides both from
`.mise.toml`. Then:

```
uv sync                     # install, including dev dependencies
uv run orrery db migrate    # create the store
uv run orrery --help
```

Four checks gate every commit, and CI runs the same four:

```
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv lock --check             # the lockfile still answers pyproject.toml
```

A test belongs with the change it covers, and a test for a bug should be confirmed to fail
without the fix — `git stash push -- src/` then run it. A fixture that is more forgiving
than the real source is how bugs reach a release: if a test passes, it is worth asking what
the fixture is not sending.

Nothing needs credentials. The daily bulk extract and attachment downloads are public, so
`orrery ingest bulk` then `orrery fetch` builds a real store to develop against without a
SAM.gov key. Point `ORRERY_DATA_DIR` at a scratch directory to keep it away from a store you
care about.

## How work happens

- **Plan before code.** Non-trivial changes start as an issue or a short design note that names the design doc section they serve. Domain review happens at the plan level, where it is cheap.
- **Small commits.** One logical change per commit, with a message that says why.
- **Tests accompany every feature.** A green suite is the objective check that anyone can trust.
- **"Make it simpler" is welcome review.** Expect it, and offer it.
- **AI-assisted contributions are fine.** Coding agents may write the bulk of a change. The human who submits it must have read it, understood it, and be able to defend it in review. The sign-off is yours, not the tool's.
- **Follow `CLAUDE.md`.** It condenses the conventions that agents and people alike work to.

## Pull requests

- One concern per pull request. Reference the design doc section the change serves.
- Schema changes ship with a migration and update `docs/DESIGN.md` §8 in the same pull request.
- Changes to the MCP tool surface are versioned; breaking changes are called out in the description.
- No secrets, no local databases, no employer-, customer-, or person-specific data in fixtures. Synthetic or public sample data only.

## Security

Do not open a public issue for a vulnerability. Report it privately through GitHub's security advisories: [Report a vulnerability](https://github.com/chrisfulcher/orrery/security/advisories/new), which only the maintainer can read. You will get an acknowledgement, a fix or a reasoned response, and credit in the advisory if you want it.

## License

Contributions are licensed under the Apache License 2.0, the same as the rest of the project. See [`LICENSE`](LICENSE).

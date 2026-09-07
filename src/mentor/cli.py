"""Command-line entry point."""

import dataclasses
import json
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer

from mentor import __version__, db, query, workspace
from mentor import quota as quota_module
from mentor.config import Settings
from mentor.embed.client import EmbeddingClient, EmbeddingError, pack
from mentor.embed.pipeline import embed_pending
from mentor.extract.text import extract_pending
from mentor.fetch import queue
from mentor.ingest import awards, bulk, notices
from mentor.quota import BudgetExceeded
from mentor.sam.client import SamError
from mentor.usaspending.client import UsaspendingError

app = typer.Typer(
    no_args_is_help=True,
    help="Self-hosted business development intelligence for U.S. federal contracting.",
)
db_app = typer.Typer(no_args_is_help=True, help="Database schema and index maintenance.")
app.add_typer(db_app, name="db")
ingest_app = typer.Typer(no_args_is_help=True, help="Pull SAM.gov data into the local store.")
app.add_typer(ingest_app, name="ingest")
searches_app = typer.Typer(no_args_is_help=True, help="Saved searches.")
app.add_typer(searches_app, name="searches")
profile_app = typer.Typer(no_args_is_help=True, help="Your company profile.")
app.add_typer(profile_app, name="profile")

JsonFlag = Annotated[bool, typer.Option("--json", help="Print as JSON.")]
DateOption = Annotated[datetime | None, typer.Option(formats=["%Y-%m-%d"], metavar="YYYY-MM-DD")]


def print_json(payload: object) -> None:
    """Every ``--json`` output goes through here: one document on stdout."""
    typer.echo(json.dumps(payload, indent=2, default=str))


@app.callback()
def main() -> None:
    """Group callback; makes every command a subcommand."""


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(f"mentor {__version__}")


@app.command()
def quota(json_output: JsonFlag = False) -> None:
    """Show today's SAM.gov request budget (UTC day)."""
    settings = Settings()
    with closing(db.connect(settings.db_path)) as conn:
        spent = quota_module.spent_today(conn)
    budget = settings.sam_daily_budget
    remaining = max(budget - spent, 0)
    if json_output:
        print_json(
            {
                "date": datetime.now(UTC).date().isoformat(),
                "spent": spent,
                "budget": budget,
                "remaining": remaining,
            }
        )
    else:
        typer.echo(f"spent {spent} of {budget} today (UTC), {remaining} remaining")


@app.command()
def fetch(
    budget: Annotated[int | None, typer.Option(help="Cap descriptions fetched this run.")] = None,
    max_attachments: Annotated[
        int | None, typer.Option(help="Cap attachment downloads this run.")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show the queue; fetch nothing.")
    ] = False,
    json_output: JsonFlag = False,
) -> None:
    """Fetch pending notice descriptions (within today's budget) and attachments (free)."""
    settings = Settings()
    if dry_run:
        with closing(db.connect(settings.db_path)) as conn:
            status = queue.queue_status(conn)
            remaining = quota_module.remaining(conn, settings)
        if json_output:
            print_json({**dataclasses.asdict(status), "budget_remaining": remaining})
        else:
            typer.echo(
                f"pending: {status.descriptions_pending} descriptions,"
                f" {status.attachments_pending} attachments;"
                f" {remaining} requests remaining today"
            )
            for item in status.next_descriptions:
                deadline = item.response_deadline or "-"
                typer.echo(f"  {item.notice_id}  {deadline:20}  {item.title}")
        return
    if settings.sam_api_key is None:
        typer.echo("MENTOR_SAM_API_KEY is not set", err=True)
        raise typer.Exit(2)
    with closing(db.connect(settings.db_path)) as conn:
        result = queue.fetch_pending(conn, settings, budget=budget, max_attachments=max_attachments)
    if json_output:
        print_json(dataclasses.asdict(result))
    else:
        note = (
            " (daily budget exhausted; attachments still fetched)"
            if result.budget_exhausted
            else ""
        )
        typer.echo(
            f"run {result.run_id}: {result.descriptions_fetched} descriptions fetched,"
            f" {result.descriptions_failed} failed; {result.attachments_fetched} attachments"
            f" fetched, {result.attachments_failed} failed, {result.attachments_skipped} skipped;"
            f" {result.requests_spent} requests{note}"
        )


@app.command()
def extract(
    limit: Annotated[int | None, typer.Option(help="Cap attachments processed this run.")] = None,
    json_output: JsonFlag = False,
) -> None:
    """Extract text from fetched attachments (PDF for now). Spends no quota."""
    settings = Settings()
    with closing(db.connect(settings.db_path)) as conn:
        result = extract_pending(conn, settings, limit=limit)
    if json_output:
        print_json(dataclasses.asdict(result))
    else:
        typer.echo(
            f"{result.done} extracted, {result.unsupported} unsupported, {result.failed} failed"
        )


@app.command()
def embed(
    limit: Annotated[int | None, typer.Option(help="Cap sources embedded this run.")] = None,
    json_output: JsonFlag = False,
) -> None:
    """Embed fetched descriptions and extracted attachment text via the configured endpoint."""
    settings = Settings()
    with closing(db.connect(settings.db_path)) as conn:
        try:
            result = embed_pending(conn, settings, limit=limit)
        except EmbeddingError as exc:
            typer.echo(f"embedding stopped: {exc}", err=True)
            raise typer.Exit(1) from exc
    if json_output:
        print_json(dataclasses.asdict(result))
    else:
        typer.echo(
            f"{result.notices} notices, {result.attachments} attachments,"
            f" {result.chunks} chunks embedded with {result.model}"
        )


NaicsOption = Annotated[str | None, typer.Option("--naics", help="NAICS codes, comma-separated.")]
SetAsideOption = Annotated[
    str | None, typer.Option("--set-aside", help="Set-aside codes, comma-separated.")
]
AgencyOption = Annotated[
    str | None, typer.Option("--agency", help="Agency path code prefixes, comma-separated.")
]
DeadlineOption = Annotated[
    int | None, typer.Option("--deadline-days", help="Only deadlines within N days.")
]


def _csv(value: str | None) -> tuple[str, ...] | None:
    items = tuple(item.strip() for item in (value or "").split(",") if item.strip())
    return items or None


def _filters(
    naics: str | None, set_aside: str | None, agency: str | None, deadline_days: int | None
) -> query.Filters:
    return query.Filters(
        naics=_csv(naics),
        set_asides=_csv(set_aside),
        agency_prefixes=_csv(agency),
        deadline_within_days=deadline_days,
    )


def _print_hits(hits: list[query.SearchHit], json_output: bool) -> None:
    if json_output:
        print_json([dataclasses.asdict(hit) for hit in hits])
        return
    if not hits:
        typer.echo("no matches")
        return
    for index, hit in enumerate(hits):
        if index:
            typer.echo("")
        typer.echo(f"{hit.notice_id}  {hit.title}")
        typer.echo(f"  {hit.agency or '-'}  deadline {hit.response_deadline or '-'}")
        page = f" p.{hit.page}" if hit.page else ""
        typer.echo(f"  {hit.source}{page}: {hit.snippet}")


@app.command()
def search(
    text: Annotated[str, typer.Argument(metavar="QUERY", help="Words, phrases, or FTS5 syntax.")],
    limit: Annotated[int, typer.Option(help="Maximum notices to show.")] = 20,
    semantic: Annotated[
        bool, typer.Option("--semantic", help="Rank by meaning via the embedding endpoint.")
    ] = False,
    naics: NaicsOption = None,
    set_aside: SetAsideOption = None,
    agency: AgencyOption = None,
    deadline_days: DeadlineOption = None,
    json_output: JsonFlag = False,
) -> None:
    """Search notice text and attachment text; one best hit per notice."""
    settings = Settings()
    filters = _filters(naics, set_aside, agency, deadline_days)
    if semantic and filters != query.NO_FILTERS:
        typer.echo("filters apply to keyword search only", err=True)
        raise typer.Exit(2)
    with closing(db.connect(settings.db_path)) as conn:
        if semantic:
            hits = _semantic_hits(conn, settings, text, limit)
        else:
            try:
                hits = query.search(conn, text, limit=limit, filters=filters)
            except query.InvalidQuery as exc:
                typer.echo(f"invalid query: {exc}", err=True)
                raise typer.Exit(1) from exc
    _print_hits(hits, json_output)


def _semantic_hits(conn, settings: Settings, text: str, limit: int) -> list[query.SearchHit]:
    try:
        db.load_vec(conn)
    except db.VecUnavailable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    try:
        with EmbeddingClient(settings) as client:
            [vector] = client.embed([text])
    except EmbeddingError as exc:
        typer.echo(f"search stopped: {exc}", err=True)
        raise typer.Exit(1) from exc
    return query.semantic_search(conn, pack(vector), model=settings.embed_model, limit=limit)


@searches_app.command("add")
def searches_add(
    name: Annotated[str, typer.Argument(help="A name unique to you; re-adding replaces it.")],
    query_text: Annotated[str | None, typer.Option("--query", help="FTS5 text.")] = None,
    naics: NaicsOption = None,
    set_aside: SetAsideOption = None,
    agency: AgencyOption = None,
    deadline_days: DeadlineOption = None,
) -> None:
    """Save a named search: optional text plus filters."""
    settings = Settings()
    with closing(db.connect(settings.db_path)) as conn:
        saved = workspace.save_search(
            conn,
            name,
            query_text=query_text,
            filters=_filters(naics, set_aside, agency, deadline_days),
        )
    typer.echo(f"saved {saved.name}")


@searches_app.command("list")
def searches_list(json_output: JsonFlag = False) -> None:
    """List saved searches."""
    settings = Settings()
    with closing(db.connect(settings.db_path)) as conn:
        searches = workspace.list_searches(conn)
    if json_output:
        print_json([dataclasses.asdict(saved) for saved in searches])
        return
    if not searches:
        typer.echo("no saved searches")
    for saved in searches:
        parts = [f"query={saved.query!r}"] if saved.query else []
        parts += [
            f"{field}={value}"
            for field, value in dataclasses.asdict(saved.filters).items()
            if value not in (None, False)
        ]
        typer.echo(f"{saved.name}  {' '.join(parts) or '(everything)'}")


@searches_app.command("run")
def searches_run(
    name: Annotated[str, typer.Argument()],
    limit: Annotated[int, typer.Option(help="Maximum notices to show.")] = 50,
    json_output: JsonFlag = False,
) -> None:
    """Run a saved search over active notices."""
    settings = Settings()
    with closing(db.connect(settings.db_path)) as conn:
        try:
            hits = workspace.run_search(conn, name, limit=limit)
        except workspace.NotFound as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
        except query.InvalidQuery as exc:
            typer.echo(f"invalid query: {exc}", err=True)
            raise typer.Exit(1) from exc
    _print_hits(hits, json_output)


@searches_app.command("rm")
def searches_rm(name: Annotated[str, typer.Argument()]) -> None:
    """Delete a saved search."""
    settings = Settings()
    with closing(db.connect(settings.db_path)) as conn:
        try:
            workspace.delete_search(conn, name)
        except workspace.NotFound as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
    typer.echo(f"deleted {name}")


def _print_tracked(tracked: workspace.Tracked) -> None:
    pwin = f"pwin {tracked.pwin}" if tracked.pwin is not None else "pwin -"
    typer.echo(f"{tracked.notice_id}  {tracked.response_deadline or '-'}  {pwin}  {tracked.title}")


@app.command()
def track(
    notice_id: Annotated[str, typer.Argument(metavar="NOTICE_ID")],
    stage: Annotated[workspace.Stage | None, typer.Option(help="Pipeline stage.")] = None,
    pwin: Annotated[int | None, typer.Option(min=0, max=100, help="Win probability.")] = None,
    notes: Annotated[str | None, typer.Option(help="Replace the notes.")] = None,
    json_output: JsonFlag = False,
) -> None:
    """Track a notice in your pipeline (default stage: watching) or update it."""
    settings = Settings()
    with closing(db.connect(settings.db_path)) as conn:
        try:
            tracked = workspace.track(
                conn, notice_id, stage=stage.value if stage else None, pwin=pwin, notes=notes
            )
        except workspace.NotFound as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
    if json_output:
        print_json(dataclasses.asdict(tracked))
    else:
        typer.echo(f"{tracked.stage}:")
        _print_tracked(tracked)


@app.command()
def pipeline(json_output: JsonFlag = False) -> None:
    """Your tracked opportunities, grouped by stage."""
    settings = Settings()
    with closing(db.connect(settings.db_path)) as conn:
        rows = workspace.pipeline(conn)
    if json_output:
        print_json([dataclasses.asdict(row) for row in rows])
        return
    if not rows:
        typer.echo("nothing tracked")
    current = None
    for tracked in rows:
        if tracked.stage != current:
            current = tracked.stage
            count = sum(1 for row in rows if row.stage == current)
            typer.echo(f"{current} ({count})")
        typer.echo("  ", nl=False)
        _print_tracked(tracked)


@app.command()
def history(
    notice_id: Annotated[str, typer.Argument(metavar="NOTICE_ID")], json_output: JsonFlag = False
) -> None:
    """The change log of a tracked opportunity."""
    settings = Settings()
    with closing(db.connect(settings.db_path)) as conn:
        try:
            events = workspace.history(conn, notice_id)
        except workspace.NotFound as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
    if json_output:
        print_json([dataclasses.asdict(event) for event in events])
        return
    for event in events:
        typer.echo(
            f"{event.changed_at}  {event.field}  {event.old_value or '-'} -> {event.new_value}"
        )


@profile_app.command("show")
def profile_show(json_output: JsonFlag = False) -> None:
    """Show your company profile."""
    settings = Settings()
    with closing(db.connect(settings.db_path)) as conn:
        profile = workspace.get_profile(conn)
    if profile is None:
        typer.echo("no profile yet; set one with `mentor profile set`")
        return
    _print_profile(profile, json_output)


@profile_app.command("set")
def profile_set(
    name: Annotated[str | None, typer.Option()] = None,
    uei: Annotated[str | None, typer.Option()] = None,
    cage: Annotated[str | None, typer.Option()] = None,
    naics: NaicsOption = None,
    cert: Annotated[
        str | None, typer.Option("--cert", help="Certification codes, comma-separated.")
    ] = None,
    capability_file: Annotated[
        Path | None, typer.Option("--capability-file", help="Capability statement text file.")
    ] = None,
    target_agency: Annotated[
        str | None, typer.Option("--target-agency", help="Agency path prefixes, comma-separated.")
    ] = None,
    json_output: JsonFlag = False,
) -> None:
    """Set profile fields; fields not given keep their values."""
    settings = Settings()
    with closing(db.connect(settings.db_path)) as conn:
        profile = workspace.set_profile(
            conn,
            name=name,
            uei=uei,
            cage=cage,
            naics=_csv(naics),
            certifications=_csv(cert),
            capability_statement=capability_file.read_text() if capability_file else None,
            target_agency_prefixes=_csv(target_agency),
        )
    _print_profile(profile, json_output)


def _print_profile(profile: workspace.Profile, json_output: bool) -> None:
    if json_output:
        print_json(dataclasses.asdict(profile))
        return
    typer.echo(f"name: {profile.name or '-'}")
    typer.echo(f"uei: {profile.uei or '-'}  cage: {profile.cage or '-'}")
    typer.echo(f"naics: {', '.join(profile.naics) or '-'}")
    typer.echo(f"certifications: {', '.join(profile.certifications) or '-'}")
    typer.echo(f"target agencies: {', '.join(profile.target_agency_prefixes) or '-'}")
    statement = profile.capability_statement or ""
    typer.echo(f"capability statement: {len(statement)} characters")


@ingest_app.command("notices")
def ingest_notices_command(
    since: DateOption = None, until: DateOption = None, json_output: JsonFlag = False
) -> None:
    """Ingest notices posted in a window (default yesterday to today, UTC) for every NAICS code."""
    settings = Settings()
    if settings.sam_api_key is None:
        typer.echo("MENTOR_SAM_API_KEY is not set", err=True)
        raise typer.Exit(2)
    if not settings.naics:
        typer.echo("MENTOR_NAICS is empty; nothing to ingest", err=True)
        raise typer.Exit(2)
    today = datetime.now(UTC).date()
    posted_from = since.date() if since else today - timedelta(days=1)
    posted_to = until.date() if until else today
    with closing(db.connect(settings.db_path)) as conn:
        try:
            result = notices.ingest_notices(
                conn, settings, posted_from=posted_from, posted_to=posted_to
            )
        except (BudgetExceeded, SamError) as exc:
            typer.echo(f"ingestion stopped: {exc}", err=True)
            raise typer.Exit(1) from exc
    if json_output:
        print_json(dataclasses.asdict(result))
    else:
        typer.echo(
            f"run {result.run_id}: {result.notices_seen} notices seen, {result.notices_new} new,"
            f" {result.versions_added} versions, {result.attachments_added} attachments,"
            f" {result.requests_spent} requests"
        )


@ingest_app.command("bulk")
def ingest_bulk_command(
    archived: Annotated[
        int | None, typer.Option("--archived", help="Fiscal year of an archived extract.")
    ] = None,
    file: Annotated[
        Path | None, typer.Option("--file", help="Ingest a local extract instead of downloading.")
    ] = None,
    limit: Annotated[int | None, typer.Option(help="Cap rows in the slice this run.")] = None,
    json_output: JsonFlag = False,
) -> None:
    """Backfill from the SAM.gov bulk extract: no key, no quota, filtered to your NAICS codes."""
    settings = Settings()
    if not settings.naics:
        typer.echo("MENTOR_NAICS is empty; nothing to ingest", err=True)
        raise typer.Exit(2)
    if archived is not None and file is not None:
        typer.echo("--archived and --file are mutually exclusive", err=True)
        raise typer.Exit(2)
    try:
        path = file or bulk.fetch_extract(settings.data_dir / "extracts", fiscal_year=archived)
        typer.echo(f"extract: {path}", err=True)
        with closing(db.connect(settings.db_path)) as conn:
            result = bulk.ingest_bulk(
                conn, settings, path, mark_inactive=(file is None and archived is None), limit=limit
            )
    except (bulk.BulkError, ValueError) as exc:
        typer.echo(f"bulk ingest stopped: {exc}", err=True)
        raise typer.Exit(1) from exc
    if json_output:
        print_json(dataclasses.asdict(result))
        return
    resumed = f" (resumed at row {result.resumed_from})" if result.resumed_from else ""
    typer.echo(
        f"run {result.run_id}: {result.rows_read} rows read, {result.rows_matched} in slice,"
        f" {result.notices_new} new, {result.notices_updated} updated,"
        f" {result.descriptions_filled} descriptions filled, {result.versions_added} versions,"
        f" {result.notices_deactivated} marked inactive{resumed}"
    )


@ingest_app.command("awards")
def ingest_awards_command(
    since: DateOption = None,
    until: DateOption = None,
    file: Annotated[
        Path | None, typer.Option("--file", help="Ingest a downloaded award file instead.")
    ] = None,
    limit: Annotated[int | None, typer.Option(help="Cap rows in the slice this run.")] = None,
    json_output: JsonFlag = False,
) -> None:
    """Ingest USAspending award history for your NAICS codes: no key, no quota.

    Actions in the window (default the last three years to today) are requested as one
    download, which the service takes minutes to prepare.
    """
    settings = Settings()
    if not settings.naics:
        typer.echo("MENTOR_NAICS is empty; nothing to ingest", err=True)
        raise typer.Exit(2)
    if file is not None and (since or until):
        typer.echo("--file ignores the window; drop --since/--until", err=True)
        raise typer.Exit(2)
    today = datetime.now(UTC).date()
    posted_to = until.date() if until else today
    posted_from = since.date() if since else _years_before(today, 3)
    if posted_from > posted_to:
        typer.echo("--since must not be after --until", err=True)
        raise typer.Exit(2)
    try:
        path = file or awards.fetch_awards(settings, since=posted_from, until=posted_to)
        typer.echo(f"awards: {path}", err=True)
        with closing(db.connect(settings.db_path)) as conn:
            result = awards.ingest_awards(conn, settings, path, limit=limit)
    except (UsaspendingError, awards.AwardsError, ValueError) as exc:
        typer.echo(f"awards ingest stopped: {exc}", err=True)
        raise typer.Exit(1) from exc
    if json_output:
        print_json(dataclasses.asdict(result))
        return
    resumed = f" (resumed at row {result.resumed_from})" if result.resumed_from else ""
    typer.echo(
        f"run {result.run_id}: {result.rows_read} rows read, {result.rows_matched} in slice,"
        f" {result.contracts_new} new, {result.contracts_updated} updated,"
        f" {result.contractors_new} contractors new, {result.offices_unresolved} offices and"
        f" {result.vendors_unresolved} vendors unresolved{resumed}"
    )


def _years_before(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:  # 29 February
        return day.replace(year=day.year - years, day=28)


@app.command()
def top(
    serve: Annotated[bool, typer.Option("--serve", help="Serve the UI in a browser tab.")] = False,
    port: Annotated[int, typer.Option(help="Port for --serve (localhost only).")] = 8000,
) -> None:
    """Full-screen terminal UI: dashboard, opportunities, context view, entity view."""
    settings = Settings()
    if serve:
        try:
            from textual_serve.server import Server
        except ImportError as exc:
            typer.echo("textual-serve is not installed: uv sync --extra serve", err=True)
            raise typer.Exit(2) from exc
        Server("mentor top", port=port).serve()
        return
    from mentor.tui.app import MentorTop

    MentorTop(settings).run()


@app.command()
def mcp() -> None:
    """Serve the store to an MCP client over standard input and output."""
    from mentor.mcp_server import main  # heavy import; only when asked for

    main()


@db_app.command()
def migrate() -> None:
    """Apply pending schema migrations."""
    settings = Settings()
    with closing(db.connect(settings.db_path)) as conn:
        applied = db.migrate(conn)
    if not applied:
        typer.echo("up to date")
    for name in applied:
        typer.echo(f"applied {name}")


@db_app.command()
def reindex() -> None:
    """Rebuild the search indexes from the notice and attachment tables."""
    settings = Settings()
    with closing(db.connect(settings.db_path)) as conn:
        query.rebuild_search(conn)
    typer.echo("search index rebuilt")


@db_app.command()
def status() -> None:
    """Show applied and pending schema migrations."""
    settings = Settings()
    typer.echo(f"database: {settings.db_path}")
    with closing(db.connect(settings.db_path)) as conn:
        current = db.status(conn)
    for name in current.applied:
        typer.echo(f"applied  {name}")
    for name in current.pending:
        typer.echo(f"pending  {name}")

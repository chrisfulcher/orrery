"""Command-line entry point."""

import dataclasses
import json
from contextlib import closing
from datetime import UTC, datetime, timedelta
from typing import Annotated

import typer

from mentor import __version__, db, query
from mentor import quota as quota_module
from mentor.config import Settings
from mentor.embed.client import EmbeddingClient, EmbeddingError, pack
from mentor.embed.pipeline import embed_pending
from mentor.extract.text import extract_pending
from mentor.fetch import queue
from mentor.ingest import notices
from mentor.quota import BudgetExceeded
from mentor.sam.client import SamError

app = typer.Typer(
    no_args_is_help=True,
    help="Self-hosted business development intelligence for U.S. federal contracting.",
)
db_app = typer.Typer(no_args_is_help=True, help="Database schema and index maintenance.")
app.add_typer(db_app, name="db")
ingest_app = typer.Typer(no_args_is_help=True, help="Pull SAM.gov data into the local store.")
app.add_typer(ingest_app, name="ingest")

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


@app.command()
def search(
    text: Annotated[str, typer.Argument(metavar="QUERY", help="Words, phrases, or FTS5 syntax.")],
    limit: Annotated[int, typer.Option(help="Maximum notices to show.")] = 20,
    semantic: Annotated[
        bool, typer.Option("--semantic", help="Rank by meaning via the embedding endpoint.")
    ] = False,
    json_output: JsonFlag = False,
) -> None:
    """Search notice text and attachment text; one best hit per notice."""
    settings = Settings()
    with closing(db.connect(settings.db_path)) as conn:
        if semantic:
            hits = _semantic_hits(conn, settings, text, limit)
        else:
            try:
                hits = query.search(conn, text, limit=limit)
            except query.InvalidQuery as exc:
                typer.echo(f"invalid query: {exc}", err=True)
                raise typer.Exit(1) from exc
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

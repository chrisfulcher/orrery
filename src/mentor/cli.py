"""Command-line entry point."""

import dataclasses
import json
from contextlib import closing
from datetime import UTC, datetime, timedelta
from typing import Annotated

import typer

from mentor import __version__, db
from mentor import quota as quota_module
from mentor.config import Settings
from mentor.ingest import notices
from mentor.quota import BudgetExceeded
from mentor.sam.client import SamError

app = typer.Typer(
    no_args_is_help=True,
    help="Self-hosted business development intelligence for U.S. federal contracting.",
)
db_app = typer.Typer(no_args_is_help=True, help="Database schema management.")
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

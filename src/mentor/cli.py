"""Command-line entry point."""

from contextlib import closing

import typer

from mentor import __version__, db
from mentor.config import Settings

app = typer.Typer(
    no_args_is_help=True,
    help="Self-hosted business development intelligence for U.S. federal contracting.",
)
db_app = typer.Typer(no_args_is_help=True, help="Database schema management.")
app.add_typer(db_app, name="db")


@app.callback()
def main() -> None:
    """Group callback; makes every command a subcommand."""


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(f"mentor {__version__}")


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

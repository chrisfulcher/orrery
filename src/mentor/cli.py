"""Command-line entry point."""

import typer

from mentor import __version__

app = typer.Typer(
    no_args_is_help=True,
    help="Self-hosted business development intelligence for U.S. federal contracting.",
)


@app.callback()
def main() -> None:
    """Group callback; makes every command a subcommand."""


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(f"mentor {__version__}")

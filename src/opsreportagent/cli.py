"""Command line interface for OpsReportAgent.

All commands are implemented with Typer and share a single ``opsreport``
console script (see ``pyproject.toml``).
"""

from __future__ import annotations

import typer

from . import __version__
from .config import load_settings
from .db import get_database, init_db

app = typer.Typer(
    name="opsreport",
    help="Document-to-reporting pipeline for retail store operations.",
    no_args_is_help=True,
    add_completion=False,
)


def _echo(message: str) -> None:
    typer.echo(message)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", help="Show version and exit."),
) -> None:
    """OpsReportAgent command line entrypoint."""
    if version:
        _echo(f"opsreportagent {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        _echo(ctx.get_help())
        raise typer.Exit()


@app.command("init-db")
def init_db_command(
    database_url: str | None = typer.Option(
        None, "--database-url", help="Override DATABASE_URL for this command."
    ),
) -> None:
    """Create all database tables and print the resulting table list."""
    settings = load_settings()
    url = database_url or settings.database_url
    tables = init_db(url)
    _echo(f"Database: {url}")
    _echo(f"Created/verified {len(tables)} tables:")
    for name in tables:
        _echo(f"  - {name}")


@app.command("info")
def info_command() -> None:
    """Print the effective (secret-redacted) configuration and DB status."""
    settings = load_settings()
    _echo("Effective settings:")
    for key, value in settings.redacted().items():
        _echo(f"  {key} = {value}")
    try:
        db = get_database(settings.database_url)
        _echo(f"Tables: {', '.join(db.table_names()) or '(none - run init-db)'}")
    except Exception as exc:  # pragma: no cover - connection problems
        _echo(f"Database not reachable: {exc}")


if __name__ == "__main__":  # pragma: no cover
    app()

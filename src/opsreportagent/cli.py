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


@app.command("ingest")
def ingest_command(
    path: str = typer.Option(
        "data/corpus", "--path", help="Directory (or file) of documents to ingest."
    ),
    database_url: str | None = typer.Option(None, "--database-url"),
) -> None:
    """Ingest every supported document under --path into the documents table."""
    from .ingest import ingest_dir

    settings = load_settings()
    url = database_url or settings.database_url
    db = get_database(url)
    db.create_all()
    report = ingest_dir(path, database_url=url)
    _echo(f"Ingested from: {path}")
    for document in report.ingested:
        _echo(f"  + {document.doc_type:4s} {document.source_path} ({document.char_count} chars)")
    for duplicate in report.duplicates:
        _echo(f"  = duplicate skipped: {duplicate}")
    for skipped_path, reason in report.skipped:
        _echo(f"  ! skipped {skipped_path}: {reason}")
    _echo(
        f"Result: {report.new_count} ingested / {report.duplicate_count} duplicates "
        f"/ {report.skipped_count} skipped"
    )


@app.command("run")
def run_command(
    run_id: str = typer.Option(..., "--run-id", help="Unique identifier for this extraction run."),
    threshold: float = typer.Option(0.75, "--threshold", help="Confidence threshold for review."),
    prompt_version: str = typer.Option("v1", "--prompt-version", help="extract prompt version."),
    limit: int | None = typer.Option(None, "--limit", help="Only process the first N documents."),
    database_url: str | None = typer.Option(None, "--database-url"),
) -> None:
    """Run the LangGraph extraction pipeline over all un-extracted documents."""
    from .pipeline import run_batch

    settings = load_settings()
    url = database_url or settings.database_url
    db = get_database(url)
    db.create_all()
    with db.session_scope() as session:
        summary = run_batch(
            session,
            settings,
            run_id=run_id,
            threshold=threshold,
            prompt_version=prompt_version,
            limit=limit,
        )
    _echo(summary.render())


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

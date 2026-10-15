"""Command line interface for OpsReportAgent.

All commands are implemented with Typer and share a single ``opsreport``
console script (see ``pyproject.toml``).
"""

from __future__ import annotations

from pathlib import Path

import typer

from . import __version__
from .config import load_settings
from .db import Document, Extraction, get_database, init_db

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


@app.command("score")
def score_command(
    run_id: str = typer.Option(..., "--run-id", help="Run to score against the gold set."),
    database_url: str | None = typer.Option(None, "--database-url"),
    gold_dir: str | None = typer.Option(None, "--gold-dir", help="Directory holding gold_labels.csv."),
) -> None:
    """Score a run against the hand-labelled gold set."""
    from .scoring import score_run, write_scorecard

    settings = load_settings()
    if gold_dir:
        settings.gold_dir = gold_dir
    db = get_database(database_url or settings.database_url)
    db.create_all()
    with db.session_scope() as session:
        card = score_run(run_id, session, settings)
    csv_path, md_path = write_scorecard(card, settings.reports_dir)

    _echo(f"Scorecard for run {run_id} ({card.n_docs} documents scored)")
    _echo(card.to_text_table())
    _echo("")
    _echo("needs_review flag quality:")
    _echo(
        f"  flagged={card.review.flagged} tp={card.review.tp} fp={card.review.fp} "
        f"fn={card.review.fn} precision={card.review.precision:.3f} "
        f"recall={card.review.recall:.3f}"
    )
    _echo(f"  human corrections applied: {card.corrections} ({', '.join(card.corrected_fields) or 'none'})")
    _echo("")
    _echo("category confusion matrix (gold x predicted):")
    _echo(card.confusion_table())
    _echo("")
    _echo(f"wrote {csv_path}")
    _echo(f"wrote {md_path}")


review_app = typer.Typer(help="Human reviewer queue and correction audit log.", no_args_is_help=True)
app.add_typer(review_app, name="review")


@review_app.command("list")
def review_list_command(
    run_id: str | None = typer.Option(None, "--run-id", help="Restrict to one run."),
    all_rows: bool = typer.Option(False, "--all", help="Include non-flagged extractions."),
    limit: int | None = typer.Option(None, "--limit"),
    database_url: str | None = typer.Option(None, "--database-url"),
) -> None:
    """Show the review queue sorted by confidence ascending."""
    from .review import list_queue

    settings = load_settings()
    db = get_database(database_url or settings.database_url)
    db.create_all()
    with db.session_scope() as session:
        rows = list_queue(session, run_id, only_flagged=not all_rows, limit=limit)
        _echo(f"{len(rows)} extraction(s) in the review queue")
        header = f"{'id':>6}  {'conf':>5}  {'run':<10}  {'category':<18}  {'store':<7}  {'date':<10}  source"
        _echo(header)
        _echo("-" * len(header))
        for row in rows:
            document = session.get(Document, row.document_id)
            source = Path(document.source_path).name if document else "?"
            _echo(
                f"{row.id:>6}  {row.confidence:>5.2f}  {row.run_id:<10}  "
                f"{(row.category or '-'):<18}  {(row.store_id or '-'):<7}  "
                f"{(row.incident_date or '-'):<10}  {source}"
            )


@review_app.command("fix")
def review_fix_command(
    extraction_id: int = typer.Option(..., "--extraction-id"),
    field: str = typer.Option(..., "--field", help=f"One of: {', '.join(Extraction.CORRECTABLE_FIELDS)}"),
    value: str = typer.Option(..., "--value"),
    reviewer: str = typer.Option(..., "--reviewer"),
    note: str | None = typer.Option(None, "--note"),
    database_url: str | None = typer.Option(None, "--database-url"),
) -> None:
    """Correct one field, log an audit row, and re-score the run."""
    from .review import apply_fix

    settings = load_settings()
    db = get_database(database_url or settings.database_url)
    db.create_all()
    with db.session_scope() as session:
        result = apply_fix(session, extraction_id, field, value, reviewer, note, settings)

    _echo(
        f"extraction {result.extraction_id} ({result.run_id}): "
        f"{result.field_name} {result.old_value!r} -> {result.new_value!r} by {result.reviewer}"
    )
    if result.score_before and result.score_after:
        _echo(
            "macro accuracy: "
            f"{result.score_before.macro_accuracy:.3f} -> {result.score_after.macro_accuracy:.3f}  "
            "| category: "
            f"{result.score_before.category_accuracy:.3f} -> {result.score_after.category_accuracy:.3f}"
        )


@review_app.command("export")
def review_export_command(
    out: str = typer.Option("reports/corrections.csv", "--out"),
    database_url: str | None = typer.Option(None, "--database-url"),
) -> None:
    """Export every logged correction to CSV for prompt revision."""
    from .review import export_corrections

    settings = load_settings()
    db = get_database(database_url or settings.database_url)
    db.create_all()
    with db.session_scope() as session:
        path = export_corrections(session, out)
    _echo(f"wrote {path}")


@app.command("report")
def report_command(
    run_id: str = typer.Option(..., "--run-id"),
    weeks: int = typer.Option(6, "--weeks", help="Weekly window to aggregate."),
    database_url: str | None = typer.Option(None, "--database-url"),
) -> None:
    """Build the weekly rollup report (Markdown + HTML + charts)."""
    from .report import build_report

    settings = load_settings()
    db = get_database(database_url or settings.database_url)
    db.create_all()
    artifacts = build_report(run_id, weeks=weeks, settings=settings)
    _echo(f"executive summary: {artifacts.summary}")
    for highlight in artifacts.highlights:
        _echo(f"  - {highlight}")
    _echo(f"wrote {artifacts.md_path}")
    _echo(f"wrote {artifacts.html_path}")
    for name, path in artifacts.figures.items():
        _echo(f"  figure {name}: {path}")


@app.command("schedule")
def schedule_command(
    weekly: bool = typer.Option(False, "--weekly", help="Run the full weekly job."),
    run_id: str | None = typer.Option(None, "--run-id", help="Defaults to run-YYYYMMDD."),
    threshold: float = typer.Option(0.75, "--threshold"),
    prompt_version: str = typer.Option("v1", "--prompt-version"),
    path: str | None = typer.Option(None, "--path", help="Corpus directory."),
    weeks: int = typer.Option(6, "--weeks"),
    database_url: str | None = typer.Option(None, "--database-url"),
) -> None:
    """Cron-friendly scheduled job: ingest -> run -> score -> report."""
    from .schedule import run_weekly

    if not weekly:
        _echo("Nothing to do: pass --weekly to run the scheduled job.")
        raise typer.Exit(code=1)

    settings = load_settings()
    result = run_weekly(
        run_id=run_id,
        settings=settings,
        threshold=threshold,
        prompt_version=prompt_version,
        corpus_path=path,
        weeks=weeks,
    )
    _echo(result.render())


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

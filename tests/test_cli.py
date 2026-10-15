"""CLI smoke tests — every command, offline, against a temp SQLite database."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from opsreportagent.cli import app
from opsreportagent.db import Database, Extraction

runner = CliRunner()


@pytest.fixture
def cli_env(monkeypatch, tmp_path, corpus):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'cli.db'}")
    monkeypatch.setenv("LLM_PROVIDER", "stub")
    return corpus


def _db(tmp_path) -> Database:
    return Database(f"sqlite:///{tmp_path / 'cli.db'}")


def _flagged_id(tmp_path) -> int:
    with _db(tmp_path).session_scope() as session:
        row = session.scalars(select(Extraction).where(Extraction.needs_review.is_(True))).first()
        return row.id


def test_cli_version(cli_env):
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "opsreportagent" in result.output


def test_cli_help_lists_commands(cli_env):
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("init-db", "ingest", "run", "score", "review", "report", "schedule", "compare-runs"):
        assert command in result.output


def test_cli_init_db(cli_env):
    result = runner.invoke(app, ["init-db"])
    assert result.exit_code == 0
    assert "documents" in result.output
    assert "extractions" in result.output
    assert "scores" in result.output


def test_cli_info(cli_env):
    runner.invoke(app, ["init-db"])
    result = runner.invoke(app, ["info"])
    assert result.exit_code == 0
    assert "llm_provider = stub" in result.output
    assert "***" not in result.output  # no keys configured, nothing to redact


def test_cli_ingest(cli_env):
    result = runner.invoke(app, ["ingest", "--path", "data/corpus"])
    assert result.exit_code == 0
    assert "3 ingested / 0 duplicates" in result.output

    again = runner.invoke(app, ["ingest", "--path", "data/corpus"])
    assert "0 ingested / 3 duplicates" in again.output


def test_cli_run_and_score(cli_env):
    runner.invoke(app, ["init-db"])
    runner.invoke(app, ["ingest", "--path", "data/corpus"])

    run = runner.invoke(app, ["run", "--run-id", "run-001", "--threshold", "0.75"])
    assert run.exit_code == 0
    assert "extracted:       3" in run.output
    assert "flagged review:  1" in run.output

    score = runner.invoke(app, ["score", "--run-id", "run-001"])
    assert score.exit_code == 0
    assert "category" in score.output
    assert "MACRO" in score.output
    assert (cli_env.root / "reports" / "scorecard_run-001.md").is_file()


def test_cli_review_list_and_fix(cli_env, tmp_path):
    runner.invoke(app, ["init-db"])
    runner.invoke(app, ["ingest", "--path", "data/corpus"])
    runner.invoke(app, ["run", "--run-id", "run-001"])

    listing = runner.invoke(app, ["review", "list", "--run-id", "run-001"])
    assert listing.exit_code == 0
    assert "review queue" in listing.output

    extraction_id = _flagged_id(tmp_path)
    fix = runner.invoke(
        app,
        [
            "review",
            "fix",
            "--extraction-id",
            str(extraction_id),
            "--field",
            "category",
            "--value",
            "Safety Incident",
            "--reviewer",
            "Maria Delgado",
            "--note",
            "triage",
        ],
    )
    assert fix.exit_code == 0
    assert "Safety Incident" in fix.output
    assert "macro accuracy" in fix.output


def test_cli_review_export(cli_env, tmp_path):
    runner.invoke(app, ["init-db"])
    runner.invoke(app, ["ingest", "--path", "data/corpus"])
    runner.invoke(app, ["run", "--run-id", "run-001"])
    extraction_id = _flagged_id(tmp_path)
    runner.invoke(
        app,
        [
            "review", "fix",
            "--extraction-id", str(extraction_id),
            "--field", "category",
            "--value", "Safety Incident",
            "--reviewer", "Maria Delgado",
        ],
    )
    result = runner.invoke(app, ["review", "export", "--out", "reports/corrections.csv"])
    assert result.exit_code == 0
    assert (cli_env.root / "reports" / "corrections.csv").is_file()


def test_cli_report(cli_env):
    runner.invoke(app, ["init-db"])
    runner.invoke(app, ["ingest", "--path", "data/corpus"])
    runner.invoke(app, ["run", "--run-id", "run-001"])
    result = runner.invoke(app, ["report", "--run-id", "run-001", "--weeks", "6"])
    assert result.exit_code == 0
    assert "executive summary" in result.output
    assert (cli_env.root / "reports" / "weekly_run-001.md").is_file()
    assert (cli_env.root / "reports" / "weekly_run-001.html").is_file()


def test_cli_compare_runs(cli_env):
    runner.invoke(app, ["init-db"])
    runner.invoke(app, ["ingest", "--path", "data/corpus"])
    runner.invoke(app, ["run", "--run-id", "run-a", "--prompt-version", "v1"])
    runner.invoke(app, ["run", "--run-id", "run-b", "--prompt-version", "v2"])
    result = runner.invoke(app, ["compare-runs", "--a", "run-a", "--b", "run-b"])
    assert result.exit_code == 0
    assert "MACRO" in result.output
    assert (cli_env.root / "reports" / "prompt_ab_run-a_vs_run-b.md").is_file()


def test_cli_schedule_weekly(cli_env):
    result = runner.invoke(app, ["schedule", "--weekly", "--run-id", "run-002"])
    assert result.exit_code == 0
    assert "ingest" in result.output
    assert "TOTAL" in result.output
    assert (cli_env.root / "reports" / "weekly_run-002.md").is_file()


def test_cli_schedule_without_weekly_is_an_error(cli_env):
    result = runner.invoke(app, ["schedule", "--run-id", "nope"])
    assert result.exit_code == 1
    assert "Nothing to do" in result.output


def test_cli_run_limit(cli_env):
    runner.invoke(app, ["init-db"])
    runner.invoke(app, ["ingest", "--path", "data/corpus"])
    result = runner.invoke(app, ["run", "--run-id", "run-limit", "--limit", "1"])
    assert result.exit_code == 0
    assert "extracted:       1" in result.output

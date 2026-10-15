"""The scheduled weekly job and the prompt v1 -> v2 revision over the real corpus."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from opsreportagent.config import Settings
from opsreportagent.db import Database, Extraction
from opsreportagent.ingest import ingest_dir
from opsreportagent.pipeline import run_batch
from opsreportagent.schedule import run_weekly
from opsreportagent.scoring import GOLD_FIELDS, load_gold, score_run

from .conftest import REAL_CORPUS, REAL_GOLD, TEMPLATES_DIR


def test_run_weekly_executes_all_stages(settings, database, corpus):
    logs: list[str] = []
    result = run_weekly(
        run_id="run-002",
        settings=settings,
        corpus_path=str(corpus.corpus_dir),
        log=logs.append,
    )
    assert [stage.name for stage in result.stages] == ["ingest", "run", "score", "report"]
    assert all(stage.seconds >= 0 for stage in result.stages)
    assert result.macro_accuracy > 0
    assert any("ingest" in line for line in logs)
    assert any("TOTAL" in line for line in logs)

    md = Path(settings.reports_dir) / "weekly_run-002.md"
    assert md.is_file()
    assert (Path(settings.reports_dir) / "scorecard_run-002.csv").is_file()
    assert len(list((Path(settings.reports_dir) / "figs").glob("*.png"))) >= 4


def test_run_weekly_defaults_run_id(settings, database, corpus):
    result = run_weekly(settings=settings, corpus_path=str(corpus.corpus_dir), log=lambda *_: None)
    assert result.run_id.startswith("run-")


# --------------------------------------------------------------------------
# Prompt v1 vs v2 over the committed 60-document corpus
# --------------------------------------------------------------------------


needs_real_corpus = pytest.mark.skipif(
    not (REAL_CORPUS.is_dir() and REAL_GOLD.is_file()),
    reason="committed synthetic corpus is not available",
)


def _real_settings(database, reports_dir: Path) -> Settings:
    return Settings(
        llm_provider="stub",
        database_url=database.url,
        gold_dir=str(REAL_GOLD.parent),
        reports_dir=str(reports_dir),
        templates_dir=str(TEMPLATES_DIR),
    )


@pytest.mark.slow
@needs_real_corpus
def test_prompt_v2_improves_category_accuracy(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'promptv2.db'}")
    database.create_all()
    settings = _real_settings(database, tmp_path / "reports")

    with database.session_scope() as session:
        report = ingest_dir(REAL_CORPUS, session)
    assert report.new_count == 60

    with database.session_scope() as session:
        run_batch(session, settings, run_id="run-v1", prompt_version="v1")
    with database.session_scope() as session:
        run_batch(session, settings, run_id="run-v2", prompt_version="v2")

    with database.session_scope() as session:
        card_v1 = score_run("run-v1", session, settings, persist=False)
        card_v2 = score_run("run-v2", session, settings, persist=False)
        v2_count = len(list(session.scalars(select(Extraction).where(Extraction.run_id == "run-v2"))))

    assert v2_count == 60
    assert card_v2.fields["category"].accuracy > card_v1.fields["category"].accuracy
    assert card_v2.fields["incident_date"].accuracy > card_v1.fields["incident_date"].accuracy
    assert card_v2.fields["dollar_impact"].accuracy > card_v1.fields["dollar_impact"].accuracy
    assert card_v2.macro_accuracy >= card_v1.macro_accuracy

    # v2 is also more decisive: fewer rows routed to review.
    with database.session_scope() as session:
        v1_flagged = len(
            list(session.scalars(select(Extraction).where(Extraction.run_id == "run-v1", Extraction.needs_review.is_(True))))
        )
        v2_flagged = len(
            list(session.scalars(select(Extraction).where(Extraction.run_id == "run-v2", Extraction.needs_review.is_(True))))
        )
    assert v2_flagged < v1_flagged


@pytest.mark.slow
@needs_real_corpus
def test_gold_set_covers_the_whole_corpus():
    gold = load_gold(REAL_GOLD)
    files = sorted(p.name for p in REAL_CORPUS.iterdir() if p.is_file())
    assert len(files) == 60
    assert sorted(gold) == files
    formats = {Path(name).suffix for name in files}
    assert formats == {".pdf", ".docx", ".eml"}
    assert set(GOLD_FIELDS) <= set(next(iter(gold.values())))

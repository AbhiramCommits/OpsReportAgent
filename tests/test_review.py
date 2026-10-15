"""Reviewer queue: corrections, the audit log, re-scoring, and CSV export."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from opsreportagent.db import Document, Extraction, Review
from opsreportagent.ingest import ingest_dir
from opsreportagent.pipeline import run_batch
from opsreportagent.review import (
    ReviewError,
    apply_fix,
    export_corrections,
    list_queue,
)


def _run(settings, database, corpus, run_id="run-001"):
    with database.session_scope() as session:
        ingest_dir(corpus.corpus_dir, session)
    with database.session_scope() as session:
        run_batch(session, settings, run_id=run_id, prompt_version="v1")


def _flagged_id(database, run_id="run-001") -> int:
    with database.session_scope() as session:
        row = session.scalars(
            select(Extraction).where(Extraction.run_id == run_id, Extraction.needs_review.is_(True))
        ).first()
        return row.id


def test_list_queue_is_sorted_by_confidence(settings, database, corpus):
    _run(settings, database, corpus)
    with database.session_scope() as session:
        rows = list_queue(session, "run-001")
    assert rows
    confidences = [row.confidence for row in rows]
    assert confidences == sorted(confidences)


def test_list_queue_can_include_all_rows(settings, database, corpus):
    _run(settings, database, corpus)
    with database.session_scope() as session:
        flagged = list_queue(session, "run-001", only_flagged=True)
        everything = list_queue(session, "run-001", only_flagged=False)
    assert len(flagged) < len(everything)


def test_apply_fix_updates_field_and_logs_audit_row(settings, database, corpus):
    _run(settings, database, corpus)
    extraction_id = _flagged_id(database)
    with database.session_scope() as session:
        result = apply_fix(
            session,
            extraction_id,
            "category",
            "Safety Incident",
            reviewer="Maria Delgado",
            note="looks like a slip incident",
            settings=settings,
        )
        review = session.scalars(select(Review)).one()
    assert result.old_value != "Safety Incident"
    assert result.new_value == "Safety Incident"
    assert review.field_name == "category"
    assert review.reviewer == "Maria Delgado"
    assert review.old_value == result.old_value


def test_apply_fix_recomputes_the_run_score(settings, database, corpus):
    _run(settings, database, corpus)
    extraction_id = _flagged_id(database)
    with database.session_scope() as session:
        result = apply_fix(
            session, extraction_id, "category", "Safety Incident", "Maria Delgado", settings=settings
        )
    assert result.score_before is not None and result.score_after is not None
    assert result.score_after.category_accuracy >= result.score_before.category_accuracy


def test_apply_fix_coerces_currency_and_dates(settings, database, corpus):
    _run(settings, database, corpus)
    extraction_id = _flagged_id(database)
    with database.session_scope() as session:
        apply_fix(session, extraction_id, "dollar_impact", "$1,234.50", "R1", settings=settings)
        apply_fix(session, extraction_id, "incident_date", "9/5/26", "R1", settings=settings)
        extraction = session.get(Extraction, extraction_id)
    assert extraction.dollar_impact == pytest.approx(1234.50)
    assert extraction.incident_date == "2026-09-05"


def test_apply_fix_rejects_unknown_field(settings, database, corpus):
    _run(settings, database, corpus)
    extraction_id = _flagged_id(database)
    with database.session_scope() as session, pytest.raises(ReviewError):
        apply_fix(session, extraction_id, "confidence", "1.0", "R1", settings=settings)


def test_apply_fix_requires_a_reviewer(settings, database, corpus):
    _run(settings, database, corpus)
    extraction_id = _flagged_id(database)
    with database.session_scope() as session, pytest.raises(ReviewError):
        apply_fix(session, extraction_id, "category", "Food Safety", "  ", settings=settings)


def test_apply_fix_rejects_bad_date(settings, database, corpus):
    _run(settings, database, corpus)
    extraction_id = _flagged_id(database)
    with database.session_scope() as session, pytest.raises(ReviewError):
        apply_fix(session, extraction_id, "incident_date", "not-a-date", "R1", settings=settings)


def test_apply_fix_raises_for_missing_extraction(settings, database):
    with database.session_scope() as session, pytest.raises(ReviewError):
        apply_fix(session, 99999, "category", "Food Safety", "R1", settings=settings)


def test_export_corrections_writes_csv(settings, database, corpus):
    _run(settings, database, corpus)
    extraction_id = _flagged_id(database)
    out = corpus.root / "reports" / "corrections.csv"
    with database.session_scope() as session:
        apply_fix(session, extraction_id, "category", "Safety Incident", "Maria Delgado", settings=settings)
        path = export_corrections(session, out)
    text = path.read_text(encoding="utf-8")
    assert "review_id,created_at,run_id" in text
    assert "Maria Delgado" in text
    assert "email_fixture.eml" in text


def test_corrections_raise_macro_accuracy(settings, database, corpus):
    """A full reviewer triage of the flagged rows must improve the score."""
    _run(settings, database, corpus)
    with database.session_scope() as session:
        extraction_id = _flagged_id(database)
        before = apply_fix(
            session, extraction_id, "category", "Safety Incident", "Maria Delgado", settings=settings
        ).score_before

    with database.session_scope() as session:
        apply_fix(session, extraction_id, "incident_date", "2026-09-05", "Maria Delgado", settings=settings)
    with database.session_scope() as session:
        from opsreportagent.scoring import score_run

        after = score_run("run-001", session, settings, persist=False)
    assert after.macro_accuracy > before.macro_accuracy


def test_document_lookup_helper(settings, database, corpus):
    _run(settings, database, corpus)
    with database.session_scope() as session:
        document = session.scalars(select(Document)).first()
    assert document.doc_type in {"pdf", "docx", "eml"}

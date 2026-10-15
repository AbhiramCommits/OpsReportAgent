"""LangGraph pipeline: graph shape, persistence, confidence thresholding."""

from __future__ import annotations

from sqlalchemy import select

from opsreportagent.db import Document, Extraction, Run
from opsreportagent.ingest import ingest_dir
from opsreportagent.pipeline import (
    NODE_ORDER,
    build_graph,
    make_context,
    run_batch,
    unextracted_documents,
)


def _seed(database, corpus) -> None:
    with database.session_scope() as session:
        ingest_dir(corpus.corpus_dir, session)


def test_graph_has_the_six_documented_nodes(settings, database):
    with database.session_scope() as session:
        ctx = make_context(session, settings, "run-graph")
        graph = build_graph(ctx)
    assert set(NODE_ORDER) == {"load", "summarize", "classify", "extract", "validate", "persist"}
    assert set(graph.get_graph().nodes) >= set(NODE_ORDER)


def test_run_batch_processes_every_document(settings, database, corpus):
    _seed(database, corpus)
    with database.session_scope() as session:
        summary = run_batch(session, settings, run_id="run-001")
    assert summary.extracted == 3
    assert summary.hard_failed == 0

    with database.session_scope() as session:
        extractions = list(session.scalars(select(Extraction).where(Extraction.run_id == "run-001")))
        assert len(extractions) == 3
        assert all(e.prompt_version == "v1" for e in extractions)
        assert all(e.prompt_hash for e in extractions)
        run = session.scalar(select(Run).where(Run.run_id == "run-001"))
        assert run is not None and run.doc_count == 3 and run.finished_at is not None


def test_low_confidence_document_is_flagged_for_review(settings, database, corpus):
    _seed(database, corpus)
    with database.session_scope() as session:
        summary = run_batch(session, settings, run_id="run-001")
    assert summary.needs_review >= 1

    with database.session_scope() as session:
        flagged = list(session.scalars(select(Extraction).where(Extraction.needs_review.is_(True))))
        assert flagged
        assert all(e.confidence < 0.75 for e in flagged)
        sources = {
            session.get(Document, e.document_id).source_path for e in flagged
        }
        assert any("email_fixture" in s for s in sources)


def test_threshold_controls_flagging(settings, database, corpus):
    _seed(database, corpus)
    strict = settings
    strict.confidence_threshold = 0.99
    with database.session_scope() as session:
        summary = run_batch(session, strict, run_id="run-strict")
    assert summary.needs_review == 3  # everything is below 0.99


def test_extraction_fields_match_the_document(settings, database, corpus):
    _seed(database, corpus)
    with database.session_scope() as session:
        run_batch(session, settings, run_id="run-001")
    with database.session_scope() as session:
        rows = {
            session.get(Document, e.document_id).source_path.split("/")[-1]: e
            for e in session.scalars(select(Extraction))
        }
    pdf = rows["incident_fixture.pdf"]
    assert pdf.category == "Equipment Failure"
    assert pdf.store_id == "ST-002"
    assert pdf.incident_date == "2026-08-10"
    assert pdf.dollar_impact == 1234.56
    assert pdf.severity == "High"


def test_unextracted_documents_shrinks_after_a_run(settings, database, corpus):
    _seed(database, corpus)
    with database.session_scope() as session:
        assert len(unextracted_documents(session, "run-001")) == 3
        run_batch(session, settings, run_id="run-001")
        assert unextracted_documents(session, "run-001") == []
        # a new run id still sees all three
        assert len(unextracted_documents(session, "run-002")) == 3


def test_limit_option_processes_a_subset(settings, database, corpus):
    _seed(database, corpus)
    with database.session_scope() as session:
        summary = run_batch(session, settings, run_id="run-limit", limit=1)
    assert summary.extracted == 1

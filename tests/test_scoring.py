"""Scoring math on a handcrafted gold set, plus the prompt A/B comparison."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from opsreportagent.db import Document, Score
from opsreportagent.ingest import ingest_dir
from opsreportagent.pipeline import run_batch
from opsreportagent.scoring import (
    compare_runs,
    field_matches,
    load_gold,
    score_extractions,
    score_run,
    write_scorecard,
)


class FakeExtraction:
    """Minimal stand-in for an Extraction row."""

    def __init__(self, document_id, **fields):
        self.document_id = document_id
        self.run_id = "run-fake"
        self.needs_review = fields.pop("needs_review", False)
        for name in (
            "category",
            "subcategory",
            "store_id",
            "incident_date",
            "vendor",
            "dollar_impact",
            "severity",
            "summary",
        ):
            setattr(self, name, fields.pop(name, None))


def _doc(doc_id: int, name: str) -> Document:
    return Document(id=doc_id, source_path=f"data/corpus/{name}", doc_type="pdf", sha256=str(doc_id))


# -- field comparison -------------------------------------------------------


def test_field_matches_dollar_tolerance():
    assert field_matches("dollar_impact", 1000.0, "1000.75")
    assert not field_matches("dollar_impact", 1000.0, "1002.00")
    assert field_matches("dollar_impact", None, "")
    assert not field_matches("dollar_impact", None, "10.00")
    assert not field_matches("dollar_impact", 10.0, "")


def test_field_matches_text_is_case_and_space_insensitive():
    assert field_matches("category", "food safety", "Food Safety")
    assert field_matches("vendor", "  Polar Mechanical ", "Polar Mechanical")
    assert not field_matches("store_id", "ST-1", "ST-001")


# -- scoring core -----------------------------------------------------------


def _gold_set() -> dict[str, dict]:
    return {
        "a.pdf": {
            "source_file": "a.pdf",
            "category": "Food Safety",
            "store_id": "ST-001",
            "incident_date": "2026-09-01",
            "vendor": "GreenLeaf Produce",
            "dollar_impact": "100.00",
            "severity": "High",
        },
        "b.pdf": {
            "source_file": "b.pdf",
            "category": "Facilities",
            "store_id": "ST-002",
            "incident_date": "2026-09-02",
            "vendor": "",
            "dollar_impact": "",
            "severity": "",
        },
    }


def test_score_extractions_perfect_run():
    docs = {1: _doc(1, "a.pdf"), 2: _doc(2, "b.pdf")}
    exts = [
        FakeExtraction(
            1,
            category="Food Safety",
            store_id="ST-001",
            incident_date="2026-09-01",
            vendor="GreenLeaf Produce",
            dollar_impact=100.0,
            severity="High",
        ),
        FakeExtraction(2, category="Facilities", store_id="ST-002", incident_date="2026-09-02"),
    ]
    card = score_extractions(exts, _gold_set(), docs)
    assert card.n_docs == 2
    assert card.macro_accuracy == 1.0
    assert card.review.fn == 0 and card.review.fp == 0


def test_score_extractions_counts_field_errors():
    docs = {1: _doc(1, "a.pdf"), 2: _doc(2, "b.pdf")}
    exts = [
        FakeExtraction(
            1,
            category="Vendor Delivery",  # wrong
            store_id="ST-001",
            incident_date="2026-09-01",
            vendor="GreenLeaf Produce",
            dollar_impact=100.0,
            severity="High",
        ),
        FakeExtraction(2, category="Facilities", store_id="ST-002", incident_date="2026-09-02"),
    ]
    card = score_extractions(exts, _gold_set(), docs)
    assert card.fields["category"].accuracy == 0.5
    assert card.fields["store_id"].accuracy == 1.0
    assert "a.pdf" in card.mismatched_docs
    assert card.macro_accuracy < 1.0


def test_needs_review_precision_and_recall():
    docs = {1: _doc(1, "a.pdf"), 2: _doc(2, "b.pdf")}
    exts = [
        # flagged and wrong -> true positive
        FakeExtraction(1, category="Vendor Delivery", needs_review=True),
        # not flagged and correct -> true negative
        FakeExtraction(2, category="Facilities", store_id="ST-002", incident_date="2026-09-02"),
    ]
    card = score_extractions(exts, _gold_set(), docs)
    assert card.review.tp == 1
    assert card.review.precision == 1.0
    assert card.review.recall == 1.0


def test_needs_review_false_negative_lowers_recall():
    docs = {1: _doc(1, "a.pdf"), 2: _doc(2, "b.pdf")}
    exts = [
        FakeExtraction(1, category="Vendor Delivery", needs_review=False),  # wrong, unflagged
        FakeExtraction(2, category="Facilities", store_id="ST-002", incident_date="2026-09-02"),
    ]
    card = score_extractions(exts, _gold_set(), docs)
    assert card.review.fn == 1
    assert card.review.recall == 0.0


def test_unscored_documents_are_reported():
    docs = {1: _doc(1, "a.pdf"), 9: _doc(9, "unknown.pdf")}
    exts = [FakeExtraction(1, category="Food Safety"), FakeExtraction(9, category="Facilities")]
    card = score_extractions(exts, _gold_set(), docs)
    assert card.unscored_docs == ["unknown.pdf"]
    assert card.n_docs == 1


# -- database-backed scoring ------------------------------------------------


def test_load_gold_reads_csv(corpus):
    gold = load_gold(corpus.gold_path)
    assert len(gold) == 3
    assert gold["incident_fixture.pdf"]["category"] == "Equipment Failure"


def test_score_run_persists_scores(settings, database, corpus):
    with database.session_scope() as session:
        ingest_dir(corpus.corpus_dir, session)
    with database.session_scope() as session:
        run_batch(session, settings, run_id="run-001")
    with database.session_scope() as session:
        card = score_run("run-001", session, settings)
        persisted = list(session.scalars(select(Score).where(Score.run_id == "run-001")))
    assert card.n_docs == 3
    assert len(persisted) == 7  # 6 fields + macro row
    assert any(row.field_name == "__macro__" for row in persisted)


def test_write_scorecard_creates_csv_and_md(settings, database, corpus):
    with database.session_scope() as session:
        ingest_dir(corpus.corpus_dir, session)
    with database.session_scope() as session:
        run_batch(session, settings, run_id="run-001")
        card = score_run("run-001", session, settings)
    csv_path, md_path = write_scorecard(card, settings.reports_dir)
    assert csv_path.is_file() and md_path.is_file()
    assert "macro" in md_path.read_text(encoding="utf-8")


def test_compare_runs_writes_delta_table(settings, database, corpus):
    with database.session_scope() as session:
        ingest_dir(corpus.corpus_dir, session)
    with database.session_scope() as session:
        run_batch(session, settings, run_id="run-a", prompt_version="v1")
        run_batch(session, settings, run_id="run-b", prompt_version="v2")
        result = compare_runs("run-a", "run-b", session, settings, settings.reports_dir)
    assert result.run_a == "run-a" and result.run_b == "run-b"
    assert {row["field"] for row in result.rows} >= {"category", "incident_date"}
    path = Path(settings.reports_dir) / "prompt_ab_run-a_vs_run-b.md"
    assert path.is_file()
    assert "macro delta" in path.read_text(encoding="utf-8")

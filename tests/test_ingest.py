"""Ingestion: per-format loaders, normalisation, sha256 de-duplication."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from opsreportagent.db import Document
from opsreportagent.ingest import (
    ingest_dir,
    ingest_file,
    load_docx,
    load_eml,
    load_file,
    load_pdf,
    load_txt,
    normalize_text,
    sha256_text,
)

from .conftest import write_docx, write_eml, write_pdf


def test_load_pdf_extracts_text(corpus):
    text = load_pdf(corpus.corpus_dir / "incident_fixture.pdf")
    assert "Store: ST-002" in text
    assert "Estimated impact: $1,234.56" in text


def test_load_docx_extracts_paragraphs(corpus):
    text = load_docx(corpus.corpus_dir / "vendor_fixture.docx")
    assert "Vendor Notice" in text
    assert "Cascade Beverage" in text


def test_load_eml_includes_headers_and_body(corpus):
    text = load_eml(corpus.corpus_dir / "email_fixture.eml")
    assert "Subject: [Store Ops] EM-9003 - ST-004" in text
    assert "Not sure what happened" in text


def test_load_txt_reads_plain_text(tmp_path: Path):
    path = tmp_path / "note.txt"
    path.write_text("Store: ST-009\nSeverity: Low\n", encoding="utf-8")
    assert "ST-009" in load_txt(path)


def test_normalize_text_collapses_whitespace_and_blank_lines():
    raw = "Hello    world\r\n\r\n\r\n\r\nSecond\tline  \n"
    assert normalize_text(raw) == "Hello world\n\nSecond line"


def test_sha256_text_is_stable_and_content_sensitive():
    assert sha256_text("abc") == sha256_text("abc")
    assert sha256_text("abc") != sha256_text("abd")


def test_load_file_dispatches_by_extension(corpus):
    text, doc_type = load_file(corpus.corpus_dir / "incident_fixture.pdf")
    assert doc_type == "pdf"
    assert "INC-9001" in text


def test_ingest_dir_reports_counts(database, corpus):
    with database.session_scope() as session:
        report = ingest_dir(corpus.corpus_dir, session)
    assert report.new_count == 3
    assert report.duplicate_count == 0
    assert report.skipped_count == 0
    # the report behaves like a list of Documents
    assert len(report) == 3
    assert all(isinstance(doc, Document) for doc in report)


def test_ingest_dir_is_idempotent_by_sha256(database, corpus):
    with database.session_scope() as session:
        first = ingest_dir(corpus.corpus_dir, session)
    with database.session_scope() as session:
        second = ingest_dir(corpus.corpus_dir, session)
    assert first.new_count == 3
    assert second.new_count == 0
    assert second.duplicate_count == 3

    with database.session_scope() as session:
        total = session.query(Document).count()
    assert total == 3


def test_ingest_file_returns_none_for_duplicate(database, corpus):
    with database.session_scope() as session:
        first = ingest_file(corpus.corpus_dir / "incident_fixture.pdf", session)
        second = ingest_file(corpus.corpus_dir / "incident_fixture.pdf", session)
    assert first is not None
    assert second is None


def test_ingest_dir_skips_unsupported_files(database, tmp_path: Path):
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    (corpus_dir / "notes.md").write_text("not supported", encoding="utf-8")
    (corpus_dir / "image.png").write_bytes(b"\x89PNG")
    write_pdf(corpus_dir / "ok.pdf", ["Store: ST-001", "Severity: Low"])
    with database.session_scope() as session:
        report = ingest_dir(corpus_dir, session)
    assert report.new_count == 1
    assert report.skipped_count == 0  # unsupported suffixes are simply ignored


def test_ingest_records_char_count_and_type(database, corpus):
    with database.session_scope() as session:
        ingest_dir(corpus.corpus_dir, session)
        doc = session.scalars(select(Document).where(Document.doc_type == "docx")).one()
        assert doc.char_count == len(doc.raw_text)
        assert doc.char_count > 0
        assert doc.sha256 == sha256_text(doc.raw_text)


def test_write_eml_roundtrip(tmp_path: Path):
    path = write_eml(tmp_path / "x.eml", "Subject line", "Body text here")
    text = load_eml(path)
    assert "Subject line" in text and "Body text here" in text


def test_write_docx_roundtrip(tmp_path: Path):
    path = write_docx(tmp_path / "x.docx", ["Alpha", "Beta"])
    assert "Alpha" in load_docx(path)

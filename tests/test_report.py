"""Weekly report generation: aggregation, charts, executive summary."""

from __future__ import annotations

import json

from opsreportagent.ingest import ingest_dir
from opsreportagent.llm import StubLLM
from opsreportagent.pipeline import run_batch
from opsreportagent.report import (
    ARCHITECTURE_MERMAID,
    build_exec_summary,
    build_report,
    build_stats,
    load_frame,
    make_charts,
)


def _prepare(settings, database, corpus, run_id="run-001"):
    with database.session_scope() as session:
        ingest_dir(corpus.corpus_dir, session)
    with database.session_scope() as session:
        run_batch(session, settings, run_id=run_id)


def test_load_frame_returns_expected_columns(settings, database, corpus):
    _prepare(settings, database, corpus)
    with database.session_scope() as session:
        frame = load_frame(session, "run-001")
    assert len(frame) == 3
    assert {"category", "dollar_impact", "needs_review", "source_file"} <= set(frame.columns)


def test_build_stats_aggregates_real_numbers(settings, database, corpus):
    _prepare(settings, database, corpus)
    with database.session_scope() as session:
        frame = load_frame(session, "run-001")
    stats = build_stats(frame, "run-001", weeks=6)
    assert stats["total_documents"] == 3
    assert stats["total_dollar_impact"] == 3734.56
    assert stats["flagged_for_review"] >= 1
    assert stats["top_store"]["id"] == "ST-003"  # 2500.00 is the largest impact
    assert set(stats["week_counts"])


def test_make_charts_writes_pngs(settings, database, corpus):
    _prepare(settings, database, corpus)
    with database.session_scope() as session:
        frame = load_frame(session, "run-001")
    stats = build_stats(frame, "run-001", weeks=6)
    figs = make_charts(frame, stats, corpus.root / "reports" / "figs")
    assert len(figs) >= 4
    for path in figs.values():
        assert path.is_file()
        assert path.stat().st_size > 1000
        assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_build_exec_summary_uses_only_supplied_numbers(settings):
    stats = {
        "total_documents": 42,
        "weeks": 6,
        "total_dollar_impact": 12345.67,
        "flagged_for_review": 7,
        "period": "2026-01-01 to 2026-02-11",
        "severity_counts": {"High": 4, "Low": 2},
        "top_category": {"name": "Facilities", "count": 9, "dollar": 1000.0},
        "top_vendor": {"name": "Anchor Maintenance", "count": 5},
        "top_store": {"id": "ST-019", "dollar": 900.0},
    }
    payload = build_exec_summary(stats, settings, llm=StubLLM())
    assert "42" in payload["summary"]
    assert "12,345.67" in payload["summary"]
    assert "Facilities" in payload["summary"]
    assert len(payload["highlights"]) >= 3


def test_build_report_writes_markdown_and_html(settings, database, corpus):
    _prepare(settings, database, corpus)
    with database.session_scope() as session:
        artifacts = build_report("run-001", weeks=6, session=session, settings=settings)
    md = artifacts.md_path.read_text(encoding="utf-8")
    html = artifacts.html_path.read_text(encoding="utf-8")
    assert artifacts.md_path.name == "weekly_run-001.md"
    assert artifacts.html_path.name == "weekly_run-001.html"
    assert "Executive summary" in md
    assert "## Scorecard" in md
    assert "Scorecard" in html
    assert "<!DOCTYPE html>" in html


def test_report_embeds_the_mermaid_architecture_diagram(settings, database, corpus):
    _prepare(settings, database, corpus)
    with database.session_scope() as session:
        artifacts = build_report("run-001", weeks=6, session=session, settings=settings)
    md = artifacts.md_path.read_text(encoding="utf-8")
    assert "```mermaid" in md
    assert "flowchart LR" in md
    assert ARCHITECTURE_MERMAID.splitlines()[0] in md
    assert "summarize" in md and "persist" in md


def test_report_references_every_chart(settings, database, corpus):
    _prepare(settings, database, corpus)
    with database.session_scope() as session:
        artifacts = build_report("run-001", weeks=6, session=session, settings=settings)
    md = artifacts.md_path.read_text(encoding="utf-8")
    for path in artifacts.figures.values():
        assert f"figs/{path.name}" in md


def test_report_stats_are_serialisable(settings, database, corpus):
    _prepare(settings, database, corpus)
    with database.session_scope() as session:
        artifacts = build_report("run-001", weeks=6, session=session, settings=settings)
    json.dumps(artifacts.stats)  # must not raise

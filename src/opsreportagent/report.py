"""Weekly rollup report: pandas aggregation + matplotlib charts + Jinja2 templates.

Produces ``reports/weekly_<run_id>.md`` and ``reports/weekly_<run_id>.html`` with
an LLM-written executive summary, the per-field scorecard, a Mermaid
architecture diagram, a generated data-flow figure and at least six data charts.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless: no interactive backend calls

import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from .config import Settings, load_settings  # noqa: E402
from .db import Document, Extraction, get_database  # noqa: E402
from .llm import get_llm, structured_complete  # noqa: E402
from .prompt_library import load_prompt  # noqa: E402
from .scoring import GOLD_FIELDS, Scorecard, score_run  # noqa: E402
from .taxonomy import CATEGORIES, SEVERITIES  # noqa: E402

ARCHITECTURE_MERMAID = """flowchart LR
  A["Mixed corpus: PDF / DOCX / EML / TXT"] --> B["Ingestion: normalise + sha256 dedupe"]
  B --> C[("documents")]
  C --> D["LangGraph StateGraph (per document)"]
  D --> L1["load"] --> L2["summarize"] --> L3["classify"] --> L4["extract"] --> L5["validate"] --> L6["persist"]
  L6 --> E[("extractions")]
  P[("prompt library: YAML + sha256")] -.-> L2
  P -.-> L3
  P -.-> L4
  E --> F{"confidence below threshold?"}
  F -->|yes| G["Human review queue"]
  F -->|no| R
  G --> H["review fix -> reviews audit log"]
  H --> I["re-score run"]
  E --> J["Scoring vs gold set"]
  I --> J
  J --> K[("scores")]
  E --> R["Weekly rollup report"]
  K --> R
  R --> S["reports/weekly_*.md + .html + figs/"]
"""

FIGS_DIRNAME = "figs"

CHART_TITLES = {
    "incidents_per_week": "Incidents per week",
    "category_mix": "Category mix (document count)",
    "dollar_by_category": "Estimated dollar impact by category",
    "dollar_by_store": "Estimated dollar impact by store",
    "severity_distribution": "Severity distribution",
    "review_queue": "Review queue volume by category",
    "data_flow": "Data flow (this run)",
}


@dataclass
class ReportArtifacts:
    run_id: str
    md_path: Path
    html_path: Path
    figures: dict[str, Path] = field(default_factory=dict)
    stats: dict = field(default_factory=dict)
    summary: str = ""
    highlights: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Data access + aggregation
# --------------------------------------------------------------------------


def load_frame(session: Session, run_id: str) -> pd.DataFrame:
    """Load one run's extractions joined to their source documents."""
    rows = session.execute(
        select(Extraction, Document).join(Document, Extraction.document_id == Document.id).where(
            Extraction.run_id == run_id
        )
    ).all()
    records = []
    for extraction, document in rows:
        records.append(
            {
                "extraction_id": extraction.id,
                "source_file": Path(document.source_path).name,
                "doc_type": document.doc_type,
                "category": extraction.category,
                "subcategory": extraction.subcategory,
                "store_id": extraction.store_id,
                "incident_date": extraction.incident_date,
                "vendor": extraction.vendor,
                "dollar_impact": extraction.dollar_impact,
                "severity": extraction.severity,
                "confidence": extraction.confidence,
                "needs_review": bool(extraction.needs_review),
            }
        )
    frame = pd.DataFrame.from_records(
        records,
        columns=[
            "extraction_id",
            "source_file",
            "doc_type",
            "category",
            "subcategory",
            "store_id",
            "incident_date",
            "vendor",
            "dollar_impact",
            "severity",
            "confidence",
            "needs_review",
        ],
    )
    if not frame.empty:
        frame["incident_date"] = pd.to_datetime(frame["incident_date"], errors="coerce")
    return frame


def _week_start(value: date) -> date:
    return value - timedelta(days=value.weekday())


def weekly_counts(frame: pd.DataFrame, weeks: int) -> dict[str, int]:
    if frame.empty:
        return {}
    dated = frame.dropna(subset=["incident_date"])
    if dated.empty:
        return {}
    keys = dated["incident_date"].dt.date.map(_week_start)
    counts = keys.value_counts().sort_index()
    counts = counts.tail(weeks)
    return {week.isoformat(): int(count) for week, count in counts.items()}


def build_stats(frame: pd.DataFrame, run_id: str, weeks: int) -> dict:
    """Aggregate the run into a JSON-serialisable statistics payload."""
    total_documents = int(len(frame))
    dollars = frame["dollar_impact"].fillna(0.0) if not frame.empty else pd.Series(dtype=float)
    total_dollar = float(dollars.sum())

    flagged = int(frame["needs_review"].sum()) if not frame.empty else 0
    week_counts = weekly_counts(frame, weeks)

    if frame.empty:
        return {
            "run_id": run_id,
            "weeks": weeks,
            "total_documents": 0,
            "total_dollar_impact": 0.0,
            "flagged_for_review": 0,
            "review_rate": 0.0,
            "category_counts": {},
            "category_dollars": {},
            "store_dollars": {},
            "severity_counts": {},
            "vendor_counts": {},
            "document_types": {},
            "week_counts": {},
            "period": "",
            "top_category": {},
            "top_vendor": {},
            "top_store": {},
            "uncategorised": 0,
        }

    category_counts = {str(k): int(v) for k, v in frame["category"].value_counts().items()}
    category_dollars = {
        str(k): round(float(v), 2)
        for k, v in frame.groupby("category")["dollar_impact"].sum(min_count=1).fillna(0).items()
    }
    store_frame = frame.dropna(subset=["store_id"])
    store_dollars = {
        str(k): round(float(v), 2)
        for k, v in store_frame.groupby("store_id")["dollar_impact"].sum(min_count=1).fillna(0).items()
    }
    severity_counts = {
        str(k): int(v) for k, v in frame["severity"].fillna("(not stated)").value_counts().items()
    }
    vendor_frame = frame.dropna(subset=["vendor"])
    vendor_counts = {str(k): int(v) for k, v in vendor_frame["vendor"].value_counts().items()}
    document_types = {str(k): int(v) for k, v in frame["doc_type"].value_counts().items()}

    period = ""
    dated = frame.dropna(subset=["incident_date"])
    if not dated.empty:
        period = f"{dated['incident_date'].min():%Y-%m-%d} to {dated['incident_date'].max():%Y-%m-%d}"

    top_category = {}
    if category_counts:
        name = max(category_counts, key=lambda k: (category_counts[k], k))
        top_category = {
            "name": name,
            "count": category_counts[name],
            "dollar": category_dollars.get(name, 0.0),
        }
    top_vendor = {}
    if vendor_counts:
        name = max(vendor_counts, key=lambda k: (vendor_counts[k], k))
        top_vendor = {"name": name, "count": vendor_counts[name]}
    top_store = {}
    if store_dollars:
        name = max(store_dollars, key=lambda k: (store_dollars[k], k))
        top_store = {"id": name, "dollar": store_dollars[name]}

    return {
        "run_id": run_id,
        "weeks": weeks,
        "total_documents": total_documents,
        "total_dollar_impact": round(total_dollar, 2),
        "flagged_for_review": flagged,
        "review_rate": round(flagged / total_documents, 4) if total_documents else 0.0,
        "category_counts": category_counts,
        "category_dollars": category_dollars,
        "store_dollars": store_dollars,
        "severity_counts": severity_counts,
        "vendor_counts": vendor_counts,
        "document_types": document_types,
        "week_counts": week_counts,
        "period": period,
        "top_category": top_category,
        "top_vendor": top_vendor,
        "top_store": top_store,
        "uncategorised": int(frame["category"].isna().sum()),
    }


# --------------------------------------------------------------------------
# Charts
# --------------------------------------------------------------------------


def _finish(ax, fig, path: Path, title: str, rotate: int = 0) -> Path:
    ax.set_title(title, fontsize=12, fontweight="bold")
    if rotate:
        plt.setp(ax.get_xticklabels(), rotation=rotate, ha="right")
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path


def chart_incidents_per_week(stats: dict, path: Path) -> Path:
    weeks = list(stats["week_counts"])
    counts = [stats["week_counts"][w] for w in weeks]
    fig, ax = plt.subplots(figsize=(7, 3.6))
    if weeks:
        ax.bar(weeks, counts, color="#2f6fb2")
    ax.set_ylabel("documents")
    ax.set_xlabel("week starting")
    return _finish(ax, fig, path, CHART_TITLES["incidents_per_week"], rotate=30)


def chart_category_mix(stats: dict, path: Path) -> Path:
    items = sorted(stats["category_counts"].items(), key=lambda kv: kv[1])
    fig, ax = plt.subplots(figsize=(7, 4.0))
    if items:
        ax.barh([k for k, _ in items], [v for _, v in items], color="#3c8d6e")
    ax.set_xlabel("documents")
    return _finish(ax, fig, path, CHART_TITLES["category_mix"])


def chart_dollar_by_category(stats: dict, path: Path) -> Path:
    items = sorted(stats["category_dollars"].items(), key=lambda kv: kv[1])
    fig, ax = plt.subplots(figsize=(7, 4.0))
    if items:
        ax.barh([k for k, _ in items], [v for _, v in items], color="#b2682f")
    ax.set_xlabel("estimated impact (USD)")
    return _finish(ax, fig, path, CHART_TITLES["dollar_by_category"])


def chart_dollar_by_store(stats: dict, path: Path, top_n: int = 10) -> Path:
    items = sorted(stats["store_dollars"].items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    labels = [k for k, _ in items]
    values = [v for _, v in items]
    fig, ax = plt.subplots(figsize=(7, 3.6))
    if items:
        ax.bar(labels, values, color="#7a52a1")
    ax.set_ylabel("estimated impact (USD)")
    return _finish(ax, fig, path, CHART_TITLES["dollar_by_store"], rotate=45)


def chart_severity_distribution(stats: dict, path: Path) -> Path:
    order = [s for s in SEVERITIES if s in stats["severity_counts"]]
    order += [k for k in stats["severity_counts"] if k not in order]
    values = [stats["severity_counts"][k] for k in order]
    fig, ax = plt.subplots(figsize=(6, 3.4))
    if order:
        ax.bar(order, values, color="#c0504d")
    ax.set_ylabel("documents")
    return _finish(ax, fig, path, CHART_TITLES["severity_distribution"])


def chart_review_queue(stats: dict, fig_frame: pd.DataFrame, path: Path) -> Path:
    if fig_frame.empty:
        counts: dict[str, int] = {}
    else:
        flagged = fig_frame[fig_frame["needs_review"]]
        counts = {str(k): int(v) for k, v in flagged["category"].fillna("(none)").value_counts().items()}
    order = [c for c in CATEGORIES if c in counts] + [c for c in counts if c not in CATEGORIES]
    fig, ax = plt.subplots(figsize=(7, 4.0))
    if order:
        ax.barh(order, [counts[c] for c in order], color="#d8a02a")
    ax.set_xlabel("flagged extractions")
    return _finish(ax, fig, path, CHART_TITLES["review_queue"])


def chart_data_flow(stats: dict, frame: pd.DataFrame, path: Path) -> Path:
    """Schematic data-flow diagram annotated with this run's real counts."""
    fig, ax = plt.subplots(figsize=(9, 3.2))
    ax.axis("off")
    boxes = [
        ("Corpus\n{} docs".format(stats["total_documents"]), 0.02, "#2f6fb2"),
        ("Extraction\n{} rows".format(stats["total_documents"]), 0.27, "#3c8d6e"),
        ("Auto-accepted\n{}".format(stats["total_documents"] - stats["flagged_for_review"]), 0.52, "#7a52a1"),
        ("Review queue\n{}".format(stats["flagged_for_review"]), 0.77, "#d8a02a"),
    ]
    for label, x, colour in boxes:
        ax.add_patch(
            plt.Rectangle((x, 0.3), 0.19, 0.4, facecolor=colour, edgecolor="white", alpha=0.9)
        )
        ax.text(x + 0.095, 0.5, label, ha="center", va="center", color="white", fontsize=10, fontweight="bold")
    for x in (0.215, 0.465, 0.715):
        ax.annotate("", xy=(x + 0.05, 0.5), xytext=(x, 0.5), arrowprops={"arrowstyle": "->", "lw": 2})
    ax.text(
        0.5,
        0.12,
        "documents -> LangGraph extraction -> confidence gate -> human review -> scorecard -> weekly report",
        ha="center",
        fontsize=9,
        style="italic",
    )
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    return _finish(ax, fig, path, CHART_TITLES["data_flow"])


def make_charts(frame: pd.DataFrame, stats: dict, figs_dir: Path) -> dict[str, Path]:
    figs_dir.mkdir(parents=True, exist_ok=True)
    return {
        "incidents_per_week": chart_incidents_per_week(stats, figs_dir / "incidents_per_week.png"),
        "category_mix": chart_category_mix(stats, figs_dir / "category_mix.png"),
        "dollar_by_category": chart_dollar_by_category(stats, figs_dir / "dollar_by_category.png"),
        "dollar_by_store": chart_dollar_by_store(stats, figs_dir / "dollar_by_store.png"),
        "severity_distribution": chart_severity_distribution(
            stats, figs_dir / "severity_distribution.png"
        ),
        "review_queue": chart_review_queue(stats, frame, figs_dir / "review_queue.png"),
        "data_flow": chart_data_flow(stats, frame, figs_dir / "data_flow.png"),
    }


# --------------------------------------------------------------------------
# Executive summary
# --------------------------------------------------------------------------


def build_exec_summary(stats: dict, settings: Settings, llm=None) -> dict:
    prompt = load_prompt("exec_summary", "v1")
    llm = llm if llm is not None else get_llm(settings)
    completion = structured_complete(
        llm,
        prompt,
        {"stats": json.dumps(stats, indent=2, sort_keys=True)},
        prompt.stub_hints,
    )
    if not completion.ok:
        return {"summary": "Executive summary unavailable: " + "; ".join(completion.errors), "highlights": []}
    return completion.payload


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _data_uri(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _environment(templates_dir: Path) -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(["html", "xml"]),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["money"] = lambda value: f"{float(value or 0):,.2f}"
    return env


def build_report(
    run_id: str,
    weeks: int = 6,
    session: Session | None = None,
    settings: Settings | None = None,
    llm=None,
) -> ReportArtifacts:
    """Build the weekly rollup report for ``run_id``."""
    settings = settings or load_settings()
    own_session = session is None
    db = get_database(settings.database_url)
    if own_session:
        session = db.session()
    assert session is not None
    try:
        frame = load_frame(session, run_id)
        stats = build_stats(frame, run_id, weeks)
        scorecard: Scorecard = score_run(run_id, session, settings, persist=False)

        reports_dir = Path(settings.reports_dir)
        reports_dir.mkdir(parents=True, exist_ok=True)
        figures = make_charts(frame, stats, reports_dir / FIGS_DIRNAME)

        summary_payload = build_exec_summary(stats, settings, llm)

        context = {
            "run_id": run_id,
            "generated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "weeks": weeks,
            "stats": stats,
            "summary": summary_payload.get("summary", ""),
            "highlights": summary_payload.get("highlights", []),
            "scorecard": scorecard,
            "gold_fields": GOLD_FIELDS,
            "categories": CATEGORIES,
            "mermaid": ARCHITECTURE_MERMAID,
            "confusion_text": scorecard.confusion_table(),
            "figures": {
                name: {"path": f"{FIGS_DIRNAME}/{path.name}", "title": CHART_TITLES[name]}
                for name, path in figures.items()
            },
            "figure_data_uris": {name: _data_uri(path) for name, path in figures.items()},
            "category_counts_sorted": sorted(
                stats["category_counts"].items(), key=lambda kv: (-kv[1], kv[0])
            ),
            "vendor_counts_sorted": sorted(
                stats["vendor_counts"].items(), key=lambda kv: (-kv[1], kv[0])
            )[:10],
            "store_dollars_sorted": sorted(
                stats["store_dollars"].items(), key=lambda kv: (-kv[1], kv[0])
            )[:10],
            "severity_counts_sorted": sorted(stats["severity_counts"].items()),
            "week_counts_sorted": sorted(stats["week_counts"].items()),
            "document_types_sorted": sorted(stats["document_types"].items()),
        }

        env = _environment(Path(settings.templates_dir))
        md_path = reports_dir / f"weekly_{run_id}.md"
        html_path = reports_dir / f"weekly_{run_id}.html"
        md_path.write_text(env.get_template("report.md.j2").render(**context), encoding="utf-8")
        html_path.write_text(env.get_template("report.html.j2").render(**context), encoding="utf-8")
    finally:
        if own_session:
            session.close()

    return ReportArtifacts(
        run_id=run_id,
        md_path=md_path,
        html_path=html_path,
        figures=figures,
        stats=stats,
        summary=context["summary"],
        highlights=context["highlights"],
    )

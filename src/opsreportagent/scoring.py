"""Extraction accuracy scoring against the hand-labelled gold set.

Joins each extraction to its gold label by source filename and computes, per
field, exact-match accuracy. Also produces a category confusion matrix and the
precision/recall of the ``needs_review`` flag (i.e. whether low confidence
actually correlates with incorrect fields).
"""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .config import Settings, load_settings
from .db import Document, Extraction, Review, Score, get_database

GOLD_FIELDS = ("category", "store_id", "incident_date", "vendor", "dollar_impact", "severity")
TEXT_FIELDS = ("category", "store_id", "vendor", "severity")
DOLLAR_TOLERANCE = 1.0
MACRO_ROW = "__macro__"


@dataclass
class FieldScore:
    field_name: str
    n: int = 0
    exact_match: int = 0
    accuracy: float = 0.0

    def to_row(self) -> dict:
        return {
            "field_name": self.field_name,
            "n": self.n,
            "exact_match": self.exact_match,
            "accuracy": round(self.accuracy, 4),
        }


@dataclass
class ReviewMetrics:
    flagged: int = 0
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0
    precision: float = 0.0
    recall: float = 0.0
    flagged_but_correct: int = 0
    wrong_but_not_flagged: int = 0

    def to_row(self) -> dict:
        return asdict(self)


@dataclass
class Scorecard:
    run_id: str
    n_docs: int = 0
    fields: dict[str, FieldScore] = field(default_factory=dict)
    macro_accuracy: float = 0.0
    confusion: dict[str, dict[str, int]] = field(default_factory=dict)
    review: ReviewMetrics = field(default_factory=ReviewMetrics)
    corrections: int = 0
    corrected_fields: list[str] = field(default_factory=list)
    mismatched_docs: list[str] = field(default_factory=list)
    unscored_docs: list[str] = field(default_factory=list)

    @property
    def category_accuracy(self) -> float:
        return self.fields["category"].accuracy if "category" in self.fields else 0.0

    def to_rows(self) -> list[dict]:
        rows = [f.to_row() for f in self.fields.values()]
        rows.append(
            {
                "field_name": MACRO_ROW,
                "n": self.n_docs,
                "exact_match": "",
                "accuracy": round(self.macro_accuracy, 4),
            }
        )
        return rows

    def to_text_table(self) -> str:
        header = f"{'field':<16}{'n':>5}{'exact':>8}{'accuracy':>10}"
        lines = [header, "-" * len(header)]
        for name in GOLD_FIELDS:
            fs = self.fields.get(name)
            if fs is None:
                continue
            lines.append(f"{name:<16}{fs.n:>5}{fs.exact_match:>8}{fs.accuracy:>10.3f}")
        lines.append("-" * len(header))
        lines.append(f"{'MACRO':<16}{self.n_docs:>5}{'':>8}{self.macro_accuracy:>10.3f}")
        return "\n".join(lines)

    def confusion_table(self) -> str:
        if not self.confusion:
            return "(no category data)"
        golds = sorted(self.confusion)
        preds = sorted({p for row in self.confusion.values() for p in row})
        width = max(len(p) for p in preds) + 1
        header = "gold \\ pred".ljust(18) + "".join(p[:width].rjust(width + 1) for p in preds)
        lines = [header, "-" * len(header)]
        for gold in golds:
            row = self.confusion.get(gold, {})
            lines.append(gold.ljust(18) + "".join(str(row.get(p, 0)).rjust(width + 1) for p in preds))
        return "\n".join(lines)

    def to_markdown(self) -> str:
        lines = [
            f"# Scorecard — run `{self.run_id}`",
            "",
            f"Documents scored: **{self.n_docs}**  ",
            f"Overall macro accuracy: **{self.macro_accuracy:.3f}**  ",
            f"Category accuracy: **{self.category_accuracy:.3f}**  ",
            f"Human corrections applied: **{self.corrections}**",
            "",
            "## Per-field exact-match accuracy",
            "",
            "| field | n | exact | accuracy |",
            "| --- | ---: | ---: | ---: |",
        ]
        for name in GOLD_FIELDS:
            fs = self.fields.get(name)
            if fs is None:
                continue
            lines.append(f"| {name} | {fs.n} | {fs.exact_match} | {fs.accuracy:.3f} |")
        lines.append(f"| **macro** | {self.n_docs} | | **{self.macro_accuracy:.3f}** |")
        lines += [
            "",
            "## needs_review flag quality",
            "",
            "| metric | value |",
            "| --- | ---: |",
            f"| flagged | {self.review.flagged} |",
            f"| true positive | {self.review.tp} |",
            f"| false positive | {self.review.fp} |",
            f"| false negative | {self.review.fn} |",
            f"| precision | {self.review.precision:.3f} |",
            f"| recall | {self.review.recall:.3f} |",
            "",
            "## Category confusion matrix",
            "",
            "```",
            self.confusion_table(),
            "```",
            "",
        ]
        if self.unscored_docs:
            lines += [
                "## Unscored documents (no gold label)",
                "",
                *[f"- {doc}" for doc in self.unscored_docs],
                "",
            ]
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Gold labels
# --------------------------------------------------------------------------


def load_gold(path: str | Path) -> dict[str, dict]:
    """Read the gold CSV into a mapping keyed by source filename."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"gold labels not found: {path}")
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    return {row["source_file"]: row for row in rows}


def _basename(path: str) -> str:
    return Path(path).name


def _norm(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _norm_text(value) -> str:
    return _norm(value).casefold()


def field_matches(field_name: str, extraction_value, gold_value) -> bool:
    """Exact-match comparison, with a $1 tolerance for dollar_impact."""
    if field_name == "dollar_impact":
        e = _norm(extraction_value)
        g = _norm(gold_value)
        if e == "" and g == "":
            return True
        if e == "" or g == "":
            return False
        try:
            return abs(float(e) - float(g)) <= DOLLAR_TOLERANCE
        except ValueError:  # pragma: no cover - malformed data
            return False
    if field_name in TEXT_FIELDS:
        return _norm_text(extraction_value) == _norm_text(gold_value)
    return _norm(extraction_value) == _norm(gold_value)


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def score_extractions(
    extractions: list[Extraction],
    gold: dict[str, dict],
    documents: dict[int, Document],
) -> Scorecard:
    """Pure scoring core (no database writes) — easy to unit test."""
    collector: dict[str, list[bool]] = {name: [] for name in GOLD_FIELDS}
    confusion: dict[str, Counter] = defaultdict(Counter)
    mismatched: list[str] = []
    unscored: list[str] = []

    review = ReviewMetrics()
    n_docs = 0

    for extraction in extractions:
        document = documents.get(extraction.document_id)
        if document is None:  # pragma: no cover - defensive
            continue
        name = _basename(document.source_path)
        gold_row = gold.get(name)
        if gold_row is None:
            unscored.append(name)
            continue
        n_docs += 1
        doc_wrong = False
        for field_name in GOLD_FIELDS:
            ok = field_matches(
                field_name,
                getattr(extraction, field_name, None),
                gold_row.get(field_name, ""),
            )
            collector[field_name].append(ok)
            if not ok:
                doc_wrong = True
        predicted = gold_row["category"]
        observed = extraction.category
        confusion[predicted][_norm(observed) or "<null>"] += 1
        if doc_wrong:
            mismatched.append(name)
        if extraction.needs_review and doc_wrong:
            review.tp += 1
        elif extraction.needs_review and not doc_wrong:
            review.fp += 1
            review.flagged_but_correct += 1
        elif not extraction.needs_review and doc_wrong:
            review.fn += 1
            review.wrong_but_not_flagged += 1
        else:
            review.tn += 1
        if extraction.needs_review:
            review.flagged += 1

    fields: dict[str, FieldScore] = {}
    for name in GOLD_FIELDS:
        results = collector[name]
        hits = sum(1 for r in results if r)
        fields[name] = FieldScore(
            field_name=name,
            n=len(results),
            exact_match=hits,
            accuracy=(hits / len(results)) if results else 0.0,
        )

    scored_fields = [f for f in fields.values() if f.n > 0]
    macro = (
        sum(f.accuracy for f in scored_fields) / len(scored_fields) if scored_fields else 0.0
    )
    denom = review.tp + review.fp
    review.precision = (review.tp / denom) if denom else 0.0
    denom = review.tp + review.fn
    review.recall = (review.tp / denom) if denom else 0.0

    return Scorecard(
        run_id=extractions[0].run_id if extractions else "",
        n_docs=n_docs,
        fields=fields,
        macro_accuracy=macro,
        confusion={k: dict(v) for k, v in confusion.items()},
        review=review,
        mismatched_docs=mismatched,
        unscored_docs=unscored,
    )


def score_run(
    run_id: str,
    session: Session,
    settings: Settings | None = None,
    persist: bool = True,
) -> Scorecard:
    """Score a run against the gold set and (by default) persist to ``scores``."""
    settings = settings or load_settings()
    gold = load_gold(Path(settings.gold_dir) / "gold_labels.csv")
    extractions = list(
        session.scalars(select(Extraction).where(Extraction.run_id == run_id).order_by(Extraction.id))
    )
    doc_ids = {e.document_id for e in extractions}
    documents = {
        doc.id: doc for doc in session.scalars(select(Document).where(Document.id.in_(doc_ids)))
    }

    card = score_extractions(extractions, gold, documents)

    reviews = list(
        session.scalars(
            select(Review).join(Extraction, Review.extraction_id == Extraction.id).where(
                Extraction.run_id == run_id
            )
        )
    )
    card.corrections = len(reviews)
    card.corrected_fields = sorted({r.field_name for r in reviews})

    if persist:
        session.execute(delete(Score).where(Score.run_id == run_id))
        for row in card.to_rows():
            session.add(
                Score(
                    run_id=run_id,
                    field_name=row["field_name"],
                    n=int(row["n"] or 0),
                    exact_match=int(row["exact_match"] or 0),
                    accuracy=float(row["accuracy"]),
                )
            )
        session.commit()
    return card


# --------------------------------------------------------------------------
# Report artifacts
# --------------------------------------------------------------------------


def write_scorecard(card: Scorecard, reports_dir: str | Path = "reports") -> tuple[Path, Path]:
    """Write ``scorecard_<run>.csv`` and ``.md`` and return both paths."""
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    csv_path = reports_dir / f"scorecard_{card.run_id}.csv"
    md_path = reports_dir / f"scorecard_{card.run_id}.md"

    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["field_name", "n", "exact_match", "accuracy"])
        writer.writeheader()
        writer.writerows(card.to_rows())

    md_path.write_text(card.to_markdown(), encoding="utf-8")
    return csv_path, md_path


def main_cli(run_id: str, database_url: str | None = None, settings: Settings | None = None):
    settings = settings or load_settings()
    db = get_database(database_url or settings.database_url)
    db.create_all()
    with db.session_scope() as session:
        card = score_run(run_id, session, settings)
    paths = write_scorecard(card, settings.reports_dir)
    return card, paths

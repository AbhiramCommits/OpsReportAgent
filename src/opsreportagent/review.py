"""Human reviewer queue + correction audit log.

Low-confidence extractions are routed to a review queue. A reviewer can correct
individual fields; every change is written to the ``reviews`` audit table and the
affected run is re-scored immediately.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings, load_settings
from .db import Document, Extraction, Review
from .scoring import Scorecard, score_run

CORRECTABLE_FIELDS = Extraction.CORRECTABLE_FIELDS
NUMERIC_FIELDS = ("dollar_impact",)
DATE_FIELDS = ("incident_date",)


class ReviewError(ValueError):
    """Raised for invalid reviewer input."""


@dataclass
class FixResult:
    extraction_id: int
    run_id: str
    field_name: str
    old_value: str | None
    new_value: str | None
    reviewer: str
    score_before: Scorecard | None = None
    score_after: Scorecard | None = None


def _coerce_dollar(value: str) -> float | None:
    cleaned = str(value).strip().replace("$", "").replace(",", "")
    if cleaned == "":
        return None
    return float(cleaned)


def _coerce_date(value: str) -> str | None:
    """Accept ISO or M/D/YY and normalise to ISO."""
    cleaned = str(value).strip()
    if cleaned == "":
        return None
    try:
        return date.fromisoformat(cleaned).isoformat()
    except ValueError:
        pass
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(cleaned, fmt).date().isoformat()
        except ValueError:
            continue
    raise ReviewError(f"Unparseable date value: {value!r} (use ISO YYYY-MM-DD)")


def _coerce(field_name: str, value):
    if field_name in NUMERIC_FIELDS:
        return _coerce_dollar(value)
    if field_name in DATE_FIELDS:
        return _coerce_date(value)
    cleaned = str(value).strip()
    return cleaned or None


def list_queue(
    session: Session,
    run_id: str | None = None,
    only_flagged: bool = True,
    limit: int | None = None,
) -> list[Extraction]:
    """Return review-queue rows, lowest confidence first."""
    stmt = select(Extraction)
    if run_id:
        stmt = stmt.where(Extraction.run_id == run_id)
    if only_flagged:
        stmt = stmt.where(Extraction.needs_review.is_(True))
    stmt = stmt.order_by(Extraction.confidence.asc(), Extraction.id.asc())
    if limit:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt))


def apply_fix(
    session: Session,
    extraction_id: int,
    field_name: str,
    value: str,
    reviewer: str,
    note: str | None = None,
    settings: Settings | None = None,
    rescore: bool = True,
) -> FixResult:
    """Correct one field, log the change, and re-score the run."""
    if field_name not in CORRECTABLE_FIELDS:
        raise ReviewError(
            f"field {field_name!r} is not correctable; choose from {list(CORRECTABLE_FIELDS)}"
        )
    if not reviewer or not reviewer.strip():
        raise ReviewError("a reviewer name is required")

    extraction = session.get(Extraction, extraction_id)
    if extraction is None:
        raise ReviewError(f"no extraction with id={extraction_id}")

    settings = settings or load_settings()
    before = score_run(extraction.run_id, session, settings, persist=False) if rescore else None

    old_value = getattr(extraction, field_name)
    new_value = _coerce(field_name, value)
    setattr(extraction, field_name, new_value)

    review = Review(
        extraction_id=extraction.id,
        field_name=field_name,
        old_value=None if old_value is None else str(old_value),
        new_value=None if new_value is None else str(new_value),
        reviewer=reviewer.strip(),
        note=note,
    )
    session.add(review)
    session.flush()

    after = score_run(extraction.run_id, session, settings, persist=True) if rescore else None

    return FixResult(
        extraction_id=extraction.id,
        run_id=extraction.run_id,
        field_name=field_name,
        old_value=review.old_value,
        new_value=review.new_value,
        reviewer=reviewer.strip(),
        score_before=before,
        score_after=after,
    )


def export_corrections(session: Session, out_path: str | Path) -> Path:
    """Dump every logged correction to CSV for prompt revision work."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows = session.execute(
        select(Review, Extraction, Document)
        .join(Extraction, Review.extraction_id == Extraction.id)
        .join(Document, Extraction.document_id == Document.id)
        .order_by(Review.id)
    ).all()

    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "review_id",
                "created_at",
                "run_id",
                "extraction_id",
                "source_file",
                "doc_type",
                "field_name",
                "old_value",
                "new_value",
                "reviewer",
                "note",
            ]
        )
        for review, extraction, document in rows:
            created = review.created_at
            if isinstance(created, datetime):
                created = created.astimezone(UTC).isoformat()
            writer.writerow(
                [
                    review.id,
                    created,
                    extraction.run_id,
                    extraction.id,
                    Path(document.source_path).name,
                    document.doc_type,
                    review.field_name,
                    review.old_value if review.old_value is not None else "",
                    review.new_value if review.new_value is not None else "",
                    review.reviewer,
                    review.note or "",
                ]
            )
    return out_path

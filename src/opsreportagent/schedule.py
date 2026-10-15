"""Scheduled job entrypoint: ingest -> run -> score -> report.

Designed to be cron-friendly. Each stage is timed and logged to stdout, and the
process exits non-zero on failure so a scheduler/cron job can alert.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .config import Settings, load_settings
from .db import get_database
from .ingest import ingest_dir
from .pipeline import run_batch
from .report import build_report
from .scoring import score_run, write_scorecard


@dataclass
class Stage:
    name: str
    seconds: float
    detail: str


@dataclass
class ScheduleResult:
    run_id: str
    stages: list[Stage] = field(default_factory=list)
    artifacts: list[Path] = field(default_factory=list)
    macro_accuracy: float = 0.0
    category_accuracy: float = 0.0
    flagged: int = 0

    @property
    def total_seconds(self) -> float:
        return sum(stage.seconds for stage in self.stages)

    def render(self) -> str:
        lines = [f"scheduled weekly job — run_id={self.run_id}"]
        for stage in self.stages:
            lines.append(f"  [{stage.seconds:6.2f}s] {stage.name:<8} {stage.detail}")
        lines.append(f"  [{self.total_seconds:6.2f}s] TOTAL")
        lines.append(
            f"  macro accuracy={self.macro_accuracy:.3f} "
            f"category accuracy={self.category_accuracy:.3f} flagged={self.flagged}"
        )
        for path in self.artifacts:
            lines.append(f"  artifact: {path}")
        return "\n".join(lines)


def default_run_id(when: date | None = None) -> str:
    when = when or date.today()
    return f"run-{when:%Y%m%d}"


def run_weekly(
    run_id: str | None = None,
    settings: Settings | None = None,
    threshold: float | None = None,
    prompt_version: str = "v1",
    corpus_path: str | None = None,
    weeks: int = 6,
    log=print,
) -> ScheduleResult:
    """Run the full weekly job end to end, logging each stage with timings."""
    settings = settings or load_settings()
    run_id = run_id or default_run_id()
    corpus_path = corpus_path or settings.corpus_dir
    settings.ensure_dirs()

    result = ScheduleResult(run_id=run_id)
    db = get_database(settings.database_url)
    db.create_all()

    def timed(name: str, fn):
        log(f"[{name}] start")
        start = time.perf_counter()
        value = fn()
        elapsed = time.perf_counter() - start
        return value, elapsed

    # 1. ingest ------------------------------------------------------------
    def _ingest():
        return ingest_dir(corpus_path, database_url=settings.database_url)

    ingest_result, elapsed = timed("ingest", _ingest)
    result.stages.append(Stage("ingest", elapsed, ingest_result.summary()))
    log(f"[ingest] done in {elapsed:.2f}s — {ingest_result.summary()}")

    # 2. extract -----------------------------------------------------------
    def _run():
        with db.session_scope() as session:
            return run_batch(
                session,
                settings,
                run_id=run_id,
                threshold=threshold,
                prompt_version=prompt_version,
            )

    summary, elapsed = timed("run", _run)
    result.stages.append(
        Stage("run", elapsed, f"{summary.extracted} extracted, {summary.needs_review} flagged")
    )
    result.flagged = summary.needs_review
    log(f"[run] done in {elapsed:.2f}s — extracted={summary.extracted} flagged={summary.needs_review}")

    # 3. score -------------------------------------------------------------
    def _score():
        with db.session_scope() as session:
            card = score_run(run_id, session, settings)
        return card, write_scorecard(card, settings.reports_dir)

    (card, paths), elapsed = timed("score", _score)
    result.macro_accuracy = card.macro_accuracy
    result.category_accuracy = card.category_accuracy
    result.stages.append(
        Stage("score", elapsed, f"macro={card.macro_accuracy:.3f} category={card.category_accuracy:.3f}")
    )
    result.artifacts.extend(paths)
    log(f"[score] done in {elapsed:.2f}s — macro={card.macro_accuracy:.3f} category={card.category_accuracy:.3f}")

    # 4. report ------------------------------------------------------------
    def _report():
        return build_report(run_id, weeks=weeks, settings=settings)

    artifacts, elapsed = timed("report", _report)
    result.stages.append(
        Stage("report", elapsed, f"{len(artifacts.figures)} charts + md/html")
    )
    result.artifacts.extend([artifacts.md_path, artifacts.html_path])
    result.artifacts.extend(artifacts.figures.values())
    log(f"[report] done in {elapsed:.2f}s — {len(artifacts.figures)} charts")

    log(result.render())
    return result

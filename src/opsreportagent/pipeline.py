"""LangGraph extraction pipeline.

A :class:`~langgraph.graph.StateGraph` runs one document through six nodes::

    load -> summarize -> classify -> extract -> validate -> persist

``summarize``, ``classify`` and ``extract`` each call an LLM with a versioned
prompt and validate the response against that prompt's JSON schema (with one
repair retry). ``validate`` merges the results and applies the confidence
threshold to decide ``needs_review``. ``persist`` writes the extraction row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .db import Document, Extraction, Run, utcnow
from .llm import get_llm, structured_complete
from .prompt_library import Prompt, load_prompt
from .taxonomy import TAXONOMY

DEFAULT_THRESHOLD = 0.75


class PipelineState(TypedDict, total=False):
    document_id: int
    source_path: str
    doc_type: str
    raw_text: str
    summary: str
    key_points: list[str]
    category: str | None
    subcategory: str | None
    classified_category: str | None
    classified_subcategory: str | None
    store_id: str | None
    incident_date: str | None
    vendor: str | None
    dollar_impact: float | None
    severity: str | None
    classify_confidence: float
    confidence: float
    needs_review: bool
    hard_failed: bool
    summary_errors: list[str]
    classify_errors: list[str]
    extract_errors: list[str]
    extraction_id: int | None


@dataclass
class PipelineContext:
    settings: Settings
    session: Session
    run_id: str
    prompt_version: str
    threshold: float
    llm: Any
    summarize_prompt: Prompt
    classify_prompt: Prompt
    extract_prompt: Prompt

    @property
    def hints(self) -> dict:
        return self.extract_prompt.stub_hints or {}


@dataclass
class RunSummary:
    run_id: str
    doc_count: int
    extracted: int
    needs_review: int
    hard_failed: int
    provider: str
    model: str
    prompt_version: str
    prompt_hash: str
    elapsed_seconds: float = 0.0
    failures: list[str] = field(default_factory=list)

    def render(self) -> str:
        minutes, seconds = divmod(self.elapsed_seconds, 60)
        lines = [
            f"run_id:          {self.run_id}",
            f"provider:        {self.provider} ({self.model})",
            f"prompt:          extract.{self.prompt_version} sha256={self.prompt_hash[:12]}",
            f"documents:       {self.doc_count}",
            f"extracted:       {self.extracted}",
            f"flagged review:  {self.needs_review}",
            f"hard failures:   {self.hard_failed}",
        ]
        if self.failures:
            lines.append(f"failure detail:  {self.failures[:3]}")
        lines.append(f"elapsed:         {int(minutes)}m {seconds:.2f}s")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Nodes
# --------------------------------------------------------------------------


def _node_load(state: PipelineState, ctx: PipelineContext) -> PipelineState:
    document = ctx.session.get(Document, state["document_id"])
    if document is None:  # pragma: no cover - defensive
        raise LookupError(f"document {state['document_id']} not found")
    return {
        "source_path": document.source_path,
        "doc_type": document.doc_type,
        "raw_text": document.raw_text,
    }


def _node_summarize(state: PipelineState, ctx: PipelineContext) -> PipelineState:
    inputs = {"document": state["raw_text"], "taxonomy": TAXONOMY}
    completion = structured_complete(ctx.llm, ctx.summarize_prompt, inputs, ctx.hints)
    if not completion.ok:
        return {"summary": "", "key_points": [], "summary_errors": completion.errors}
    return {
        "summary": completion.payload["summary"],
        "key_points": completion.payload["key_points"],
        "summary_errors": [],
    }


def _node_classify(state: PipelineState, ctx: PipelineContext) -> PipelineState:
    inputs = {"document": state["raw_text"], "taxonomy": TAXONOMY}
    completion = structured_complete(ctx.llm, ctx.classify_prompt, inputs, ctx.hints)
    if not completion.ok:
        return {"classify_errors": completion.errors, "classify_confidence": 0.0}
    return {
        "classified_category": completion.payload["category"],
        "classified_subcategory": completion.payload.get("subcategory"),
        "classify_confidence": float(completion.payload["confidence"]),
        "classify_errors": [],
    }


def _node_extract(state: PipelineState, ctx: PipelineContext) -> PipelineState:
    inputs = {
        "document": state["raw_text"],
        "taxonomy": TAXONOMY,
        "today": date.today().isoformat(),
    }
    completion = structured_complete(ctx.llm, ctx.extract_prompt, inputs, ctx.hints)
    if not completion.ok:
        return {"extract_errors": completion.errors, "hard_failed": True}
    payload = completion.payload
    return {
        "category": payload["category"],
        "subcategory": payload.get("subcategory"),
        "store_id": payload.get("store_id"),
        "incident_date": payload.get("incident_date"),
        "vendor": payload.get("vendor"),
        "dollar_impact": payload.get("dollar_impact"),
        "severity": payload.get("severity"),
        "summary": payload.get("summary") or state.get("summary", ""),
        "confidence": float(payload["confidence"]),
        "extract_errors": [],
        "hard_failed": False,
    }


def _node_validate(state: PipelineState, ctx: PipelineContext) -> PipelineState:
    hard_failed = bool(state.get("hard_failed"))
    confidence = float(state.get("confidence", 0.0))

    if hard_failed:
        return {"confidence": 0.0, "needs_review": True, "hard_failed": True}

    # Disagreement between the dedicated classifier and the extractor is a
    # strong signal of uncertainty: keep the extractor's answer and lower
    # confidence so the row is routed to a human.
    classified = state.get("classified_category")
    extracted = state.get("category")
    disagreement = bool(state.get("classify_errors")) or (
        classified is not None and extracted is not None and classified != extracted
    )
    if disagreement:
        confidence = min(confidence, 0.5)

    return {
        "category": extracted or classified,
        "subcategory": state.get("subcategory") or state.get("classified_subcategory"),
        "confidence": confidence,
        "needs_review": confidence < ctx.threshold,
        "hard_failed": False,
    }


def _node_persist(state: PipelineState, ctx: PipelineContext) -> PipelineState:
    extraction = Extraction(
        document_id=state["document_id"],
        run_id=ctx.run_id,
        category=state.get("category"),
        subcategory=state.get("subcategory"),
        store_id=state.get("store_id"),
        incident_date=state.get("incident_date"),
        vendor=state.get("vendor"),
        dollar_impact=state.get("dollar_impact"),
        severity=state.get("severity"),
        summary=state.get("summary"),
        confidence=float(state.get("confidence", 0.0)),
        needs_review=bool(state.get("needs_review", True)),
        prompt_version=ctx.prompt_version,
        prompt_hash=ctx.extract_prompt.sha256,
        model=ctx.settings.model,
    )
    ctx.session.add(extraction)
    ctx.session.flush()
    return {"extraction_id": extraction.id}


NODE_ORDER = ("load", "summarize", "classify", "extract", "validate", "persist")


def build_graph(ctx: PipelineContext):
    """Compile the LangGraph state machine for a single document."""
    graph = StateGraph(PipelineState)
    graph.add_node("load", lambda state: _node_load(state, ctx))
    graph.add_node("summarize", lambda state: _node_summarize(state, ctx))
    graph.add_node("classify", lambda state: _node_classify(state, ctx))
    graph.add_node("extract", lambda state: _node_extract(state, ctx))
    graph.add_node("validate", lambda state: _node_validate(state, ctx))
    graph.add_node("persist", lambda state: _node_persist(state, ctx))

    graph.add_edge(START, "load")
    for source, target in zip(NODE_ORDER, NODE_ORDER[1:], strict=False):
        graph.add_edge(source, target)
    graph.add_edge("persist", END)
    return graph.compile()


# --------------------------------------------------------------------------
# Batch driver
# --------------------------------------------------------------------------


def unextracted_documents(session: Session, run_id: str) -> list[Document]:
    """Documents with no extraction row for ``run_id`` yet."""
    stmt = (
        select(Document)
        .where(
            ~Document.id.in_(
                select(Extraction.document_id).where(Extraction.run_id == run_id)
            )
        )
        .order_by(Document.id)
    )
    return list(session.scalars(stmt))


def make_context(
    session: Session,
    settings: Settings,
    run_id: str,
    threshold: float | None = None,
    prompt_version: str = "v1",
    llm: Any = None,
) -> PipelineContext:
    return PipelineContext(
        settings=settings,
        session=session,
        run_id=run_id,
        prompt_version=prompt_version,
        threshold=settings.confidence_threshold if threshold is None else threshold,
        llm=llm if llm is not None else get_llm(settings),
        summarize_prompt=load_prompt("summarize", "v1"),
        classify_prompt=load_prompt("classify", "v1"),
        extract_prompt=load_prompt("extract", prompt_version),
    )


def run_batch(
    session: Session,
    settings: Settings,
    run_id: str,
    threshold: float | None = None,
    prompt_version: str = "v1",
    limit: int | None = None,
    llm: Any = None,
) -> RunSummary:
    """Run the extraction graph over all not-yet-extracted documents."""
    started = datetime.now()
    if threshold is None:
        threshold = settings.confidence_threshold
    documents = unextracted_documents(session, run_id)
    if limit is not None:
        documents = documents[:limit]

    ctx = make_context(session, settings, run_id, threshold, prompt_version, llm)
    graph = build_graph(ctx)

    run = session.scalar(select(Run).where(Run.run_id == run_id))
    if run is None:
        run = Run(
            run_id=run_id,
            provider=settings.llm_provider,
            model=settings.model,
            prompt_version=prompt_version,
        )
        session.add(run)
    run.started_at = utcnow()
    run.provider = settings.llm_provider
    run.model = settings.model
    run.prompt_version = prompt_version
    session.flush()

    extracted = 0
    needs_review = 0
    hard_failed = 0
    failures: list[str] = []

    for document in documents:
        try:
            final = graph.invoke({"document_id": document.id})
        except Exception as exc:  # noqa: BLE001 - keep the batch going
            failures.append(f"{document.source_path}: {exc}")
            hard_failed += 1
            continue
        extracted += 1
        if final.get("needs_review"):
            needs_review += 1
        if final.get("hard_failed"):
            hard_failed += 1
        session.commit()

    run.finished_at = utcnow()
    run.doc_count = extracted
    run.notes = (
        f"threshold={threshold} needs_review={needs_review} hard_failed={hard_failed}"
    )
    session.commit()

    elapsed = (datetime.now() - started).total_seconds()
    return RunSummary(
        run_id=run_id,
        doc_count=len(documents),
        extracted=extracted,
        needs_review=needs_review,
        hard_failed=hard_failed,
        provider=settings.llm_provider,
        model=settings.model,
        prompt_version=prompt_version,
        prompt_hash=ctx.extract_prompt.sha256,
        elapsed_seconds=elapsed,
        failures=failures,
    )

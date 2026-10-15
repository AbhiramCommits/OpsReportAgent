"""LLM providers and schema-validated structured completion.

Three providers are supported, selected with ``LLM_PROVIDER``:

* ``stub``      - deterministic offline provider (regex + keyword heuristics).
                  Used by the test-suite and the demo; no API key required.
* ``openai``    - LangChain ``ChatOpenAI``.
* ``anthropic`` - LangChain ``ChatAnthropic``.

Every provider must return JSON that validates against the prompt's
``output_schema``. On validation failure the call is retried once with a repair
instruction; if the repair also fails the caller records a hard failure
(``confidence = 0.0``, ``needs_review = True``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr

from .config import Settings
from .prompt_library import Prompt

# Markers used by tests to exercise the repair-retry and hard-fail paths.
FORCE_INVALID_ONCE = "[[FORCE_INVALID_ONCE]]"
FORCE_INVALID_ALWAYS = "[[FORCE_INVALID_ALWAYS]]"

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

FIELD_LABEL_PREFIXES = (
    "reference:", "store:", "date:", "vendor:", "severity:", "reported by:",
    "prepared by:", "estimated impact:", "impact:", "status:", "from:", "to:",
    "subject:", "thanks,", "narrative:", "=====", "retailco store operations",
    "vendor notice",
)

DEFAULT_HINTS: dict[str, Any] = {
    "store_pattern": r"\bST-[0-9]{3}\b",
    "date_patterns": ["long", "iso"],
    "currency_patterns": ["dollar_symbol"],
    "severity_values": ["Low", "Medium", "High", "Critical"],
    "category_keywords": {},
    "subcategory_keywords": {},
}


# --------------------------------------------------------------------------
# Deterministic parsing helpers (used by the stub provider)
# --------------------------------------------------------------------------


def _match_category(low_text: str, hints: dict) -> tuple[str | None, str | None]:
    """Return the first taxonomy category whose keyword appears in the text.

    Keywords are anchored with a leading word boundary so that, e.g., the
    keyword ``truck`` does not fire inside ``struck``.
    """
    for category, keywords in (hints.get("category_keywords") or {}).items():
        for keyword in keywords:
            if re.search(r"\b" + re.escape(str(keyword).lower()), low_text):
                return category, str(keyword)
    return None, None


def _find_store(text: str, hints: dict) -> str | None:
    pattern = hints.get("store_pattern") or DEFAULT_HINTS["store_pattern"]
    match = re.search(pattern, text)
    return match.group(0) if match else None


def _find_date(text: str, hints: dict) -> str | None:
    patterns = hints.get("date_patterns") or ["long", "iso"]
    for name in patterns:
        if name == "long":
            m = re.search(
                r"\b(" + "|".join(m.capitalize() for m in MONTHS) + r")\s+(\d{1,2}),\s*(\d{4})\b",
                text,
            )
            if m:
                month = MONTHS[m.group(1).lower()]
                return f"{int(m.group(3)):04d}-{month:02d}-{int(m.group(2)):02d}"
        elif name == "iso":
            m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
            if m:
                return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        elif name == "short":
            m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", text)
            if m:
                month, day, year = int(m.group(1)), int(m.group(2)), m.group(3)
                year_int = int(year) if len(year) == 4 else 2000 + int(year)
                return f"{year_int:04d}-{month:02d}-{day:02d}"
    return None


def _find_vendor(text: str) -> str | None:
    m = re.search(r"(?im)^\s*Vendor:\s*(.+?)\s*$", text)
    return m.group(1).strip() if m else None


def _find_severity(text: str, hints: dict) -> str | None:
    values = hints.get("severity_values") or DEFAULT_HINTS["severity_values"]
    m = re.search(r"(?im)^\s*Severity:\s*(" + "|".join(values) + r")\b", text)
    return m.group(1) if m else None


def _parse_amount(raw: str) -> float:
    return float(raw.replace(",", ""))


def _find_dollar(text: str, hints: dict) -> float | None:
    patterns = hints.get("currency_patterns") or ["dollar_symbol"]
    for name in patterns:
        if name == "dollar_symbol":
            m = re.search(r"\$\s*([\d,]+(?:\.\d{1,2})?)", text)
            if m:
                return _parse_amount(m.group(1))
        elif name == "word":
            m = re.search(r"([\d,]+(?:\.\d{1,2})?)\s*dollars?\b", text, re.IGNORECASE)
            if m:
                return _parse_amount(m.group(1))
    return None


def _find_subcategory(low_text: str, hints: dict) -> str | None:
    for subcategory, keywords in (hints.get("subcategory_keywords") or {}).items():
        for keyword in keywords:
            if re.search(r"\b" + re.escape(str(keyword).lower()), low_text):
                return subcategory
    return None


def _build_summary(text: str) -> str:
    lines = [line.strip() for line in text.splitlines()]
    for line in lines:
        if not line:
            continue
        if line.lower().startswith(FIELD_LABEL_PREFIXES):
            continue
        if len(line) > 15:
            return line[:220]
    return "Document contains insufficient detail for a summary."


WEIGHTS = {
    "store_id": 0.16,
    "incident_date": 0.17,
    "category": 0.20,
    "vendor": 0.11,
    "dollar_impact": 0.15,
    "severity": 0.14,
}
BASE_CONFIDENCE = 0.07
MAX_CONFIDENCE = 0.97


@dataclass
class Analysis:
    """Deterministic analysis of one document."""

    category: str | None
    matched_keyword: str | None
    subcategory: str | None
    store_id: str | None
    incident_date: str | None
    vendor: str | None
    dollar_impact: float | None
    severity: str | None
    summary: str
    confidence: float
    key_points: list[str] = field(default_factory=list)


def analyze_document(document: str, hints: dict | None = None) -> Analysis:
    """Apply the deterministic heuristics shared by the stub provider."""
    hints = {**DEFAULT_HINTS, **(hints or {})}
    low = document.lower()
    category, matched = _match_category(low, hints)
    store_id = _find_store(document, hints)
    incident_date = _find_date(document, hints)
    vendor = _find_vendor(document)
    dollar = _find_dollar(document, hints)
    severity = _find_severity(document, hints)
    subcategory = _find_subcategory(low, hints)

    confidence = BASE_CONFIDENCE
    if store_id:
        confidence += WEIGHTS["store_id"]
    if incident_date:
        confidence += WEIGHTS["incident_date"]
    if matched:
        confidence += WEIGHTS["category"]
    if vendor:
        confidence += WEIGHTS["vendor"]
    if dollar is not None:
        confidence += WEIGHTS["dollar_impact"]
    if severity:
        confidence += WEIGHTS["severity"]

    key_points = []
    for label, value in (
        ("Store", store_id),
        ("Date", incident_date),
        ("Vendor", vendor),
        ("Severity", severity),
        ("Impact", None if dollar is None else f"{dollar:.2f}"),
    ):
        if value:
            key_points.append(f"{label}: {value}")

    return Analysis(
        category=category,
        matched_keyword=matched,
        subcategory=subcategory,
        store_id=store_id,
        incident_date=incident_date,
        vendor=vendor,
        dollar_impact=dollar,
        severity=severity,
        summary=_build_summary(document),
        confidence=round(min(confidence, MAX_CONFIDENCE), 3),
        key_points=key_points or ["No structured fields found in document."],
    )


# --------------------------------------------------------------------------
# Stub provider
# --------------------------------------------------------------------------


class StubLLM(BaseChatModel):
    """Deterministic offline "LLM".

    Implemented as a LangChain chat model so it is drop-in compatible with the
    LangGraph pipeline, but its answers come from regex/keyword heuristics keyed
    off the prompt's ``stub_hints`` rather than from a network call. This makes
    the whole pipeline reproducible without API keys.
    """

    model_name: str = "stub-deterministic-v1"

    _counters: dict = PrivateAttr(default_factory=dict)

    @property
    def _llm_type(self) -> str:
        return "stub"

    # -- structured entry point used by the pipeline -----------------------
    def complete(
        self,
        prompt: Prompt,
        inputs: dict,
        hints: dict | None = None,
    ) -> dict:
        """Return schema-shaped JSON for ``prompt`` deterministically."""
        document = str(inputs.get("document", ""))
        effective_hints = hints if hints is not None else (prompt.stub_hints or {})

        key = (prompt.ref, hash(document))
        count = self._counters.get(key, 0)
        self._counters[key] = count + 1

        if FORCE_INVALID_ALWAYS in document:
            return {"unexpected": "always-invalid"}
        if FORCE_INVALID_ONCE in document and count == 0:
            return {"unexpected": "first-attempt-invalid"}

        analysis = analyze_document(document, effective_hints)

        if prompt.name == "summarize":
            return {
                "summary": analysis.summary,
                "key_points": analysis.key_points,
            }
        if prompt.name == "classify":
            return {
                "category": analysis.category or _fallback_category(effective_hints),
                "subcategory": analysis.subcategory,
                "confidence": 0.9 if analysis.matched_keyword else 0.3,
                "rationale": (
                    f"Matched keyword {analysis.matched_keyword!r} in the document."
                    if analysis.matched_keyword
                    else "No taxonomy keyword present; low-confidence guess."
                ),
            }
        if prompt.name == "extract":
            return {
                "category": analysis.category or _fallback_category(effective_hints),
                "subcategory": analysis.subcategory,
                "store_id": analysis.store_id,
                "incident_date": analysis.incident_date,
                "vendor": analysis.vendor,
                "dollar_impact": analysis.dollar_impact,
                "severity": analysis.severity,
                "summary": analysis.summary,
                "confidence": analysis.confidence,
            }
        if prompt.name == "exec_summary":
            return _exec_summary_from_stats(inputs, effective_hints)
        raise ValueError(f"StubLLM has no handler for prompt {prompt.ref!r}")

    # -- LangChain chat interface -----------------------------------------
    def _generate(
        self,
        messages: list,
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        text = ""
        for message in reversed(messages):
            if isinstance(message, HumanMessage):
                text = str(message.content)
                break
        analysis = analyze_document(text)
        payload = {
            "category": analysis.category or "Equipment Failure",
            "subcategory": analysis.subcategory,
            "store_id": analysis.store_id,
            "incident_date": analysis.incident_date,
            "vendor": analysis.vendor,
            "dollar_impact": analysis.dollar_impact,
            "severity": analysis.severity,
            "summary": analysis.summary,
            "confidence": analysis.confidence,
        }
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=json.dumps(payload)))])


def _fallback_category(hints: dict) -> str:
    keywords = hints.get("category_keywords") or {}
    if keywords:
        return next(iter(keywords))
    return "Equipment Failure"


def _fmt_money(value) -> str:
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "n/a"


def _exec_summary_from_stats(inputs: dict, hints: dict) -> dict:
    """Render an executive summary purely from the supplied statistics JSON."""
    stats = inputs.get("stats", {})
    if isinstance(stats, str):
        try:
            stats = json.loads(stats)
        except json.JSONDecodeError:  # pragma: no cover - defensive
            stats = {}
    if not isinstance(stats, dict):  # pragma: no cover - defensive
        stats = {}

    total_docs = stats.get("total_documents", 0)
    weeks = stats.get("weeks", 0)
    total_dollar = stats.get("total_dollar_impact", 0.0)
    flagged = stats.get("flagged_for_review", 0)
    period = stats.get("period", "")
    top_category = stats.get("top_category") or {}
    top_vendor = stats.get("top_vendor") or {}
    top_store = stats.get("top_store") or {}
    severity_counts = stats.get("severity_counts") or {}

    sentences = []
    sentences.append(
        f"OpsReportAgent processed {total_docs} operations documents across {weeks} weeks"
        + (f" ({period})" if period else "")
        + f", with a total estimated dollar impact of {_fmt_money(total_dollar)}."
    )
    if top_category:
        sentences.append(
            f"The highest-volume category was {top_category.get('name')} with "
            f"{top_category.get('count')} incidents and "
            f"{_fmt_money(top_category.get('dollar'))} of estimated impact."
        )
    if top_store:
        sentences.append(
            f"Store {top_store.get('id')} carried the largest single-store impact at "
            f"{_fmt_money(top_store.get('dollar'))}."
        )
    if severity_counts:
        ranked = sorted(severity_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        sentences.append(
            "Severity mix: "
            + ", ".join(f"{name} {count}" for name, count in ranked)
            + "."
        )
    sentences.append(
        f"{flagged} extraction(s) fell below the confidence threshold and were routed "
        f"to the human review queue."
    )

    highlights = [f"{total_docs} documents processed over {weeks} weeks"]
    if top_category:
        highlights.append(
            f"{top_category.get('name')}: {top_category.get('count')} incidents, "
            f"{_fmt_money(top_category.get('dollar'))} impact"
        )
    if top_vendor:
        highlights.append(f"Top vendor by volume: {top_vendor.get('name')} ({top_vendor.get('count')} documents)")
    if top_store:
        highlights.append(f"Highest-impact store: {top_store.get('id')} ({_fmt_money(top_store.get('dollar'))})")
    highlights.append(f"{_fmt_money(total_dollar)} total estimated impact")
    highlights.append(f"{flagged} extraction(s) flagged for human review")

    limit = int(hints.get("max_highlights", 6) or 6)
    return {"summary": " ".join(sentences), "highlights": highlights[:limit]}


# --------------------------------------------------------------------------
# LangChain providers
# --------------------------------------------------------------------------


class LangChainProvider:
    """Wraps a real LangChain chat model (OpenAI / Anthropic)."""

    def __init__(self, model: BaseChatModel, provider: str):
        self.model = model
        self.provider = provider

    def messages_for(self, prompt: Prompt, inputs: dict, repair: str | None = None) -> list:
        system = (
            f"{prompt.system}\n\n"
            "Respond with a single JSON object that validates against this JSON Schema:\n"
            f"{prompt.schema_json()}"
        )
        if repair:
            system += f"\n\nYour previous response was rejected: {repair}\nReturn corrected JSON only."
        return [
            SystemMessage(content=system),
            HumanMessage(content=prompt.render(**inputs)),
        ]

    def invoke_raw(self, prompt: Prompt, inputs: dict, repair: str | None = None) -> str:
        response = self.model.invoke(self.messages_for(prompt, inputs, repair))
        content = response.content
        if isinstance(content, list):  # some providers return content blocks
            content = "".join(
                block.get("text", "") if isinstance(block, dict) else str(block) for block in content
            )
        return str(content)


def get_llm(settings: Settings) -> StubLLM | LangChainProvider:
    """Return the configured provider.

    ``stub`` returns a deterministic :class:`StubLLM`; ``openai`` and
    ``anthropic`` return a LangChain chat model wrapper.
    """
    settings.validate_for_provider()
    if settings.llm_provider == "stub":
        return StubLLM()
    if settings.llm_provider == "openai":
        from langchain_openai import ChatOpenAI

        model = ChatOpenAI(model=settings.model_name or "gpt-4o-mini", api_key=settings.openai_api_key)
        return LangChainProvider(model, "openai")
    if settings.llm_provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        model = ChatAnthropic(
            model=settings.model_name or "claude-3-5-sonnet-latest",
            api_key=settings.anthropic_api_key,
        )
        return LangChainProvider(model, "anthropic")
    raise ValueError(f"Unsupported LLM_PROVIDER: {settings.llm_provider!r}")


# --------------------------------------------------------------------------
# Validation + repair
# --------------------------------------------------------------------------


@dataclass
class Completion:
    payload: dict | None
    raw: str
    errors: list[str] = field(default_factory=list)
    attempts: int = 0
    repaired: bool = False

    @property
    def ok(self) -> bool:
        return self.payload is not None


def validate_payload(schema: dict, payload: Any) -> list[str]:
    """Return a list of human-readable validation errors (empty when valid)."""
    validator = Draft202012Validator(schema)
    return [
        f"{'/'.join(str(p) for p in error.absolute_path) or '<root>'}: {error.message}"
        for error in sorted(validator.iter_errors(payload), key=lambda e: list(e.absolute_path))
    ]


def extract_json(text: str) -> Any:
    """Best-effort JSON extraction from an LLM response."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\n?", "", stripped)
        stripped = re.sub(r"\n?```$", "", stripped).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise


def structured_complete(
    llm: StubLLM | LangChainProvider,
    prompt: Prompt,
    inputs: dict,
    hints: dict | None = None,
) -> Completion:
    """Call a provider and validate its JSON against the prompt's schema.

    Retries exactly once with a repair instruction on validation failure.
    """
    attempts = 0
    raw = ""
    errors: list[str] = []

    for repair_message in (None, "repair"):
        attempts += 1
        try:
            if isinstance(llm, StubLLM):
                payload = llm.complete(prompt, inputs, hints)
                raw = json.dumps(payload)
                candidate: Any = payload
            else:
                raw = llm.invoke_raw(prompt, inputs, None if repair_message is None else " / ".join(errors))
                candidate = extract_json(raw)
        except Exception as exc:  # noqa: BLE001 - surfaced as a validation error
            errors = [f"provider error: {exc}"]
            if repair_message:
                return Completion(None, raw, errors, attempts, True)
            continue

        errors = validate_payload(prompt.output_schema, candidate)
        if not errors:
            return Completion(candidate, raw, [], attempts, repair_message is not None)

    return Completion(None, raw, errors, attempts, True)

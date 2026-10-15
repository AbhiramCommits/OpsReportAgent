"""LLM providers: schema validation, the repair retry, and the hard-fail path."""

from __future__ import annotations

import json

import pytest

from opsreportagent.config import Settings
from opsreportagent.llm import (
    FORCE_INVALID_ALWAYS,
    FORCE_INVALID_ONCE,
    StubLLM,
    analyze_document,
    extract_json,
    get_llm,
    structured_complete,
    validate_payload,
)
from opsreportagent.prompt_library import load_prompt

EXTRACT_P = load_prompt("extract", "v1")
HINTS = EXTRACT_P.stub_hints

FULL_DOC = (
    "Reference: INC-0002\n"
    "Store: ST-011\n"
    "Date: September 20, 2026\n"
    "Vendor: Ridgeline Distribution\n"
    "Severity: High\n"
    "Narrative:\n"
    "The delivery arrived two pallets short of the order.\n"
    "Estimated impact: $2,420.33\n"
)


def _inputs(document: str) -> dict:
    return {"document": document, "taxonomy": "- Equipment Failure", "today": "2026-10-15"}


# -- deterministic analysis -------------------------------------------------


def test_analyze_document_extracts_all_fields():
    analysis = analyze_document(FULL_DOC, HINTS)
    assert analysis.category == "Vendor Delivery"
    assert analysis.store_id == "ST-011"
    assert analysis.incident_date == "2026-09-20"
    assert analysis.vendor == "Ridgeline Distribution"
    assert analysis.dollar_impact == pytest.approx(2420.33)
    assert analysis.severity == "High"
    assert analysis.confidence > 0.75


def test_analyze_document_low_confidence_for_vague_text():
    analysis = analyze_document("Not sure what happened.", HINTS)
    assert analysis.category is None
    assert analysis.confidence < 0.75


def test_keyword_matching_respects_word_boundaries():
    # "truck" must not fire inside "struck"
    analysis = analyze_document("Someone was struck by a cart.", HINTS)
    assert analysis.matched_keyword is None


def test_short_date_and_word_currency_are_v2_only():
    doc = "Store: ST-009\nDate: 9/5/26\nEstimated impact: about 1,200.00 dollars\n"
    v1 = analyze_document(doc, load_prompt("extract", "v1").stub_hints)
    v2 = analyze_document(doc, load_prompt("extract", "v2").stub_hints)
    assert v1.incident_date is None and v1.dollar_impact is None
    assert v2.incident_date == "2026-09-05"
    assert v2.dollar_impact == pytest.approx(1200.00)


# -- structured completion --------------------------------------------------


def test_stub_completion_is_schema_valid():
    completion = structured_complete(StubLLM(), EXTRACT_P, _inputs(FULL_DOC), HINTS)
    assert completion.ok
    assert completion.attempts == 1
    assert completion.payload["store_id"] == "ST-011"


def test_stub_classify_and_summarize_are_schema_valid():
    stub = StubLLM()
    summarize = load_prompt("summarize", "v1")
    classify = load_prompt("classify", "v1")
    s = structured_complete(stub, summarize, _inputs(FULL_DOC), HINTS)
    c = structured_complete(stub, classify, _inputs(FULL_DOC), HINTS)
    assert s.ok and s.payload["key_points"]
    assert c.ok and c.payload["category"] == "Vendor Delivery"


def test_repair_retry_recovers_from_invalid_first_attempt():
    completion = structured_complete(
        StubLLM(), EXTRACT_P, _inputs(FULL_DOC + FORCE_INVALID_ONCE), HINTS
    )
    assert completion.ok
    assert completion.attempts == 2
    assert completion.repaired is True


def test_hard_failure_after_repair_is_reported():
    completion = structured_complete(
        StubLLM(), EXTRACT_P, _inputs(FULL_DOC + FORCE_INVALID_ALWAYS), HINTS
    )
    assert not completion.ok
    assert completion.attempts == 2
    assert completion.errors


def test_validate_payload_reports_missing_required_field():
    errors = validate_payload(EXTRACT_P.output_schema, {"category": "Food Safety"})
    assert any("required" in error or "is a required property" in error for error in errors)


def test_validate_payload_accepts_good_payload():
    payload = {
        "category": "Food Safety",
        "subcategory": None,
        "store_id": None,
        "incident_date": None,
        "vendor": None,
        "dollar_impact": None,
        "severity": None,
        "summary": "s",
        "confidence": 0.5,
    }
    assert validate_payload(EXTRACT_P.output_schema, payload) == []


def test_validate_payload_rejects_bad_store_id_pattern():
    payload = {
        "category": "Food Safety",
        "subcategory": None,
        "store_id": "store-4",
        "incident_date": None,
        "vendor": None,
        "dollar_impact": None,
        "severity": None,
        "summary": "s",
        "confidence": 0.5,
    }
    errors = validate_payload(EXTRACT_P.output_schema, payload)
    assert any("does not match" in error for error in errors)


def test_extract_json_handles_markdown_fences():
    raw = '```json\n{"a": 1}\n```'
    assert extract_json(raw) == {"a": 1}


def test_extract_json_handles_surrounding_prose():
    raw = 'Here you go: {"a": 2} thanks!'
    assert extract_json(raw) == {"a": 2}


def test_get_llm_returns_stub_for_stub_provider():
    llm = get_llm(Settings(llm_provider="stub"))
    assert isinstance(llm, StubLLM)
    assert llm._llm_type == "stub"


def test_get_llm_requires_api_key_for_openai():
    with pytest.raises(ValueError):
        get_llm(Settings(llm_provider="openai", openai_api_key=""))


def test_stub_chat_model_generate_returns_json_message():
    from langchain_core.messages import HumanMessage

    result = StubLLM().invoke([HumanMessage(content=FULL_DOC)])
    payload = json.loads(result.content)
    assert payload["store_id"] == "ST-011"

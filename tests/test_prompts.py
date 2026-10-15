"""Prompt library: loading, versioning, hashing, rendering."""

from __future__ import annotations

import pytest

from opsreportagent.prompt_library import (
    PromptError,
    clear_cache,
    list_prompts,
    load_prompt,
    prompt_path,
)


def test_load_prompt_v1_has_documented_fields():
    prompt = load_prompt("extract", "v1")
    assert prompt.name == "extract"
    assert prompt.version == "v1"
    assert prompt.inputs == ["document", "taxonomy", "today"]
    assert prompt.output_schema["type"] == "object"
    assert prompt.known_failure_modes
    assert prompt.examples
    assert prompt.ref == "extract.v1"


def test_prompt_hash_is_stable_across_loads():
    clear_cache()
    first = load_prompt("classify", "v1").sha256
    clear_cache()
    second = load_prompt("classify", "v1").sha256
    assert first == second
    assert len(first) == 64


def test_prompt_versions_have_different_hashes():
    v1 = load_prompt("extract", "v1")
    v2 = load_prompt("extract", "v2")
    assert v1.sha256 != v2.sha256
    assert v2.stub_hints["date_patterns"] == ["long", "iso", "short"]


def test_list_prompts_covers_the_library():
    prompts = list_prompts()
    refs = {p["ref"] for p in prompts}
    assert {"summarize.v1", "classify.v1", "extract.v1", "extract.v2", "exec_summary.v1"} <= refs
    assert all(len(p["sha256"]) == 64 for p in prompts)


def test_missing_prompt_raises_with_available_list():
    with pytest.raises(PromptError) as excinfo:
        load_prompt("does-not-exist", "v1")
    assert "available" in str(excinfo.value)


def test_missing_version_raises():
    with pytest.raises(PromptError):
        load_prompt("extract", "v99")


def test_render_substitutes_inputs():
    prompt = load_prompt("summarize", "v1")
    rendered = prompt.render(document="DOC-BODY", taxonomy="- X")
    assert "DOC-BODY" in rendered
    assert "{document}" not in rendered


def test_render_rejects_missing_inputs():
    prompt = load_prompt("summarize", "v1")
    with pytest.raises(PromptError):
        prompt.render(document="only-one")


def test_prompt_path_is_inside_the_package():
    assert prompt_path("extract", "v1").name == "extract.v1.yaml"
    assert prompt_path("extract", "v1").is_file()


def test_v2_known_failure_modes_document_v1_misses():
    prompt = load_prompt("extract", "v2")
    blob = " ".join(prompt.known_failure_modes)
    assert "use-by" in blob
    assert "corrections.csv" in blob

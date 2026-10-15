"""Versioned prompt library.

Prompts live as YAML files in ``opsreportagent/prompts`` named
``<name>.<version>.yaml``. Each file documents its own inputs, system message,
template, JSON output schema, known failure modes and few-shot examples.

Every prompt is content-hashed so the exact prompt revision used for an
extraction can be recorded and audited later.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

REQUIRED_FIELDS = (
    "name",
    "version",
    "description",
    "inputs",
    "system",
    "template",
    "output_schema",
    "known_failure_modes",
    "examples",
)


class PromptError(RuntimeError):
    """Raised when a prompt cannot be found or is malformed."""


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    description: str
    inputs: list[str]
    system: str
    template: str
    output_schema: dict
    known_failure_modes: list[str]
    examples: list[dict]
    stub_hints: dict = field(default_factory=dict)
    path: str = ""
    sha256: str = ""
    body: str = ""

    @property
    def ref(self) -> str:
        """Canonical ``name.version`` reference."""
        return f"{self.name}.{self.version}"

    def render(self, **kwargs) -> str:
        """Render the template, leaving unknown placeholders untouched."""
        missing = [key for key in self.inputs if key not in kwargs]
        if missing:
            raise PromptError(f"{self.ref} missing inputs: {missing}")
        text = self.template
        for key, value in kwargs.items():
            text = text.replace("{" + key + "}", str(value))
        return text

    def schema_json(self, indent: int = 2) -> str:
        return json.dumps(self.output_schema, indent=indent, sort_keys=True)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "ref": self.ref,
            "description": self.description,
            "inputs": self.inputs,
            "known_failure_modes": self.known_failure_modes,
            "sha256": self.sha256,
            "path": self.path,
        }


def _hash_body(payload: dict) -> str:
    """Stable content hash of the prompt body (schema-driven, whitespace-proof)."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_prompt(path: Path) -> Prompt:
    raw = path.read_text(encoding="utf-8")
    payload = yaml.safe_load(raw)
    if not isinstance(payload, dict):
        raise PromptError(f"{path} must contain a YAML mapping")
    missing = [key for key in REQUIRED_FIELDS if key not in payload]
    if missing:
        raise PromptError(f"{path} is missing required fields: {missing}")
    return Prompt(
        name=payload["name"],
        version=payload["version"],
        description=payload["description"],
        inputs=list(payload["inputs"]),
        system=payload["system"],
        template=payload["template"],
        output_schema=payload["output_schema"],
        known_failure_modes=list(payload["known_failure_modes"]),
        examples=list(payload["examples"]),
        stub_hints=payload.get("stub_hints", {}) or {},
        path=str(path),
        sha256=_hash_body(payload),
        body=raw,
    )


def prompt_path(name: str, version: str) -> Path:
    return PROMPTS_DIR / f"{name}.{version}.yaml"


@lru_cache(maxsize=128)
def load_prompt(name: str, version: str = "v1") -> Prompt:
    """Load and validate a prompt by name and version."""
    path = prompt_path(name, version)
    if not path.is_file():
        available = sorted(p.stem for p in PROMPTS_DIR.glob("*.yaml"))
        raise PromptError(f"No such prompt {name}.{version} (available: {available})")
    return _parse_prompt(path)


def cached_prompt_versions() -> dict[str, list[str]]:
    versions: dict[str, list[str]] = {}
    for path in sorted(PROMPTS_DIR.glob("*.yaml")):
        prompt = _parse_prompt(path)
        versions.setdefault(prompt.name, []).append(prompt.version)
    return {name: sorted(v) for name, v in versions.items()}


def list_prompts() -> list[dict]:
    """Describe every prompt in the library (name, version, hash, purpose)."""
    prompts = [_parse_prompt(path) for path in sorted(PROMPTS_DIR.glob("*.yaml"))]
    return [p.to_dict() for p in prompts]


def clear_cache() -> None:
    load_prompt.cache_clear()

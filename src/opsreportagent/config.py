"""Configuration for OpsReportAgent.

Settings are loaded from the process environment (optionally seeded by a ``.env``
file) with safe offline defaults so that the whole pipeline runs without any API
keys using the deterministic ``stub`` provider.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

# Project-relative defaults --------------------------------------------------
PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent.parent
DEFAULT_DATABASE_URL = "sqlite:///./opsreport.db"

VALID_PROVIDERS = ("stub", "openai", "anthropic")


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (no external dependency).

    Values already present in the environment win, mirroring common tooling.
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(slots=True)
class Settings:
    """Runtime settings for the pipeline."""

    llm_provider: str = "stub"
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    model_name: str = ""
    database_url: str = DEFAULT_DATABASE_URL
    confidence_threshold: float = 0.75
    prompt_version: str = "v1"
    corpus_dir: str = "data/corpus"
    gold_dir: str = "data/gold"
    reports_dir: str = "reports"
    templates_dir: str = str(PROJECT_ROOT / "templates")

    # ----- constructors -----------------------------------------------------
    @classmethod
    def from_env(cls, dotenv: bool = True) -> Settings:
        if dotenv:
            _load_dotenv(PROJECT_ROOT / ".env")
        provider = (os.environ.get("LLM_PROVIDER") or "stub").strip().lower()
        if provider not in VALID_PROVIDERS:
            raise ValueError(
                f"Unsupported LLM_PROVIDER={provider!r}; expected one of {VALID_PROVIDERS}"
            )
        threshold_raw = os.environ.get("CONFIDENCE_THRESHOLD") or "0.75"
        return cls(
            llm_provider=provider,
            openai_api_key=os.environ.get("OPENAI_API_KEY", "") or "",
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", "") or "",
            model_name=os.environ.get("MODEL_NAME", "") or "",
            database_url=os.environ.get("DATABASE_URL", "") or DEFAULT_DATABASE_URL,
            confidence_threshold=float(threshold_raw),
            prompt_version=os.environ.get("PROMPT_VERSION", "v1") or "v1",
        )

    # ----- helpers ----------------------------------------------------------
    @property
    def model(self) -> str:
        """Effective model label recorded with each extraction/run."""
        if self.model_name:
            return self.model_name
        return {
            "stub": "stub-deterministic-v1",
            "openai": "gpt-4o-mini",
            "anthropic": "claude-3-5-sonnet-latest",
        }.get(self.llm_provider, self.llm_provider)

    def as_dict(self) -> dict:
        return asdict(self)

    def redacted(self) -> dict:
        """Settings dump with secrets masked (safe for logs/reports)."""
        data = self.as_dict()
        for key in ("openai_api_key", "anthropic_api_key"):
            if data.get(key):
                data[key] = "***redacted***"
        return data

    def ensure_dirs(self) -> None:
        Path(self.reports_dir).mkdir(parents=True, exist_ok=True)
        Path(self.reports_dir, "figs").mkdir(parents=True, exist_ok=True)

    def validate_for_provider(self) -> None:
        if self.llm_provider == "openai" and not self.openai_api_key:
            raise ValueError("LLM_PROVIDER=openai requires OPENAI_API_KEY")
        if self.llm_provider == "anthropic" and not self.anthropic_api_key:
            raise ValueError("LLM_PROVIDER=anthropic requires ANTHROPIC_API_KEY")


def load_settings(**overrides) -> Settings:
    """Build settings from the environment and apply explicit overrides."""
    settings = Settings.from_env()
    valid = {f.name for f in fields(Settings)}
    for key, value in overrides.items():
        if value is None:
            continue
        if key not in valid:
            raise TypeError(f"Unknown setting {key!r}")
        setattr(settings, key, value)
    return settings

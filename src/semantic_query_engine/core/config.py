"""Central configuration: filesystem paths, LLM provider settings, runtime limits.

Every path and tunable in the application resolves through this module. Nothing
else in the codebase should read ``os.environ`` directly or build a path from
``__file__``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

# ---------------------------------------------------------------------------
# Filesystem layout
# ---------------------------------------------------------------------------

# config.py -> core/ -> semantic_query_engine/ -> src/ -> <root>, hence parents[3]
PROJECT_ROOT = Path(
    os.getenv("SQE_PROJECT_ROOT") or Path(__file__).resolve().parents[3]
)

DATA_DIR = PROJECT_ROOT / "data"
RAW_DATA_DIR = DATA_DIR / "raw"
SEMANTIC_DIR = DATA_DIR / "semantic"
WAREHOUSE_DIR = DATA_DIR / "warehouse"
EVALS_DIR = PROJECT_ROOT / "evals"

SEMANTIC_LAYER_PATH = SEMANTIC_DIR / "semantic_layer.json"
VECTOR_CACHE_PATH = SEMANTIC_DIR / "embedding_cache.json"
DUCKDB_PATH = Path(os.getenv("SQE_DUCKDB_PATH") or WAREHOUSE_DIR / "semantic_query_engine.duckdb")

DAILY_SALES_CSV = RAW_DATA_DIR / "fmcg_daily_sales_2022_2024.csv"
WEEKLY_MODELING_CSV = RAW_DATA_DIR / "fmcg_weekly_modeling.csv"

GOLD_QUERIES_PATH = EVALS_DIR / "datasets" / "gold_queries.json"


# ---------------------------------------------------------------------------
# Environment loading
# ---------------------------------------------------------------------------

def _load_env_file_manually(env_path: Path) -> None:
    """Minimal ``.env`` parser used only when ``python-dotenv`` is unavailable.

    Split out from :func:`load_environment` so it can be exercised directly in
    tests without depending on whether ``python-dotenv`` happens to be installed.
    Malformed or unreadable files are a silent no-op rather than a crash --
    logging isn't configured yet this early in import, and a broken ``.env``
    shouldn't prevent the application from starting.
    """
    try:
        text = env_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_environment() -> None:
    """Load ``.env`` into the process environment without overriding real vars.

    Falls back to a minimal parser when ``python-dotenv`` is unavailable, so the
    application still starts in a bare interpreter.
    """
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return

    try:
        from dotenv import load_dotenv
    except ImportError:
        load_dotenv = None  # type: ignore[assignment]

    if load_dotenv is not None:
        load_dotenv(env_path, override=False)
        return

    _load_env_file_manually(env_path)


load_environment()


# ---------------------------------------------------------------------------
# LLM provider
# ---------------------------------------------------------------------------

Provider = Literal["groq", "openai", "none"]

# Groq is OpenAI-API-compatible but serves Llama models and has no embeddings
# endpoint. The key prefix is what distinguishes the two at runtime.
_GROQ_KEY_PREFIX = "gsk_"
_GROQ_BASE_URL = "https://api.groq.com/openai/v1"

_MODELS_BY_PROVIDER: dict[str, dict[str, str]] = {
    "groq": {
        "generator": "openai/gpt-oss-120b",
        "synthesizer": "openai/gpt-oss-120b",
        "embedding": "",  # Groq exposes no embeddings endpoint
    },
    "openai": {
        "generator": "gpt-4o-mini",
        "synthesizer": "gpt-4o-mini",
        "embedding": "text-embedding-3-small",
    },
}


@dataclass(frozen=True)
class LLMSettings:
    """Resolved LLM configuration for the current process."""

    provider: Provider
    api_key: str | None
    base_url: str | None
    generator_model: str
    synthesizer_model: str
    embedding_model: str
    temperature_generation: float = 0.0
    temperature_synthesis: float = 0.2
    request_timeout_seconds: float = 60.0

    @property
    def is_enabled(self) -> bool:
        """True when an API key is present and LLM calls can be attempted."""
        return self.provider != "none" and bool(self.api_key)

    @property
    def supports_embeddings(self) -> bool:
        """True when the provider can serve the vector-retrieval path."""
        return bool(self.embedding_model)


def load_llm_settings() -> LLMSettings:
    """Detect the provider from the configured API key and resolve its models."""
    api_key = os.getenv("SQE_LLM_API_KEY") or os.getenv("OPENAI_API_KEY")

    if not api_key:
        return LLMSettings(
            provider="none",
            api_key=None,
            base_url=None,
            generator_model="",
            synthesizer_model="",
            embedding_model="",
        )

    provider: Provider = "groq" if api_key.startswith(_GROQ_KEY_PREFIX) else "openai"
    models = _MODELS_BY_PROVIDER[provider]

    return LLMSettings(
        provider=provider,
        api_key=api_key,
        base_url=_GROQ_BASE_URL if provider == "groq" else None,
        generator_model=os.getenv("SQE_GENERATOR_MODEL") or models["generator"],
        synthesizer_model=os.getenv("SQE_SYNTHESIZER_MODEL") or models["synthesizer"],
        embedding_model=os.getenv("SQE_EMBEDDING_MODEL") or models["embedding"],
    )


# ---------------------------------------------------------------------------
# Pipeline limits
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PipelineSettings:
    """Guardrails and tuning for the analytics pipeline."""

    max_repair_attempts: int = 2
    max_result_rows: int = 1_000
    min_analytical_rows: int = 5
    retrieval_top_k_tables: int = 2
    retrieval_top_k_metrics: int = 3
    synthesis_sample_rows: int = 20
    allowed_tables: frozenset[str] = field(
        default_factory=lambda: frozenset({"fmcg_sales", "weekly_modeling_data"})
    )


PIPELINE = PipelineSettings()

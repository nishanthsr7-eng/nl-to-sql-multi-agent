"""Central configuration: filesystem paths, LLM provider settings, runtime limits.

Every path and tunable in the application resolves through this module. Nothing
else in the codebase should read ``os.environ`` directly or build a path from
``__file__``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
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

# Per-domain paths -- the semantic layer, the warehouse file, the embedding
# cache and the gold set -- are no longer constants here. They are properties of
# a Domain, declared under data/domains/ and resolved at runtime; see
# :mod:`semantic_query_engine.core.domains`. Only paths that are the same for
# every domain remain in this module.
#
# SQE_DUCKDB_PATH predates domains and still works: ``domains`` applies it to the
# default domain. It is read here so this module stays the only place that
# touches os.environ for a path.
DUCKDB_PATH_OVERRIDE = os.getenv("SQE_DUCKDB_PATH") or ""

DAILY_SALES_CSV = RAW_DATA_DIR / "fmcg_daily_sales_2022_2024.csv"
WEEKLY_MODELING_CSV = RAW_DATA_DIR / "fmcg_weekly_modeling.csv"

# The Phase 4 star schema, generated from DAILY_SALES_CSV by
# scripts/build_star_schema.py. The original denormalized CSV is kept as the
# generator's input and is no longer loaded into the warehouse directly: the
# narrow fact in STAR_DIR supersedes it. Referenced by the generator and by the
# retail domain manifest; nothing in the pipeline reads it.
STAR_DIR = RAW_DATA_DIR / "star"


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
    # The model that grades narratives (`sqe judge`). Separate from the
    # synthesiser because a model grading its own output is the oldest failure
    # in this corner of evaluation -- see evals/judge.py. It falls back to the
    # synthesiser when unset, which is honest but self-graded, and the judge
    # report says so rather than quietly reporting the agreement as clean.
    judge_model: str = ""
    # Embeddings may be served from a different endpoint than generation. Groq
    # serves no embeddings model at all, so a run against it silently drops to
    # keyword retrieval -- which is fine for one run and fatal for a comparison,
    # because `sqe matrix` would then vary *two* things and attribute the whole
    # difference to the generator. Pointing these at a local embedder keeps
    # retrieval identical across rows. Both default to the generation endpoint,
    # so the single-provider case is unchanged.
    embedding_base_url: str | None = None
    embedding_api_key: str | None = None
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

    @property
    def embedding_endpoint(self) -> tuple[str | None, str | None]:
        """The (api_key, base_url) embeddings are served from.

        Falls back to the generation endpoint field by field, so overriding only
        the URL -- the usual case, a local embedder that ignores the key -- does
        not also require restating the key.
        """
        return (
            self.embedding_api_key or self.api_key,
            self.embedding_base_url or self.base_url,
        )


def _model_override(env_var: str, default: str) -> str:
    """Resolve a model name from the environment, honouring an explicit empty value.

    Setting the variable to "" must mean "this provider serves no such model",
    not "fall back to the default". A local or third-party endpoint that has no
    embeddings model needs SQE_EMBEDDING_MODEL="" to switch the vector-retrieval
    path off; a plain ``or`` would silently ask it for an OpenAI model instead.
    """
    value = os.getenv(env_var)
    return default if value is None else value


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

    # Any OpenAI-compatible endpoint is reachable by overriding the base URL:
    # a local server (llama.cpp, Ollama, vLLM, LM Studio) or another hosted
    # provider that speaks the same wire format. "openai" is therefore the
    # provider label for "OpenAI-compatible", not a claim about who is serving.
    base_url = os.getenv("SQE_LLM_BASE_URL") or (
        _GROQ_BASE_URL if provider == "groq" else None
    )

    return LLMSettings(
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        generator_model=_model_override("SQE_GENERATOR_MODEL", models["generator"]),
        synthesizer_model=_model_override("SQE_SYNTHESIZER_MODEL", models["synthesizer"]),
        embedding_model=_model_override("SQE_EMBEDDING_MODEL", models["embedding"]),
        judge_model=os.getenv("SQE_JUDGE_MODEL", ""),
        # `or None` rather than `_model_override`: an empty embedding base URL
        # means "no separate endpoint, use the generation one", which is the
        # default -- unlike the model name, where "" is a meaningful "this
        # provider serves none".
        embedding_base_url=os.getenv("SQE_EMBEDDING_BASE_URL") or None,
        embedding_api_key=os.getenv("SQE_EMBEDDING_API_KEY") or None,
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

    # --- Warehouse resource ceilings ---------------------------------------
    # A model-authored query is untrusted input: an accidental cross join over
    # 190k x 31k rows is a plausible generation, not a hypothetical. These bound
    # what any single query can consume, so a bad generation costs one slow
    # request instead of the whole process.
    # --- Semantic cache ----------------------------------------------------
    # Off unless asked for. A cache changes what a run measures -- a hit reports
    # a previous question's SQL and a cost of zero -- so it must never be
    # something an evaluation picked up by accident. Interactive surfaces opt in
    # (``sqe ask --cache``), and ``sqe eval --cache`` exists precisely to
    # measure the hit rate and the saving, with the report saying so.
    semantic_cache_enabled: bool = False
    # None means "let the cache pick per backend": an embedding hit and a
    # lexical hit are not equally trustworthy and are not held to one bar.
    semantic_cache_threshold: float | None = None

    query_timeout_seconds: float = 30.0
    warehouse_memory_limit: str = "2GB"
    warehouse_threads: int = 4


PIPELINE = PipelineSettings()


# ---------------------------------------------------------------------------
# Ablation configuration (the evaluation baseline ladder)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AblationConfig:
    """Which guardrails are active for one pipeline run.

    The baseline ladder in the evaluation report ("naive prompt" -> "+ semantic
    layer" -> "+ validator" -> "full pipeline") is the same engine with stages
    switched off, not four separate implementations. Making that a config object
    rather than four code paths is what makes the comparison honest: every rung
    shares the planner, the generator, the executor and the synthesiser, so any
    accuracy difference between two rows is attributable to the one toggle that
    changed between them.

    ``FULL`` is the default and is what every non-evaluation caller gets, so the
    product behaviour is the top rung of its own ladder rather than a fifth
    configuration that is never measured.
    """

    # False replaces semantic-layer retrieval with a raw ``information_schema``
    # dump of the allowed tables -- the "schema dump in the prompt" baseline that
    # most text-to-SQL demos actually are.
    use_semantic_layer: bool = True
    # False downgrades the validator to its mutation check only: no schema
    # resolution, no metric contracts, no LIMIT injection, no EXPLAIN. It is not
    # switched off entirely because executing arbitrary model-authored DDL
    # against the warehouse to score a baseline would be a genuinely unsafe
    # experiment, and blocking mutation is not the guardrail under measurement.
    use_validator: bool = True
    # 0 measures the validator as a pure gate: rejections become failures instead
    # of being handed back to the model to fix.
    max_repair_attempts: int = PipelineSettings.max_repair_attempts

    @property
    def label(self) -> str:
        if not self.use_semantic_layer:
            return "naive"
        if not self.use_validator:
            return "semantic"
        return "validator" if self.max_repair_attempts == 0 else "full"


# The four rungs, in the order they are reported. Each differs from the one above
# it by exactly one toggle, which is what lets a reader attribute the delta.
ABLATION_LADDER: dict[str, AblationConfig] = {
    "naive":     AblationConfig(use_semantic_layer=False, use_validator=False, max_repair_attempts=0),
    "semantic":  AblationConfig(use_semantic_layer=True,  use_validator=False, max_repair_attempts=0),
    "validator": AblationConfig(use_semantic_layer=True,  use_validator=True,  max_repair_attempts=0),
    "full":      AblationConfig(use_semantic_layer=True,  use_validator=True,
                                max_repair_attempts=PipelineSettings.max_repair_attempts),
}

FULL_PIPELINE = ABLATION_LADDER["full"]

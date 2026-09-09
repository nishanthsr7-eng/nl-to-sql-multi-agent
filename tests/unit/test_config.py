"""Environment and configuration loading -- no real API key required.

The old version of this test asserted ``OPENAI_API_KEY`` was truthy, which
meant a clean checkout (or CI with no secret configured) failed before
anything interesting ran. Structural checks here don't need a real key; the
provider-detection tests already used monkeypatch and are unaffected.
"""

from __future__ import annotations

from semantic_query_engine.core import config as app_config
from semantic_query_engine.core.domains import active_domain


def test_project_root_and_semantic_layer_paths_exist():
    assert app_config.PROJECT_ROOT.exists()
    assert active_domain().semantic_layer_path.exists()


def test_load_environment_is_idempotent(monkeypatch):
    monkeypatch.setenv("SOME_UNRELATED_VAR", "keep-me")
    app_config.load_environment()
    app_config.load_environment()
    assert __import__("os").getenv("SOME_UNRELATED_VAR") == "keep-me"


def test_llm_settings_detect_groq_from_key_prefix(monkeypatch):
    monkeypatch.setenv("SQE_LLM_API_KEY", "gsk_fake_key_for_test")
    settings = app_config.load_llm_settings()
    assert settings.provider == "groq"
    assert settings.base_url == "https://api.groq.com/openai/v1"
    assert not settings.supports_embeddings


def test_llm_settings_detect_openai_from_non_groq_key(monkeypatch):
    # conftest blanks SQE_EMBEDDING_MODEL so a developer's local endpoint cannot
    # leak in; removing it here is what makes this assert the provider default
    # rather than whatever the environment happened to carry.
    monkeypatch.delenv("SQE_EMBEDDING_MODEL", raising=False)
    monkeypatch.setenv("SQE_LLM_API_KEY", "sk-fake-openai-key")
    settings = app_config.load_llm_settings()
    assert settings.provider == "openai"
    assert settings.supports_embeddings


def test_llm_settings_report_disabled_without_a_key(monkeypatch):
    monkeypatch.delenv("SQE_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    settings = app_config.load_llm_settings()
    assert settings.provider == "none"
    assert not settings.is_enabled


def test_manual_env_parser_is_a_no_op_on_non_utf8_bytes(tmp_path):
    """A malformed/non-UTF-8 .env must not crash the app at import time -- it
    should just mean nothing gets loaded from it (config.py:_load_env_file_manually)."""
    env_path = tmp_path / ".env"
    env_path.write_bytes(b"\xff\xfe\x00SOME_VAR=broken\x00")

    app_config._load_env_file_manually(env_path)  # must not raise


def test_manual_env_parser_loads_simple_key_value_pairs(tmp_path, monkeypatch):
    monkeypatch.delenv("SQE_TEST_MANUAL_ENV_VAR", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text('SQE_TEST_MANUAL_ENV_VAR="hello"\n# a comment\n', encoding="utf-8")

    app_config._load_env_file_manually(env_path)

    assert __import__("os").getenv("SQE_TEST_MANUAL_ENV_VAR") == "hello"


def test_llm_settings_honour_an_explicit_base_url_override(monkeypatch):
    """An OpenAI-compatible endpoint (local server, other hosted provider) is
    reachable without a code change. Before this, base_url was derived solely
    from the key prefix, so only Groq and OpenAI itself could ever be called."""
    monkeypatch.setenv("SQE_LLM_API_KEY", "local-no-auth")
    monkeypatch.setenv("SQE_LLM_BASE_URL", "http://localhost:8000/v1")
    settings = app_config.load_llm_settings()
    assert settings.base_url == "http://localhost:8000/v1"
    assert settings.is_enabled


def test_llm_settings_treat_an_empty_embedding_model_as_unsupported(monkeypatch):
    """SQE_EMBEDDING_MODEL="" must disable the vector path, not fall through to
    the provider default -- an endpoint with no embeddings would otherwise be
    asked for text-embedding-3-small and fail at request time."""
    monkeypatch.setenv("SQE_LLM_API_KEY", "sk-not-a-groq-key")
    monkeypatch.setenv("SQE_EMBEDDING_MODEL", "")
    settings = app_config.load_llm_settings()
    assert settings.embedding_model == ""
    assert not settings.supports_embeddings


def test_embeddings_default_to_the_generation_endpoint(monkeypatch):
    """The single-provider case must be untouched by the split: overriding
    nothing has to resolve exactly as it did before embeddings got their own
    client."""
    monkeypatch.setenv("SQE_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("SQE_LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.delenv("SQE_EMBEDDING_BASE_URL", raising=False)
    monkeypatch.delenv("SQE_EMBEDDING_API_KEY", raising=False)

    settings = app_config.load_llm_settings()
    assert settings.embedding_endpoint == ("sk-test", "https://example.invalid/v1")


def test_embeddings_can_be_served_from_a_separate_endpoint(monkeypatch):
    """A hosted generator with no embeddings model plus a local embedder is a
    working combination, and it is what keeps `sqe matrix` rows comparable."""
    monkeypatch.setenv("SQE_LLM_API_KEY", "gsk_test")
    monkeypatch.setenv("SQE_EMBEDDING_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.delenv("SQE_EMBEDDING_API_KEY", raising=False)

    settings = app_config.load_llm_settings()
    key, url = settings.embedding_endpoint
    assert url == "http://localhost:11434/v1"
    # The key falls back field by field: a local embedder ignores it, and
    # restating it would be a second thing to keep in sync.
    assert key == "gsk_test"

def test_the_judge_model_can_be_set_independently_of_the_synthesiser(monkeypatch):
    """A model grading its own output is the oldest failure in this corner of
    evaluation, and until this existed there was no way to avoid it: the judge
    always fell back to the synthesiser. The 2026-09-22 calibration was run
    self-graded for exactly that reason."""
    monkeypatch.setenv("SQE_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("SQE_SYNTHESIZER_MODEL", "writer")
    monkeypatch.setenv("SQE_JUDGE_MODEL", "grader")

    from evals.judge import judge_model_for

    settings = app_config.load_llm_settings()
    assert judge_model_for(settings) == "grader"


def test_the_judge_falls_back_to_the_synthesiser_when_unset(monkeypatch):
    """The fallback is kept rather than made an error: a self-graded run is
    still worth having, as long as the report says that is what it is."""
    monkeypatch.setenv("SQE_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("SQE_SYNTHESIZER_MODEL", "writer")
    monkeypatch.delenv("SQE_JUDGE_MODEL", raising=False)

    from evals.judge import judge_model_for

    assert judge_model_for(app_config.load_llm_settings()) == "writer"

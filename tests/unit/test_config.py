"""Environment and configuration loading -- no real API key required.

The old version of this test asserted ``OPENAI_API_KEY`` was truthy, which
meant a clean checkout (or CI with no secret configured) failed before
anything interesting ran. Structural checks here don't need a real key; the
provider-detection tests already used monkeypatch and are unaffected.
"""

from __future__ import annotations

from semantic_query_engine.core import config as app_config


def test_project_root_and_semantic_layer_paths_exist():
    assert app_config.PROJECT_ROOT.exists()
    assert app_config.SEMANTIC_LAYER_PATH.exists()


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

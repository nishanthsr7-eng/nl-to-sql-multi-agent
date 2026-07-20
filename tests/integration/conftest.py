"""Integration-tier fixtures: these tests hit a real LLM provider over the network.

Deselected by default via ``addopts = "-m 'not integration'"`` in pyproject.toml.
Run explicitly with ``pytest -m integration``. If no API key is configured even
then, tests skip cleanly instead of erroring.
"""

from __future__ import annotations

import pytest

from semantic_query_engine.core.config import load_llm_settings


@pytest.fixture(autouse=True)
def _require_llm_key():
    if not load_llm_settings().is_enabled:
        pytest.skip("No LLM API key configured (OPENAI_API_KEY / SQE_LLM_API_KEY) -- skipping integration test.")

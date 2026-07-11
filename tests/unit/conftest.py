"""Unit-tier fixtures: no network, no real API key required.

Every unit test runs offline by default -- the deterministic fallback / keyword
retrieval paths -- regardless of whether the developer's real `.env` has an API
key configured. A test that specifically wants to exercise the LLM-primary code
path should inject a fake client (see tests/unit/fakes.py) and monkeypatch a
placeholder key back in for that one test.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_llm_by_default(monkeypatch):
    monkeypatch.delenv("SQE_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

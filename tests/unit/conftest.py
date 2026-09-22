"""Unit-tier fixtures: no network, no real API key required.

Every unit test runs offline -- the deterministic fallback and keyword-retrieval
paths -- regardless of whether the developer's real ``.env`` has a key configured.

The keys are cleared at *import* time rather than in an autouse fixture, and that
is load-bearing. Agents resolve their LLM settings once, when they are
constructed, so a fixture that clears the environment per test is too late for
anything built by a wider-scoped fixture: pytest sets a module-scoped
``pipeline`` up before the function-scoped fixtures of the first test that uses
it, and that pipeline would capture the developer's real key and start making
network calls from the unit tier. Clearing here happens during collection, ahead
of every fixture, so the guarantee does not depend on fixture ordering.

Empty strings rather than ``del``: ``core.config.load_environment()`` runs at
import and repopulates from ``.env``, and ``python-dotenv`` is called with
``override=False``, so a key that is already present -- even as "" -- wins. An
empty key resolves the provider to "none".

A test that specifically wants to exercise the LLM-primary path sets a
placeholder key with ``monkeypatch`` and constructs the agent inside the test,
injecting a fake client (see tests/unit/fakes.py).
"""

from __future__ import annotations

import os

_LLM_KEY_VARS = ("SQE_LLM_API_KEY", "OPENAI_API_KEY")

# Endpoint and model overrides are cleared for the same reason as the keys, and
# for one more: they change what a *resolved* setting looks like even when the
# provider is "none". A developer pointing .env at a local OpenAI-compatible
# server leaves SQE_LLM_BASE_URL set, which then overrides the provider's own
# base URL and fails the provider-detection tests on their machine but not in
# CI -- the worst kind of test failure. The unit tier asserts against the
# defaults, so it must not see a developer's endpoint.
#
# Empty string rather than ``del`` for the same reason the keys use it, with one
# extra wrinkle: test_config calls ``load_environment()`` directly, so a deleted
# variable comes back from .env part-way through the suite and the failure lands
# on whichever test happens to run next.
_LLM_ENDPOINT_VARS = (
    "SQE_LLM_BASE_URL",
    "SQE_GENERATOR_MODEL",
    "SQE_SYNTHESIZER_MODEL",
    "SQE_EMBEDDING_MODEL",
)

for _var in _LLM_KEY_VARS + _LLM_ENDPOINT_VARS:
    os.environ[_var] = ""

# The audit log is on by default -- deliberately, see governance.audit -- and the
# orchestrator writes one record per run. Left on, the unit tier would append a
# few hundred entries to the developer's real data/audit/retail_audit.jsonl on
# every test run, and the hash chain would interleave test runs with real ones.
# Cleared at import time for the same reason as the keys: a pipeline built by a
# module-scoped fixture is constructed before any function-scoped fixture runs.
# The audit tests set it back with monkeypatch and point at a tmp_path.
os.environ["SQE_AUDIT"] = "0"

import pytest  # noqa: E402

from semantic_query_engine.core.config import load_llm_settings  # noqa: E402


@pytest.fixture(autouse=True)
def _no_llm_by_default(monkeypatch):
    """Re-assert the offline default for each test, after any test that set a key."""
    for var in _LLM_KEY_VARS:
        monkeypatch.setenv(var, "")


@pytest.fixture(scope="session", autouse=True)
def _assert_unit_tier_is_offline():
    """Fail loudly if the unit tier could reach a provider.

    A silent regression here does not break a test -- it makes the offline suite
    quietly start billing and depending on the network, which is far worse than a
    failure.
    """
    assert not load_llm_settings().is_enabled, (
        "Unit tests must run offline, but an LLM API key is configured. "
        "The key-clearing in tests/unit/conftest.py did not take effect."
    )

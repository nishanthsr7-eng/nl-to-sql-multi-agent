"""End-to-end orchestrator behaviour across all four intent archetypes, offline.

Runs entirely through the deterministic fallback path (tests/unit/conftest.py
strips any configured API key) -- zero network, zero API key requirement, and
fast. tests/integration/test_orchestrator_llm.py covers the same shapes against
a real LLM provider.
"""

from __future__ import annotations

from semantic_query_engine.agents.sql_generator import SQLGenerationResult
from semantic_query_engine.agents.validator import ValidationResult
from semantic_query_engine.pipeline.orchestrator import AnalyticsPipeline


def test_pipeline_lookup_question():
    pipeline = AnalyticsPipeline()
    response = pipeline.run("What is total revenue by region?")
    assert hasattr(response, "sql_query")
    assert response.result_table
    assert response.sql_source == "fallback"


def test_pipeline_comparative_question():
    pipeline = AnalyticsPipeline()
    response = pipeline.run("Which region had the highest week-on-week growth in units sold during March 2024?")
    assert hasattr(response, "sql_query")
    assert response.result_table
    assert response.intent == "comparative_analysis"


def test_pipeline_ambiguous_question_needs_clarification():
    pipeline = AnalyticsPipeline()
    response = pipeline.run("How did the promotion do?")
    assert isinstance(response, dict)
    assert response.get("needs_clarification") is True
    assert "clarification_prompt" in response


def test_pipeline_sku_lookup_question():
    pipeline = AnalyticsPipeline()
    response = pipeline.run("What were total units sold for SKU MI-006 in the South region last week of January 2024?")
    assert hasattr(response, "sql_query")
    assert response.result_table
    assert response.intent == "descriptive_lookup"


def test_conversation_context_resolves_a_clarification_follow_up():
    """A vague opener that gets clarified is understood with prior context (§10)."""
    pipeline = AnalyticsPipeline()
    first = pipeline.run("How did the promotion do?")
    assert isinstance(first, dict) and first.get("needs_clarification")

    second = pipeline.run("revenue, last month", context=["How did the promotion do?"])
    assert not (isinstance(second, dict) and second.get("needs_clarification"))


def test_repair_loop_stops_after_one_attempt_when_the_fallback_is_unrepairable():
    """Without an LLM, a repair that reproduces the same SQL can never succeed --
    the orchestrator should stop after the first wasted attempt instead of retrying
    an identical query for the full MAX_REPAIR_ATTEMPTS budget."""
    pipeline = AnalyticsPipeline()

    always_invalid = ValidationResult(is_valid=False, sanitized_sql="SELECT 1", errors=["Unknown column 'x'."])
    pipeline.validator.run = lambda *a, **k: always_invalid
    # Both the initial generation and the repair return identical SQL, simulating
    # the real no-LLM case where the deterministic fallback can't act on feedback.
    pipeline.generator.run = lambda *a, **k: SQLGenerationResult(sql="SELECT 1", source="fallback")

    repair_calls = []

    def fake_repair(**kwargs):
        repair_calls.append(kwargs)
        return SQLGenerationResult(sql="SELECT 1", source="fallback_unrepairable")

    pipeline.generator.repair = fake_repair

    response = pipeline.run("What is total revenue by region?")

    assert isinstance(response, dict)
    assert response.get("error") == "SQL validation failed"
    assert len(repair_calls) == 1
    assert any("no further attempts would help" in step for step in response["agent_trace"])

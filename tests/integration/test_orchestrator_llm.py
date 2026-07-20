"""End-to-end orchestrator behaviour with a real LLM provider.

Requires OPENAI_API_KEY or SQE_LLM_API_KEY; hits the network. See
tests/unit/test_orchestrator_fallback.py for the offline, no-API-key-required
equivalent that runs on every ``pytest`` invocation.
"""

from __future__ import annotations

import pytest

from semantic_query_engine.pipeline.orchestrator import AnalyticsPipeline

pytestmark = pytest.mark.integration


def test_pipeline_lookup_question():
    pipeline = AnalyticsPipeline()
    response = pipeline.run("What is total revenue by region?")
    assert hasattr(response, "sql_query")
    assert response.result_table


def test_gold_standard_comparison_question():
    pipeline = AnalyticsPipeline()
    response = pipeline.run("Which region had the highest week-on-week growth in units sold during March 2024?")
    assert hasattr(response, "sql_query")
    assert response.result_table
    assert response.intent == "comparative_analysis"


def test_gold_standard_ambiguous_question():
    pipeline = AnalyticsPipeline()
    response = pipeline.run("How did the promotion do?")
    assert isinstance(response, dict)
    assert response.get("needs_clarification") is True
    assert "clarification_prompt" in response


def test_gold_standard_sku_question():
    pipeline = AnalyticsPipeline()
    response = pipeline.run("What were total units sold for SKU MI-006 in the South region last week of January 2024?")
    assert hasattr(response, "sql_query")
    assert response.result_table
    assert response.intent == "descriptive_lookup"


@pytest.mark.parametrize(
    ("question", "required_columns"),
    [
        ("Compare total revenue across all brands", {"brand", "total_revenue"}),
        ("Show units sold by category across each channel", {"category", "channel", "total_units"}),
        ("Compare promotion versus non-promotion revenue by category", {"category", "promotion_flag", "total_revenue"}),
        ("What is the stock depletion rate by SKU?", {"sku", "stock_depletion_rate"}),
        ("Show monthly units sold by channel", {"channel", "month_start", "total_units"}),
    ],
)
def test_analysis_catalog_queries(question, required_columns):
    pipeline = AnalyticsPipeline()
    response = pipeline.run(question)
    assert hasattr(response, "result_table"), question
    assert response.result_table, question
    assert required_columns.issubset(response.result_table[0]), question


def test_gold_evaluation_dataset_against_real_llm():
    """The same gold dataset unit tests run against fallback, now against the real LLM."""
    from evals.gold_eval import evaluate_gold_set

    pipeline = AnalyticsPipeline()
    results = evaluate_gold_set(pipeline)
    failures = [r for r in results if not r.passed]
    assert not failures, "\n".join(f"{r.case_id}: {r.message}\nSQL: {r.sql}" for r in failures)


def test_conversation_context_resolves_a_clarification_follow_up():
    """A vague opener that gets clarified should be understood with prior context (§10)."""
    pipeline = AnalyticsPipeline()
    first = pipeline.run("How did the promotion do?")
    assert isinstance(first, dict) and first.get("needs_clarification")

    second = pipeline.run("revenue, last month", context=["How did the promotion do?"])
    assert hasattr(second, "sql_query") or (isinstance(second, dict) and not second.get("needs_clarification"))

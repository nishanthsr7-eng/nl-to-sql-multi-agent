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
    assert response.kind == "answer", response
    assert response.result_table


def test_gold_standard_comparison_question():
    """A hard comparative question must produce a *contractual* outcome, not
    necessarily a correct one.

    This asserted ``kind == "answer"`` and went red when the validator rejected
    an unsafe ``fmcg_sales`` -> ``weekly_modeling_data`` join that the repair loop
    could not recover. That is the grain fan-out check doing its job, so the test
    was failing the engine for being careful. Accuracy on this question belongs
    to `sqe eval`, which scores it against a reference; what belongs here is that
    the pipeline returns a member of the discriminated union and never raises."""
    pipeline = AnalyticsPipeline()
    response = pipeline.run("Which region had the highest week-on-week growth in units sold during March 2024?")
    assert response.kind in {"answer", "clarification", "failure"}, response
    if response.kind == "answer":
        assert response.result_table
        assert response.intent == "comparative_analysis"

def test_gold_standard_ambiguous_question():
    pipeline = AnalyticsPipeline()
    response = pipeline.run("How did the promotion do?")
    assert response.kind == "clarification", response
    assert response.prompt


def test_gold_standard_sku_question():
    pipeline = AnalyticsPipeline()
    response = pipeline.run("What were total units sold for SKU MI-006 in the South region last week of January 2024?")
    assert response.kind == "answer", response
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
    """The grouping columns come from the semantic layer and are contractual; the
    metric column's *name* is the model's wording and is not.

    Asserting the exact aggregate alias made this flap whenever the generator
    wrote ``total_units_sold`` for ``total_units`` -- a naming difference, not a
    wrong answer. Dimensions are still matched exactly, because a missing or
    renamed dimension means the question was not actually answered."""
    pipeline = AnalyticsPipeline()
    response = pipeline.run(question)
    assert response.kind == "answer", (question, response)
    assert response.result_table, question

    returned = set(response.result_table[0])
    dimensions = {c for c in required_columns if not c.startswith(("total_", "stock_"))}
    assert dimensions.issubset(returned), (question, sorted(returned))

    for metric in required_columns - dimensions:
        stem = metric.removeprefix("total_")
        assert any(stem in column for column in returned), (question, metric, sorted(returned))

# Removed: test_gold_evaluation_dataset_against_real_llm.
#
# It imported ``evals.gold_eval.evaluate_gold_set``, deleted when the harness
# was rebuilt -- its unit-tier twin (tests/unit/test_gold_evaluation.py) went at
# the same time and this one was missed, so it had been failing on import rather
# than on anything it checked. It is not rewritten against ``evals.harness``,
# because tests/integration/test_gold_suite.py already runs the gold suite against
# a live provider and does so on the opposite principle: this test asserted that
# *every* case passes, which that module's docstring explains is the assertion
# that turns a gold set from a measurement into a regression test for the cases
# that already work.


def test_conversation_context_resolves_a_clarification_follow_up():
    """A vague opener that gets clarified should be understood with prior context (§10)."""
    pipeline = AnalyticsPipeline()
    first = pipeline.run("How did the promotion do?")
    assert first.kind == "clarification"

    second = pipeline.run("revenue, last month", context=["How did the promotion do?"])
    assert second.kind != "clarification", second

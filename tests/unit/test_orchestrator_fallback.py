"""End-to-end orchestrator behaviour across all four intent archetypes, offline.

Runs entirely through the deterministic fallback path (tests/unit/conftest.py
strips any configured API key) -- zero network, zero API key requirement, and
fast. tests/integration/test_orchestrator_llm.py covers the same shapes against
a real LLM provider.
"""

from __future__ import annotations

from semantic_query_engine.agents.sql_generator import SQLGenerationResult
from semantic_query_engine.agents.validator import IssueCode, ValidationIssue, ValidationResult
from semantic_query_engine.pipeline.orchestrator import AnalyticsPipeline


def test_pipeline_lookup_question():
    pipeline = AnalyticsPipeline()
    response = pipeline.run("What is total revenue by region?")
    assert response.kind == "answer"
    assert response.result_table
    assert response.sql_source == "fallback"


def test_pipeline_comparative_question():
    pipeline = AnalyticsPipeline()
    response = pipeline.run("Which region had the highest week-on-week growth in units sold during March 2024?")
    assert response.kind == "answer"
    assert response.result_table
    assert response.intent == "comparative_analysis"


def test_pipeline_ambiguous_question_needs_clarification():
    pipeline = AnalyticsPipeline()
    response = pipeline.run("How did the promotion do?")
    assert response.kind == "clarification"
    assert response.prompt


def test_pipeline_sku_lookup_question():
    pipeline = AnalyticsPipeline()
    response = pipeline.run("What were total units sold for SKU MI-006 in the South region last week of January 2024?")
    assert response.kind == "answer"
    assert response.result_table
    assert response.intent == "descriptive_lookup"


def test_conversation_context_resolves_a_clarification_follow_up():
    """A vague opener that gets clarified is understood with prior context (§10)."""
    pipeline = AnalyticsPipeline()
    first = pipeline.run("How did the promotion do?")
    assert first.kind == "clarification"

    second = pipeline.run("revenue, last month", context=["How did the promotion do?"])
    assert second.kind != "clarification"


def test_repair_loop_stops_after_one_attempt_when_the_fallback_is_unrepairable():
    """Without an LLM, a repair that reproduces the same SQL can never succeed --
    the orchestrator should stop after the first wasted attempt instead of retrying
    an identical query for the full MAX_REPAIR_ATTEMPTS budget."""
    pipeline = AnalyticsPipeline()

    always_invalid = ValidationResult(
        is_valid=False,
        sanitized_sql="SELECT 1",
        issues=[ValidationIssue(IssueCode.UNKNOWN_COLUMN, "Unknown column 'x'.")],
    )
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

    assert response.kind == "failure"
    assert response.reason == "validation_failed"
    assert len(repair_calls) == 1
    assert any("no further attempts would help" in step for step in response.agent_trace)


def test_validation_failure_carries_machine_readable_issue_codes():
    """A failure reports *why* it was rejected in a form that can be counted.

    ``details`` is prose for the user; ``issue_codes`` is what an evaluation run
    groups by to report the validator's rejection breakdown.
    """
    pipeline = AnalyticsPipeline()
    pipeline.validator.run = lambda *a, **k: ValidationResult(
        is_valid=False,
        sanitized_sql="SELECT bogus FROM fmcg_sales",
        issues=[ValidationIssue(IssueCode.UNKNOWN_COLUMN, "Unknown column 'bogus'.")],
    )
    pipeline.generator.repair = lambda **kwargs: SQLGenerationResult(
        sql="SELECT bogus FROM fmcg_sales", source="fallback_unrepairable"
    )

    response = pipeline.run("What is total revenue by region?")

    assert response.kind == "failure"
    assert response.issue_codes == [IssueCode.UNKNOWN_COLUMN]


def test_concurrent_runs_on_one_pipeline_do_not_interfere():
    """A DuckDB connection is not safe to use from two threads at once, so each
    run takes its own cursor. Sharing one connection across concurrent requests --
    which any server in front of this pipeline does -- previously risked crossed
    results or a crash."""
    import concurrent.futures

    pipeline = AnalyticsPipeline()
    questions = [
        "What is total revenue by region?",
        "What is the stock depletion rate by region?",
        "Show monthly revenue trend",
        "Compare total revenue across all brands",
    ] * 3

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(pipeline.run, questions))

    assert all(r.kind == "answer" for r in results), [
        (r.kind, getattr(r, "details", None)) for r in results if r.kind != "answer"
    ]
    # Each question must have produced its own answer, not another thread's.
    by_question = {q: r for q, r in zip(questions, results, strict=True)}
    assert "region" in by_question["What is total revenue by region?"].sql_query
    assert "brand" in by_question["Compare total revenue across all brands"].sql_query


def test_every_result_variant_serialises_for_json_output():
    """The JSON surface and the eval harness read to_dict(), so every variant has one."""
    pipeline = AnalyticsPipeline()

    answer = pipeline.run("What is total revenue by region?")
    clarification = pipeline.run("How did the promotion do?")

    for result in (answer, clarification):
        payload = result.to_dict()
        assert payload["kind"] == result.kind
        assert "elapsed_ms" in payload

"""SQL generator: deterministic fallback template selection, and the LLM-primary
path exercised offline against a fake client (tests/unit/fakes.py)."""

from __future__ import annotations

from fakes import FakeChatClient, RaisingChatClient

from semantic_query_engine.agents.sql_generator import SQLGeneratorAgent


def test_sql_generator_handles_trend_and_time_based_questions():
    generator = SQLGeneratorAgent()
    result = generator.run(
        "Which region had the highest week-on-week growth in units sold during March 2024?",
        "Relevant schema context",
        "comparative_analysis",
    )
    sql = result.sql.lower()
    assert "date_trunc" in sql or "lag(" in sql or "week" in sql
    assert result.source == "fallback"


def test_fallback_honours_selected_year_for_non_yoy_analysis():
    generator = SQLGeneratorAgent()
    result = generator.run(
        "Compare promotion versus non-promotion revenue by category for 2024",
        "Relevant schema context",
        "comparative_analysis",
    )
    assert "2024-01-01" in result.sql
    assert "2024-12-31" in result.sql


def test_lifecycle_stage_question_uses_weekly_modeling_data():
    generator = SQLGeneratorAgent()
    result = generator.run(
        "What is the average units sold by lifecycle stage?", "Relevant schema context", "descriptive_lookup"
    )
    assert "weekly_modeling_data" in result.sql
    assert "lifecycle_stage" in result.sql


def test_revenue_fallback_uses_the_certified_formula_from_the_registry():
    generator = SQLGeneratorAgent()
    result = generator.run("What is total revenue by region?", "context", "descriptive_lookup")
    assert "units_sold * price_unit" in result.sql


def test_sku_lookup_binds_the_sku_as_a_parameter_not_a_literal():
    generator = SQLGeneratorAgent()
    result = generator.run(
        "What were total units sold for SKU MI-006 in the South region?", "context", "descriptive_lookup"
    )
    assert "?" in result.sql
    assert "MI-006" not in result.sql
    assert "MI-006" in result.params


# ---------------------------------------------------------------------------
# LLM-primary path, exercised offline via FakeChatClient
# ---------------------------------------------------------------------------


def test_llm_path_is_used_when_a_key_is_configured_and_returns_parsed_sql(monkeypatch):
    monkeypatch.setenv("SQE_LLM_API_KEY", "sk-test-placeholder-not-a-real-key")
    fake = FakeChatClient({"sql": "SELECT region, SUM(units_sold) FROM fmcg_sales GROUP BY region"})
    generator = SQLGeneratorAgent(client_factory=lambda settings: fake)

    result = generator.run("units sold by region", "schema context", "descriptive_lookup")

    assert result.source == "llm"
    assert result.sql == "SELECT region, SUM(units_sold) FROM fmcg_sales GROUP BY region"
    assert fake.calls, "expected the fake client to have been called"


def test_llm_path_falls_back_when_the_payload_is_missing_the_sql_field(monkeypatch):
    """A malformed LLM payload (§6) should degrade to the fallback, not an empty-string SQL."""
    monkeypatch.setenv("SQE_LLM_API_KEY", "sk-test-placeholder-not-a-real-key")
    fake = FakeChatClient({"unexpected_field": "oops"})
    generator = SQLGeneratorAgent(client_factory=lambda settings: fake)

    result = generator.run("units sold by region", "schema context", "descriptive_lookup")

    assert result.source == "fallback"
    assert result.sql


def test_llm_path_falls_back_on_a_provider_error(monkeypatch):
    monkeypatch.setenv("SQE_LLM_API_KEY", "sk-test-placeholder-not-a-real-key")
    raising = RaisingChatClient()
    generator = SQLGeneratorAgent(client_factory=lambda settings: raising)

    result = generator.run("units sold by region", "schema context", "descriptive_lookup")

    assert result.source == "fallback"
    assert result.sql


# ---------------------------------------------------------------------------
# repair() without an LLM: the deterministic fallback is a pure function of
# `question` alone, so it can't act on validator feedback -- it must say so
# rather than silently reproducing the SQL that just failed validation.
# ---------------------------------------------------------------------------


def test_repair_without_llm_reports_unrepairable_when_fallback_reproduces_the_failed_sql():
    generator = SQLGeneratorAgent()
    question = "What is total revenue by region?"
    original = generator.run(question, "context", "descriptive_lookup")

    result = generator.repair(
        question=question,
        schema_context="context",
        intent="descriptive_lookup",
        failed_sql=original.sql,
        validation_errors=["Unknown column 'bogus'."],
    )

    assert result.sql == original.sql
    assert result.source == "fallback_unrepairable"


def test_repair_without_llm_reports_fallback_repair_when_the_sql_actually_differs():
    generator = SQLGeneratorAgent()

    result = generator.repair(
        question="What is total revenue by region?",
        schema_context="context",
        intent="descriptive_lookup",
        failed_sql="SELECT 1",  # deliberately not what the fallback would generate
        validation_errors=["some validator error"],
    )

    assert result.source == "fallback_repair"

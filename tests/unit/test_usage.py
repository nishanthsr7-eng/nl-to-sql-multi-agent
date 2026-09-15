"""Token and cost accounting (ROADMAP Phase 3 task 4).

The regressions worth guarding here are all the same shape: a number the system
does not actually know being reported as if it did. An unpriced model must not
cost $0, a partial total must not look complete, and a run the deterministic
fallback answered must not be priced as a model run.
"""

from __future__ import annotations

import pytest

from semantic_query_engine.core.usage import (
    NO_USAGE,
    TokenUsage,
    format_cost,
    price_for,
    usage_from_response,
)


class _Usage:
    def __init__(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _Response:
    def __init__(self, usage: _Usage | None = None) -> None:
        self.usage = usage


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

def test_a_dated_snapshot_inherits_its_base_model_price():
    """Providers pin dated ids (``gpt-4o-mini-2024-07-18``). Without prefix
    matching every real deployment would report an unpriced run."""
    assert price_for("gpt-4o-mini-2024-07-18") == price_for("gpt-4o-mini")


def test_prefix_matching_prefers_the_longest_match():
    """``gpt-4o-mini`` starts with ``gpt-4o``, and the two are priced an order of
    magnitude apart. Picking the shorter match would overprice every mini run by
    ~16x."""
    assert price_for("gpt-4o-mini") == price_for("gpt-4o-mini")
    assert price_for("gpt-4o-mini").input_per_mtok < price_for("gpt-4o").input_per_mtok


def test_an_unknown_model_has_no_price_rather_than_a_free_one():
    assert price_for("some-model-nobody-priced") is None


# ---------------------------------------------------------------------------
# Reading usage off a response
# ---------------------------------------------------------------------------

def test_tokens_are_priced_per_million_and_split_by_direction():
    usage = usage_from_response(_Response(_Usage(1_000_000, 1_000_000)), "gpt-4o-mini")

    assert usage.prompt_tokens == 1_000_000
    assert usage.total_tokens == 2_000_000
    assert usage.calls == 1
    # 0.15 in + 0.60 out, per the published per-Mtok rates.
    assert usage.cost_usd == pytest.approx(0.75)


def test_an_unpriced_model_reports_tokens_and_no_cost():
    """The load-bearing one: a zero here would print "$0.000000 per query" under
    an accuracy table, which is a false claim that happens to flatter the
    system."""
    usage = usage_from_response(_Response(_Usage(500, 500)), "some-model-nobody-priced")

    assert usage.total_tokens == 1000
    assert usage.cost_usd is None


def test_a_response_without_a_usage_block_does_not_raise():
    """Accounting must never be what fails a query that otherwise worked -- and
    the fake client in tests/unit/fakes.py omits usage by default, which is the
    same shape as a provider that does not report it."""
    usage = usage_from_response(_Response(), "gpt-4o-mini")

    assert usage.calls == 1
    assert usage.total_tokens == 0


def test_a_model_on_a_loopback_endpoint_is_free_whatever_it_is_called():
    """Zero is the truth for the local Ollama runner, and it has to survive the
    model being renamed -- the configured local model is ``sqe-coder``, a name no
    price table would ever contain."""
    local = usage_from_response(
        _Response(_Usage(10, 10)), "sqe-coder", "http://localhost:11434/v1"
    )
    assert local.cost_usd == 0.0
    assert local.total_tokens == 20

    # Same unknown model name, a remote endpoint: now genuinely unpriced.
    remote = usage_from_response(
        _Response(_Usage(10, 10)), "sqe-coder", "https://api.example.com/v1"
    )
    assert remote.cost_usd is None


# ---------------------------------------------------------------------------
# Summing
# ---------------------------------------------------------------------------

def test_usage_sums_across_the_calls_one_run_makes():
    total = NO_USAGE + TokenUsage(100, 20, 1, 0.5) + TokenUsage(300, 40, 1, 1.5)

    assert (total.prompt_tokens, total.completion_tokens, total.calls) == (400, 60, 2)
    assert total.cost_usd == pytest.approx(2.0)


def test_one_unpriced_call_makes_the_whole_total_unpriced():
    """A total that silently dropped the unpriced half would understate the run
    while still looking like a complete number."""
    total = TokenUsage(100, 20, 1, 0.5) + TokenUsage(100, 20, 1, None)

    assert total.total_tokens == 240
    assert total.cost_usd is None


def test_no_usage_is_a_real_zero_not_an_unknown():
    """A run that never called a provider -- the deterministic fallback, or an
    ablation rung with generation off -- cost zero, and that is knowable."""
    assert NO_USAGE.cost_usd == 0.0
    assert NO_USAGE.calls == 0


def test_a_round_trip_through_a_dict_preserves_an_unpriced_total():
    """Case records are persisted as plain dicts in evals/results/*.json; a
    ``None`` cost that came back as 0.0 would resurrect the flattering number
    one file read later."""
    restored = TokenUsage.from_dict(TokenUsage(10, 5, 1, None).to_dict())

    assert restored.cost_usd is None
    assert restored.total_tokens == 15


def test_an_unknown_cost_renders_as_na_not_as_free():
    assert format_cost(None) == "n/a"
    assert format_cost(0.0) == "$0.000000"


# ---------------------------------------------------------------------------
# Accumulation through a run
# ---------------------------------------------------------------------------

def test_a_run_reports_what_the_whole_question_cost_not_the_last_call():
    """The orchestrator sums generation, every repair attempt and synthesis, then
    stamps the total on the result. Synthesis is the last agent to touch a
    response and sets its own call's usage on the way past; a result carrying
    that instead of the total would price a three-attempt repair loop as if it
    were a single call.
    """
    from semantic_query_engine.agents.sql_generator import SQLGenerationResult
    from semantic_query_engine.pipeline.orchestrator import AnalyticsPipeline

    pipeline = AnalyticsPipeline()
    pipeline.generator.run = lambda *a, **k: SQLGenerationResult(  # type: ignore[method-assign]
        sql="SELECT region, SUM(units_sold) AS total_units"
            " FROM fmcg_sales s JOIN dim_store d ON s.store_id = d.store_id"
            " GROUP BY region",
        source="llm",
        usage=TokenUsage(prompt_tokens=900, completion_tokens=100, calls=1, cost_usd=0.25),
    )

    result, trace = pipeline.run_traced("total units by region")

    assert result.kind == "answer"
    assert trace.usage.prompt_tokens == 900
    assert result.usage.total_tokens >= 1000
    assert result.usage is trace.usage
    assert result.to_dict()["usage"]["cost_usd"] == pytest.approx(0.25)


def test_a_failed_run_still_carries_its_cost():
    """A run that burned generations and then failed validation cost real money.
    Dropping it would let the ladder price the guardrails by counting only the
    queries they let through."""
    from semantic_query_engine.agents.sql_generator import SQLGenerationResult
    from semantic_query_engine.pipeline.orchestrator import AnalyticsPipeline

    spend = TokenUsage(prompt_tokens=800, completion_tokens=200, calls=1, cost_usd=0.5)
    pipeline = AnalyticsPipeline()
    pipeline.generator.run = lambda *a, **k: SQLGenerationResult(  # type: ignore[method-assign]
        sql="SELECT nonexistent_column FROM fmcg_sales", source="llm", usage=spend
    )
    pipeline.generator.repair = lambda **kwargs: SQLGenerationResult(  # type: ignore[method-assign]
        sql="SELECT still_nonexistent FROM fmcg_sales", source="llm_repair", usage=spend
    )

    result, _ = pipeline.run_traced("total revenue by region")

    assert result.kind == "failure"
    assert result.usage.calls >= 2
    assert result.usage.cost_usd == pytest.approx(0.5 * result.usage.calls)

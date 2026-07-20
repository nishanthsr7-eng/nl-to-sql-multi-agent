"""Runs the gold evaluation dataset end-to-end through the orchestrator.

The evaluation logic (structural + value-level assertions) lives in
evals/gold_eval.py and is shared with evals/run_gold_eval.py -- see that
module for how to extend the dataset. Parametrizing per case gives a normal
pytest pass/fail per gold query instead of one assertion for the whole set.
"""

from __future__ import annotations

import pytest
from evals.gold_eval import evaluate_case, load_gold_cases

from semantic_query_engine.pipeline.orchestrator import AnalyticsPipeline

CASES = load_gold_cases()


@pytest.fixture(scope="module")
def pipeline():
    return AnalyticsPipeline()


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_gold_case(pipeline, case):
    result = evaluate_case(pipeline, case)
    assert result.passed, f"{result.message}\nSQL: {result.sql}"

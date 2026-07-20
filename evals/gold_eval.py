"""Shared gold-evaluation harness.

Both ``evals/run_gold_eval.py`` (a readable pass/fail report with SQL and agent
trace for failures) and ``tests/test_gold_evaluation.py`` (a pytest assertion
per case) call :func:`evaluate_gold_set` -- there used to be two independent
copies of this loop that had to be kept in sync by hand (ARCHITECTURE_REVIEW.md
§9.3).

Cases assert structural shape (intent, required columns) and, where an
``expected_values`` block is present, that the *result values* match a
precomputed correct answer within ``tolerance`` -- not just that the right
columns came back (§9.4).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from semantic_query_engine.core.config import GOLD_QUERIES_PATH
from semantic_query_engine.pipeline.orchestrator import AnalyticsPipeline

DEFAULT_TOLERANCE = 0.01  # 1% relative tolerance for float comparisons


@dataclass
class GoldCaseResult:
    case_id: str
    passed: bool
    message: str = ""
    sql: str = ""
    agent_trace: list[str] = field(default_factory=list)


def load_gold_cases(path=GOLD_QUERIES_PATH) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def _check_expected_values(row: dict[str, Any], expected: dict[str, Any], tolerance: float) -> None:
    for column, expected_value in expected.items():
        actual = row.get(column)
        if isinstance(expected_value, (int, float)) and not isinstance(expected_value, bool):
            assert actual is not None, f"{column}: expected ~{expected_value}, got no value"
            allowed = abs(expected_value) * tolerance + 1e-9
            assert abs(actual - expected_value) <= allowed, (
                f"{column}: expected ~{expected_value} (+/-{tolerance:.0%}), got {actual}"
            )
        else:
            assert actual == expected_value, f"{column}: expected {expected_value!r}, got {actual!r}"


def evaluate_case(pipeline: AnalyticsPipeline, case: dict[str, Any]) -> GoldCaseResult:
    response = pipeline.run(case["question"])
    try:
        if case["expect_clarification"]:
            assert isinstance(response, dict), "expected a clarification dict, got a structured response"
            assert response.get("needs_clarification") is True
        else:
            assert hasattr(response, "result_table"), "pipeline returned an error dict, not a response"
            assert response.intent == case["intent"], f"intent={response.intent!r} expected={case['intent']!r}"
            assert response.result_table, "query returned zero rows"
            top_row = response.result_table[0]
            missing = set(case["required_columns"]) - set(top_row)
            assert not missing, f"missing required columns: {sorted(missing)}"
            expected_values = case.get("expected_values")
            if expected_values:
                _check_expected_values(top_row, expected_values, case.get("tolerance", DEFAULT_TOLERANCE))
    except AssertionError as exc:
        sql = response.get("sql_query", "") if isinstance(response, dict) else response.sql_query
        trace = response.get("agent_trace", []) if isinstance(response, dict) else response.agent_trace
        return GoldCaseResult(case["id"], False, str(exc), sql, trace)

    sql = "" if isinstance(response, dict) else response.sql_query
    return GoldCaseResult(case["id"], True, sql=sql)


def evaluate_gold_set(
    pipeline: AnalyticsPipeline, cases: list[dict[str, Any]] | None = None
) -> list[GoldCaseResult]:
    cases = cases if cases is not None else load_gold_cases()
    return [evaluate_case(pipeline, case) for case in cases]

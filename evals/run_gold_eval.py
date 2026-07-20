"""Standalone gold-evaluation runner.

Runs every case in evals/datasets/gold_queries.json through the orchestrator
and prints a readable pass/fail report (SQL and agent trace for failures).
The evaluation logic itself lives in evals/gold_eval.py and is shared with
tests/test_gold_evaluation.py -- run this file directly for a report instead
of a pytest pass/fail line.

Usage:
    python evals/run_gold_eval.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make both `semantic_query_engine` (src/) and `evals` (this file's parent) importable when
# run directly, without requiring an editable install.
_ROOT = Path(__file__).resolve().parents[1]
for candidate in (_ROOT, _ROOT / "src"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from evals.gold_eval import evaluate_gold_set  # noqa: E402
from semantic_query_engine.pipeline.orchestrator import AnalyticsPipeline  # noqa: E402


def run() -> int:
    pipeline = AnalyticsPipeline()
    results = evaluate_gold_set(pipeline)

    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(f"{status}  {result.case_id}" + ("" if result.passed else f": {result.message}"))
        if not result.passed:
            print(f"      SQL: {result.sql}")
            for step in result.agent_trace[-3:]:
                print(f"      trace: {step}")

    passed = sum(r.passed for r in results)
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())

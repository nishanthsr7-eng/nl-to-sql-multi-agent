"""Qualitative preview of the synthesis agent's output across the intent archetypes.

Unlike run_gold_eval.py (which only checks structural correctness: intent and
required columns), this prints the actual narrative, key metric, and chart
recommendation for one representative question per archetype -- useful when
tuning the synthesis prompt or comparing LLM output against the deterministic
fallback.

Usage:
    python evals/run_synthesis_preview.py
"""

from __future__ import annotations

try:
    import semantic_query_engine  # noqa: F401
except ImportError:  # pragma: no cover - convenience fallback, not the primary path
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from semantic_query_engine.pipeline.orchestrator import AnalyticsPipeline

REPRESENTATIVE_QUESTIONS = [
    ("A: SKU Lookup",         "What were total units sold for SKU MI-006 in the PL-South region in the last week of January 2024?"),
    ("A: Stock Depletion",    "What is the stock depletion rate by category?"),
    ("B: Brand Revenue",      "Compare total revenue across all brands"),
    ("B: Promo by Channel",   "Compare units sold during promotion vs non-promotion periods by channel"),
    ("B: Year-on-Year",       "Show year over year revenue trend"),
    ("C: Category x Channel", "Show units sold by category across each channel"),
    ("D: Ambiguous",          "How did the promotion do?"),
]


def run() -> None:
    pipeline = AnalyticsPipeline()

    for label, question in REPRESENTATIVE_QUESTIONS:
        print("=" * 70)
        print(f"[{label}]")
        print(f"Q: {question}")
        print("-" * 70)

        result = pipeline.run(question)

        if isinstance(result, dict):
            print("[CLARIFICATION NEEDED]")
            print(result.get("clarification_prompt", ""))
        else:
            print(f"Archetype  : {result.archetype_label}")
            print(f"Key Metric : {result.key_metric}")
            print()
            print("Summary:")
            print(result.narrative_summary)
            print()
            if result.comparison_context:
                print(f"Comparison : {result.comparison_context}")
            print(f"Chart Rec  : {result.chart_recommendation}")
            print(f"Rows       : {len(result.result_table)}")
            print(f"SQL        : {result.sql_query[:100]}...")
        print()


if __name__ == "__main__":
    run()

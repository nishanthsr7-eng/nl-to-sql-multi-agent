"""Static content: landing-page sample questions and error recovery suggestions.

Nothing here touches Streamlit or the pipeline -- it is pure content lookup,
which makes it trivial to unit test and to extend with new question families.
"""

from __future__ import annotations

# (button label, sample question) shown on the landing page before the first
# message. One example per question family, kept short.
EXAMPLE_QUESTIONS: list[tuple[str, str]] = [
    ("Brand ranking", "Compare total revenue across all brands"),
    ("Promotion impact by channel", "Compare promotion versus non-promotion units sold by channel"),
    ("Stock depletion by SKU", "What is the stock depletion rate by SKU?"),
    ("Monthly revenue trend", "Show monthly revenue trend"),
    ("Category x channel", "Show revenue by category across each channel"),
    ("Year over year growth", "Show year over year revenue trend"),
]


def recovery_ideas(question: str) -> list[str]:
    """Give users a working alternative when a guarded query cannot run."""
    lower = question.lower()
    if "promotion" in lower or "promo" in lower:
        return [
            "Compare promotion versus non-promotion units sold by channel.",
            "Compare promotion versus non-promotion revenue by category.",
            "Which brand had the highest promotional uplift?",
        ]
    if "stock" in lower or "inventory" in lower:
        return [
            "What is the stock depletion rate by SKU?",
            "Compare stock depletion rate by category.",
            "What is the stock depletion rate by channel?",
        ]
    return [
        "Compare total revenue across all brands.",
        "Show units sold by category across each channel.",
        "Show year-over-year revenue trend.",
    ]


def apply_period_filter(question: str, selected_period: str) -> str:
    """Append a sidebar period choice without overriding an explicit user timeframe."""
    if selected_period == "All available data" or any(str(year) in question for year in range(2020, 2031)):
        return question
    return f"{question.rstrip('?')} for {selected_period}"

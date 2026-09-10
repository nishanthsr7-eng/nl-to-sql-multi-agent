"""Sample questions and failure-recovery suggestions, read from the domain.

This was ``ui/catalog.py``. It never touched Streamlit -- it is pure content
lookup -- so it outlived the UI it was written for and moved here when the CLI
replaced it. ``apply_period_filter`` did not: it existed only to fold a sidebar
dropdown into the question text, and a CLI user simply types the period.

Phase 4 emptied it of content. Both lists used to be FMCG literals; they are now
read from the active domain's semantic layer, because a suggestion is only
useful if it names tables the warehouse actually has -- offering "compare total
revenue across all brands" to someone querying an airline warehouse is worse
than offering nothing.
"""

from __future__ import annotations

from semantic_query_engine.domain.registry import load_domain_registries
from semantic_query_engine.semantic.layer import load_semantic_layer


def example_questions() -> list[tuple[str, str]]:
    """Shown by ``sqe repl`` on start and by ``sqe examples``, one per family."""
    return [
        (str(entry.get("label", "")), str(entry.get("question", "")))
        for entry in load_semantic_layer().example_questions
    ]


def recovery_ideas(question: str) -> list[str]:
    """Give users a working alternative when a guarded query cannot run."""
    return load_domain_registries().language.recovery_ideas(question.lower())

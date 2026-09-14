"""Few-shot (question -> DuckDB SQL) pairs, read from the active domain.

The pairs themselves used to be an eleven-entry literal here. They are written
in the FMCG warehouse's own tables, so a second domain would have been shown
eleven worked examples of a schema it does not have -- the same failure this
module already suffered once, in the other direction: when Phase 4 normalised
the warehouse into a star schema, these examples still selected ``region``,
``brand`` and ``category`` straight off ``fmcg_sales``. The retrieved schema
context correctly said those columns had moved, but a model shown a rule and
then eleven worked examples breaking it follows the examples: 80% of
first-attempt generations were rejected with ``unknown_column``, which reads as
model weakness rather than as a stale prompt.

They now live in the ``few_shot_examples`` block of each domain's semantic
layer, and ``tests/unit/test_few_shot_examples.py`` still executes every one of
the active domain's against its warehouse so a bank cannot drift from its schema
silently.
"""

from __future__ import annotations

from typing import TypedDict

from semantic_query_engine.semantic.layer import load_semantic_layer


class FewShotExample(TypedDict):
    question: str
    sql: str


def few_shot_examples() -> list[FewShotExample]:
    """The active domain's verified (question -> SQL) pairs, in declared order."""
    return [
        FewShotExample(question=str(entry.get("question", "")), sql=str(entry.get("sql", "")))
        for entry in load_semantic_layer().few_shot_examples
    ]

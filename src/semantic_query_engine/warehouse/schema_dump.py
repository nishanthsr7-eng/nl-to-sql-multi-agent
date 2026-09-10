"""Raw ``information_schema`` context -- the no-semantic-layer baseline.

This is the prompt context most text-to-SQL demos actually build: every column
of every allowed table, typed, with no business metric definitions, no dimension
vocabulary and no relevance ranking. It exists so the bottom rung of the
evaluation ladder is a *fair* representation of that approach rather than a
strawman -- the model gets the complete, correct schema, just none of the
semantic layer's interpretation of it.

The output is a :class:`~semantic_query_engine.semantic.retriever.RetrievedContext`
so the orchestrator can substitute it for the retriever without branching on
context shape downstream.
"""

from __future__ import annotations

from typing import Any

import duckdb

from semantic_query_engine.core.domains import active_domain
from semantic_query_engine.semantic.retriever import RetrievedContext


def raw_schema_context(conn: duckdb.DuckDBPyConnection) -> RetrievedContext:
    """Every allowed table's columns, formatted for a prompt, with no metrics."""
    tables: list[dict[str, Any]] = []
    blocks: list[str] = []

    for table in sorted(active_domain().allowed_tables):
        rows = conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = ? ORDER BY ordinal_position",
            [table],
        ).fetchall()
        if not rows:
            continue
        columns = [{"name": name, "type": dtype} for name, dtype in rows]
        tables.append({"table_name": table, "columns": columns})
        rendered = ", ".join(f"{c['name']} {c['type']}" for c in columns)
        blocks.append(f"TABLE {table}({rendered})")

    # No "business metrics" section and no guidance block: the absence is the
    # point of this baseline, so it must not be quietly compensated for here.
    return RetrievedContext(tables=tables, metrics=[], formatted_context="\n".join(blocks))

"""Load the semantic layer definition (tables, columns, certified business metrics).

The semantic layer is a single JSON document hand-authored by whoever owns the
warehouse schema — see data/semantic/semantic_layer.json. This module only
parses it into plain dict structures and builds the text documents that get
embedded or keyword-matched for retrieval; it has no retrieval logic itself.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from semantic_query_engine.core.domains import active_domain


@dataclass
class SemanticLayer:
    tables: list[dict[str, Any]]
    metrics: list[dict[str, Any]]
    dimension_values: dict[str, list[str]]
    dimension_aliases: dict[str, dict[str, str]]
    # Regex patterns for high-cardinality identifiers (SKU codes, store ids) that
    # are impractical to enumerate as dimension values. Declared here rather than
    # hardcoded in the planner and validator so both extract the same entities.
    identifier_patterns: dict[str, str]
    # The domain's vocabulary for the rule-based planner and the prompt blocks:
    # which dimensions anchor a question, which words name a metric, what a
    # clarification should offer. It lives here rather than as literals in
    # ``agents/planner.py`` for the same reason the dimension values do -- a
    # second domain would otherwise need a code change to be understood, which
    # would falsify the whole "the semantic layer is the contract" claim.
    language: dict[str, Any]
    # Few-shot SQL exemplars and the CLI's example questions, both of which are
    # written in the domain's own tables and would be actively misleading if a
    # second domain inherited them.
    few_shot_examples: list[dict[str, Any]]
    example_questions: list[dict[str, str]]
    # Declared foreign-key paths between tables. The validator uses them to tell
    # a supported join from an arbitrary one; without them, any two tables in the
    # star look equally joinable and a cross join reads as intentional.
    relationships: list[dict[str, Any]]
    # Row-level security policies and PII column tags. Here rather than in a
    # separate governance file for the same reason the grain is here: who may
    # see which rows is a statement about what the data *means* (a sales row
    # belongs to a store, a store belongs to a region), and a second document
    # would be free to disagree with this one about which tables exist.
    # Absent in a domain that declares no policies; see
    # :mod:`semantic_query_engine.governance.policy`.
    governance: dict[str, Any] = field(default_factory=dict)


def load_semantic_layer(path: Path | None = None) -> SemanticLayer:
    """Parse a domain's semantic layer, defaulting to the active domain's.

    Resolved per call rather than bound as a default argument: ``--domain`` is
    selected after this module is imported, and a default argument would freeze
    the path at import time and serve the retail layer over an airline warehouse.
    """
    path = path or active_domain().semantic_layer_path
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    return SemanticLayer(
        tables=payload.get("tables", []),
        metrics=payload.get("business_metrics", []),
        dimension_values=payload.get("dimension_values", {}),
        dimension_aliases=payload.get("dimension_aliases", {}),
        identifier_patterns=payload.get("identifier_patterns", {}),
        relationships=payload.get("relationships", []),
        language=payload.get("language", {}),
        few_shot_examples=payload.get("few_shot_examples", []),
        example_questions=payload.get("example_questions", []),
        governance=payload.get("governance", {}),
    )


def build_table_document(table: dict[str, Any]) -> str:
    """Render a table definition as text suitable for embedding or keyword search."""
    lines = [
        f"Table Name: {table.get('table_name', '')}",
        f"Description: {table.get('description', '')}",
    ]
    # Grain goes in the prompt, not just in the validator. Rejecting a fan-out
    # join after the fact is a repair round-trip; telling the model what one row
    # of each table means usually prevents the generation outright.
    grain = table.get("grain") or {}
    if grain.get("columns"):
        lines.append(f"Grain (one row per): {', '.join(grain['columns'])}")
    if grain.get("additive_measures"):
        lines.append(f"Additive measures: {', '.join(grain['additive_measures'])}")
    if grain.get("level_measures"):
        lines.append(
            f"Level measures (do not SUM across {grain['columns'][0]}): "
            f"{', '.join(grain['level_measures'])}"
        )
    lines.append("Columns:")
    for col in table.get("columns", []):
        lines.append(f"- {col.get('name')}: {col.get('description', '')}")
    return "\n".join(lines)


def build_relationship_document(relationships: list[dict[str, Any]]) -> str:
    """Render the declared join paths as a prompt block.

    Without this the model has to infer joinability from column names, which is
    how ``fmcg_sales.date = weekly_modeling_data.week`` gets written: the names
    are both dates and both plausible.
    """
    if not relationships:
        return ""
    lines = ["Supported join paths:"]
    for rel in relationships:
        lines.append(f"- {rel.get('from')} -> {rel.get('to')} ({rel.get('cardinality', '')})")
    return "\n".join(lines)


def build_metric_document(metric: dict[str, Any]) -> str:
    """Render a business-metric definition as text suitable for embedding or keyword search."""
    return (
        f"Metric Name: {metric.get('metric_name', '')}\n"
        f"Definition: {metric.get('definition', '')}\n"
        f"Description: {metric.get('description', '')}"
    )

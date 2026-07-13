"""Load the semantic layer definition (tables, columns, certified business metrics).

The semantic layer is a single JSON document hand-authored by whoever owns the
warehouse schema — see data/semantic/semantic_layer.json. This module only
parses it into plain dict structures and builds the text documents that get
embedded or keyword-matched for retrieval; it has no retrieval logic itself.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from semantic_query_engine.core.config import SEMANTIC_LAYER_PATH


@dataclass
class SemanticLayer:
    tables: list[dict[str, Any]]
    metrics: list[dict[str, Any]]
    dimension_values: dict[str, list[str]]
    dimension_aliases: dict[str, dict[str, str]]


def load_semantic_layer(path: Path = SEMANTIC_LAYER_PATH) -> SemanticLayer:
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    return SemanticLayer(
        tables=payload.get("tables", []),
        metrics=payload.get("business_metrics", []),
        dimension_values=payload.get("dimension_values", {}),
        dimension_aliases=payload.get("dimension_aliases", {}),
    )


def build_table_document(table: dict[str, Any]) -> str:
    """Render a table definition as text suitable for embedding or keyword search."""
    lines = [
        f"Table Name: {table.get('table_name', '')}",
        f"Description: {table.get('description', '')}",
        "Columns:",
    ]
    for col in table.get("columns", []):
        lines.append(f"- {col.get('name')}: {col.get('description', '')}")
    return "\n".join(lines)


def build_metric_document(metric: dict[str, Any]) -> str:
    """Render a business-metric definition as text suitable for embedding or keyword search."""
    return (
        f"Metric Name: {metric.get('metric_name', '')}\n"
        f"Definition: {metric.get('definition', '')}\n"
        f"Description: {metric.get('description', '')}"
    )

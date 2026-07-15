"""Domain registry -- the one place business formulas and dimension values live.

``prompts``, ``agents.validator``, ``agents.planner`` and ``agents.sql_generator``
all import from here instead of hardcoding their own copies of a metric formula
or a dimension's allowed values. See :mod:`semantic_query_engine.domain.registry`.
"""

from __future__ import annotations

from semantic_query_engine.domain.registry import (
    DimensionRegistry,
    MetricDefinition,
    MetricRegistry,
    load_domain_registries,
)

__all__ = [
    "DimensionRegistry",
    "MetricDefinition",
    "MetricRegistry",
    "load_domain_registries",
]

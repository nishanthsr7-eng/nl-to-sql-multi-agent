"""Semantic retrieval: table/metric grounding for a natural-language question."""

from __future__ import annotations

from semantic_query_engine.semantic.retriever import SemanticRetriever


def test_semantic_retrieval():
    retriever = SemanticRetriever()
    ctx = retriever.retrieve("What is total revenue by region for beverages?")
    assert ctx.tables
    assert "fmcg_sales" in {t["table_name"] for t in ctx.tables}


def test_semantic_retrieval_uses_synonyms_for_promotion_metrics():
    retriever = SemanticRetriever()
    ctx = retriever.retrieve("How did promotions affect sales?")
    metric_names = {metric["metric_name"] for metric in ctx.metrics}
    assert "promotional_uplift" in metric_names


def test_owns_all_rejects_a_table_missing_one_of_the_formulas_columns():
    """The metric-owner branch of the join closure must not accept a fact that
    carries only part of the formula.

    Regression: `_looks_like_column` tested ownership against the *same* table
    `_owns_all` was checking, so any column the table lacked was filtered out of
    the check meant to catch it, and the function degraded to "owns at least one
    token". `fact_inventory` has `units_sold` but no `price_unit`, so it claimed
    SUM(units_sold * price_unit) and retrieval picked it over `fmcg_sales`
    whenever ranking placed inventory higher -- which vector retrieval does.
    """
    from semantic_query_engine.domain import GrainRegistry
    from semantic_query_engine.semantic.layer import load_semantic_layer
    from semantic_query_engine.semantic.retriever import _owns_all

    layer = load_semantic_layer()
    grains = GrainRegistry(layer.tables, layer.relationships)
    required = {"sum", "units_sold", "price_unit"}

    assert _owns_all(grains, "fmcg_sales", required)
    assert not _owns_all(grains, "fact_inventory", required)


def test_any_table_owns_column_separates_columns_from_sql_keywords():
    """The distinction `_owns_all` depends on: a formula token is a real column
    when some table declares it, and a SQL keyword when none does."""
    from semantic_query_engine.domain import GrainRegistry
    from semantic_query_engine.semantic.layer import load_semantic_layer

    layer = load_semantic_layer()
    grains = GrainRegistry(layer.tables, layer.relationships)

    assert grains.any_table_owns_column("price_unit")
    assert grains.any_table_owns_column("units_sold")
    assert not grains.any_table_owns_column("sum")
    assert not grains.any_table_owns_column("case")

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

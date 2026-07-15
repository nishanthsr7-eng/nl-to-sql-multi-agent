"""Planner agent: intent classification and ambiguity detection."""

from __future__ import annotations

from semantic_query_engine.agents.planner import IntentArchetype, PlannerAgent


def test_planner_detects_ambiguity():
    planner = PlannerAgent()
    result = planner.run("How did the promotion do?")
    assert result.intent == IntentArchetype.AMBIGUOUS
    assert result.needs_clarification


def test_planner_accepts_explicit_promotion_metric_without_timeframe():
    planner = PlannerAgent()
    result = planner.run("How did promotions affect units sold by region?")
    assert not result.needs_clarification


def test_planner_extracts_brand_from_the_domain_registry():
    """Brand extraction reads DimensionRegistry like every other dimension (planner.py
    docstring) -- it previously used a hardcoded regex that silently drifted from the
    registry (only matched a fixed set of two-letter prefixes + single digit)."""
    planner = PlannerAgent()
    result = planner.run("What was total revenue for MiBrand1 in 2024?")
    assert result.entities["brand"] == "MiBrand1"

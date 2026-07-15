"""Planner agent — Intent-Based Classification Pipeline (Archetypes A-D).

Deliberately rule-based, unlike the other LLM-primary agents in this pipeline
(SQL generation, synthesis) -- not an unfinished corner. Intent classification
here is a cheap, zero-network, fully explainable pre-filter that runs before
any expensive LLM call; its entity extraction reads dimension values from the
same semantic_query_engine.domain registry the rest of the system uses, so it can't drift
out of sync with what the SQL layer actually recognises. See ARCHITECTURE.md
for the full reasoning, and ARCHITECTURE_REVIEW.md §3 for the tradeoff this
was weighed against (an LLM-primary planner, for higher accuracy on phrasings
the keyword lists don't anticipate, at the cost of latency/cost per question).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from semantic_query_engine.domain.registry import DimensionRegistry, load_domain_registries


class IntentArchetype(str, Enum):
    # Archetype A — explicit entity match, single data point
    LOOKUP = "descriptive_lookup"
    # Archetype B — period or entity deltas, window functions
    COMPARATIVE = "comparative_analysis"
    # Archetype C — multi-table joins, rankings, pivot operations
    DIAGNOSTIC = "diagnostic_pivot"
    # Archetype D — missing parameters, conversational loop required
    AMBIGUOUS = "ambiguous"


# Human-readable labels for UI display
ARCHETYPE_LABELS: dict[IntentArchetype, str] = {
    IntentArchetype.LOOKUP:      "A · Descriptive Lookup",
    IntentArchetype.COMPARATIVE: "B · Comparative Analysis",
    IntentArchetype.DIAGNOSTIC:  "C · Diagnostic & Pivot",
    IntentArchetype.AMBIGUOUS:   "D · Ambiguous — Clarification Needed",
}

ARCHETYPE_DESCRIPTIONS: dict[IntentArchetype, str] = {
    IntentArchetype.LOOKUP: (
        "Single-fact retrieval — explicit entity matching across product, region and time "
        "attributes to extract a standalone data point."
    ),
    IntentArchetype.COMPARATIVE: (
        "Multi-period or multi-entity delta — requires time-series manipulation, window "
        "functions and mathematical delta calculations across two or more entities."
    ),
    IntentArchetype.DIAGNOSTIC: (
        "Multi-table join and ranking — high-complexity operation spanning multiple "
        "transactional boundaries with sorting and cross-entity analysis."
    ),
    IntentArchetype.AMBIGUOUS: (
        "Insufficient parameters detected — metric, timeframe or product scope is missing. "
        "The system triggers a conversational clarification loop rather than guessing."
    ),
}


@dataclass
class PlanResult:
    intent: IntentArchetype
    needs_clarification: bool
    clarification_prompt: str | None
    entities: dict[str, str | None]
    reasoning: str
    archetype_label: str = ""
    archetype_description: str = ""
    missing_params: list[str] = field(default_factory=list)


class PlannerAgent:
    # ---------------------------------------------------------------------------
    # Archetype B — Comparative Analysis signals
    # ---------------------------------------------------------------------------
    _COMPARATIVE_HINTS = (
        "compare", "vs", "versus", "growth", "delta",
        "week-on-week", "month over month", "year over year", "yoy",
        "highest", "lowest", "top", "rank",
        "more than", "less than", "better", "worse",
        "outperform", "underperform", "difference",
        "increased", "decreased", "improved", "declined",
        "did last month", "last month campaign",
        "campaign improve",
    )

    # ---------------------------------------------------------------------------
    # Archetype C — Diagnostic & Pivot signals
    # ---------------------------------------------------------------------------
    _DIAGNOSTIC_HINTS = (
        "which", "benefit", "inventory", "promotion",
        "category", "join", "across", "impact", "drivers",
        "breakdown", "split", "by brand", "by channel",
        "by category", "by pack", "by segment",
        "benefited most", "saw", "reduction", "during",
        "trade promotion", "sku hierarchy",
    )

    def __init__(self, dimensions: DimensionRegistry | None = None):
        self.dimensions = dimensions or load_domain_registries()[1]

    def run(self, question: str) -> PlanResult:
        q     = question.strip()
        lower = q.lower()

        entities = {
            "sku":       self._extract_sku(lower),
            "region":    self.dimensions.match("region", lower),
            "timeframe": self._extract_timeframe(lower),
            "metric":    self._extract_metric(lower),
            "category":  self.dimensions.match("category", lower),
            "channel":   self.dimensions.match("channel", lower),
            "pack_type": self.dimensions.match("pack_type", lower),
            "brand":     self.dimensions.match("brand", lower),
        }

        # -----------------------------------------------------------------
        # Archetype D — detect ambiguity first (hard boundary)
        # -----------------------------------------------------------------
        if self._is_ambiguous(lower, entities):
            missing = self._identify_missing_params(lower, entities)
            clarification = self._build_clarification_prompt(lower, missing)
            result = PlanResult(
                intent=IntentArchetype.AMBIGUOUS,
                needs_clarification=True,
                clarification_prompt=clarification,
                entities=entities,
                reasoning=f"Archetype D: missing parameters — {', '.join(missing)}.",
                missing_params=missing,
            )
            self._attach_labels(result)
            return result

        # -----------------------------------------------------------------
        # Archetype A — single-fact lookup
        #   Requires an explicit product/SKU anchor AND a timeframe AND a metric.
        #   Must not have open-ended comparative or diagnostic signals.
        #   SKU presence is a strong signal for A regardless of 'last week' etc.
        # -----------------------------------------------------------------
        has_sku_anchor  = bool(entities["sku"])
        has_dim_anchor  = bool(entities["category"]) or bool(entities["brand"])
        has_time        = bool(entities["timeframe"])
        has_metric      = bool(entities["metric"])
        has_comparative = any(h in lower for h in self._COMPARATIVE_HINTS)
        has_diagnostic  = any(h in lower for h in self._DIAGNOSTIC_HINTS)

        # Strong A: SKU explicitly present with a timeframe → always a point lookup
        if has_sku_anchor and has_time and not (
            any(h in lower for h in ("compare", "vs", "versus", "growth", "rank", "highest", "lowest"))
        ):
            result = PlanResult(
                intent=IntentArchetype.LOOKUP,
                needs_clarification=False,
                clarification_prompt=None,
                entities=entities,
                reasoning=(
                    "Archetype A: explicit SKU anchor with timeframe — single-fact retrieval."
                ),
            )
            self._attach_labels(result)
            return result

        # Standard A: dimension + time + metric, no comparative/diagnostic signals
        if has_dim_anchor and has_time and has_metric and not has_comparative and not has_diagnostic:
            result = PlanResult(
                intent=IntentArchetype.LOOKUP,
                needs_clarification=False,
                clarification_prompt=None,
                entities=entities,
                reasoning=(
                    "Archetype A: dimension anchor, timeframe, and metric present "
                    "with no comparative or diagnostic signals."
                ),
            )
            self._attach_labels(result)
            return result

        # -----------------------------------------------------------------
        # Archetype B — comparative analysis
        # -----------------------------------------------------------------
        if has_comparative:
            result = PlanResult(
                intent=IntentArchetype.COMPARATIVE,
                needs_clarification=False,
                clarification_prompt=None,
                entities=entities,
                reasoning=(
                    "Archetype B: comparative keyword detected — requires time-series "
                    "manipulation, window functions, or multi-entity delta calculations."
                ),
            )
            self._attach_labels(result)
            return result

        # -----------------------------------------------------------------
        # Archetype C — diagnostic & pivot
        # -----------------------------------------------------------------
        if has_diagnostic:
            result = PlanResult(
                intent=IntentArchetype.DIAGNOSTIC,
                needs_clarification=False,
                clarification_prompt=None,
                entities=entities,
                reasoning=(
                    "Archetype C: diagnostic keyword detected — requires rankings, joins, "
                    "or multi-entity pivot operations across transactional boundaries."
                ),
            )
            self._attach_labels(result)
            return result

        # -----------------------------------------------------------------
        # Fallback — if a metric is present, treat as lookup; else ambiguous
        # -----------------------------------------------------------------
        if has_metric:
            result = PlanResult(
                intent=IntentArchetype.LOOKUP,
                needs_clarification=False,
                clarification_prompt=None,
                entities=entities,
                reasoning="Archetype A (fallback): metric present, defaulting to descriptive lookup.",
            )
            self._attach_labels(result)
            return result

        missing = self._identify_missing_params(lower, entities)
        clarification = self._build_clarification_prompt(lower, missing)
        result = PlanResult(
            intent=IntentArchetype.AMBIGUOUS,
            needs_clarification=True,
            clarification_prompt=clarification,
            entities=entities,
            reasoning=f"Archetype D (fallback): insufficient parameters — {', '.join(missing)}.",
            missing_params=missing,
        )
        self._attach_labels(result)
        return result

    # -----------------------------------------------------------------------
    # Label helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _attach_labels(result: PlanResult) -> None:
        result.archetype_label       = ARCHETYPE_LABELS[result.intent]
        result.archetype_description = ARCHETYPE_DESCRIPTIONS[result.intent]

    # -----------------------------------------------------------------------
    # Entity extractors
    # -----------------------------------------------------------------------

    @staticmethod
    def _extract_sku(text: str) -> str | None:
        match = re.search(r"\b([A-Z]{2}-\d{3})\b", text.upper())
        return match.group(1) if match else None

    @staticmethod
    def _extract_timeframe(text: str) -> str | None:
        if re.search(r"20\d{2}", text):
            return "year_detected"
        if "last week" in text or "this week" in text:
            return "recent_week"
        if "last month" in text or "this month" in text:
            return "recent_month"
        if "last year" in text or "this year" in text:
            return "recent_year"
        return None

    @staticmethod
    def _extract_metric(text: str) -> str | None:
        if any(k in text for k in ("revenue", "sales", "units")):
            return "units_or_revenue"
        if any(k in text for k in ("stock", "depletion", "inventory")):
            return "stock"
        if any(k in text for k in ("promotion", "promo", "uplift", "campaign")):
            return "promotion"
        if "delivery" in text:
            return "delivery"
        return None

    # -----------------------------------------------------------------------
    # Ambiguity detection
    # -----------------------------------------------------------------------

    @staticmethod
    def _is_ambiguous(text: str, entities: dict[str, str | None]) -> bool:
        # Vague openers with no grounding metric
        vague_openers = ("how did", "how is", "tell me about", "what about")
        has_grounding_keyword = any(t in text for t in (
            "revenue", "sales", "units", "stock", "depletion",
            "inventory", "brand", "category", "channel",
        ))
        if any(v in text for v in vague_openers) and not has_grounding_keyword:
            return True

        # "promotion" mentioned but no concrete metric, timeframe, or product scoping
        is_promo_metric_only = (entities["metric"] == "promotion" and not any(k in text for k in ("revenue", "sales", "units", "stock", "depletion", "inventory")))
        
        if (
            any(t in text for t in ("promotion", "promo", "campaign"))
            and (not entities["metric"] or is_promo_metric_only)
            and not entities["timeframe"]
            and not entities["sku"]
            and not any(cue in text for cue in (
                "compare", "versus", "growth", "highest", "lowest",
                "which", "by", "across", "breakdown", "split",
            ))
        ):
            return True

        return False

    @staticmethod
    def _identify_missing_params(text: str, entities: dict[str, str | None]) -> list[str]:
        missing = []
        has_metric = any(k in text for k in (
            "revenue", "sales", "units", "stock", "depletion", "inventory", "promo",
        ))
        if not has_metric and not entities["metric"]:
            missing.append("metric (e.g. revenue, units sold, stock depletion)")
        if not entities["timeframe"]:
            missing.append("timeframe (e.g. last week, March 2024, 2023)")
        has_product_scope = (
            entities["sku"] or entities["category"]
            or entities["brand"] or entities["channel"]
        )
        if not has_product_scope:
            missing.append("product scope (e.g. SKU, brand, or category)")
        return missing

    @staticmethod
    def _build_clarification_prompt(text: str, missing: list[str]) -> str:
        if any(t in text for t in ("promotion", "promo", "campaign")):
            return (
                "Your promotion question needs more detail. Would you like to see:\n"
                "• Revenue impact — total revenue during vs. outside promotion?\n"
                "• Inventory depletion — stock reduction rate during the campaign?\n"
                "• Regional sales comparison — which region responded best?\n\n"
                "Please re-state with: metric, timeframe, and product/category scope."
            )
        if missing:
            items = "\n".join(f"  • {p}" for p in missing)
            return (
                f"Your question is missing key parameters:\n{items}\n\n"
                "Example: \"What was the total revenue for the Yogurt category in 2024?\""
            )
        return (
            "Please specify: (1) metric — revenue, units sold, or stock depletion; "
            "(2) timeframe — e.g. last week, March 2024; "
            "(3) product scope — SKU, brand, or category."
        )

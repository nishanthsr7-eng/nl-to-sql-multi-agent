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

from semantic_query_engine.domain.registry import (
    DimensionRegistry,
    IdentifierRegistry,
    LanguageProfile,
    load_domain_registries,
)


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
        "Single-fact retrieval — explicit entity matching across the domain's "
        "dimensions and time attributes to extract a standalone data point."
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
        "Insufficient parameters detected — metric, timeframe or entity scope is missing. "
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
    """Rule-based intent classification over the domain's own vocabulary.

    The keyword tuples that used to sit here -- comparative hints, diagnostic
    hints, metric words, the FMCG clarification menu -- now come from the
    ``language`` block of the active domain's semantic layer through
    :class:`LanguageProfile`. They were the single largest reason a second
    domain could not be added without touching Python, which is the claim
    Phase 4 task 3 exists to make checkable.
    """

    def __init__(
        self,
        dimensions: DimensionRegistry | None = None,
        identifiers: IdentifierRegistry | None = None,
        language: LanguageProfile | None = None,
    ):
        registries = load_domain_registries()
        self.dimensions = dimensions or registries.dimensions
        self.identifiers = identifiers or registries.identifiers
        self.language = language or registries.language
        self._comparative = self.language.hints("comparative_hints")
        self._diagnostic = self.language.hints("diagnostic_hints")
        self._vague_openers = self.language.hints("vague_openers")
        self._grounding = self.language.hints("grounding_keywords")
        self._scoping_cues = self.language.hints("scoping_cues")
        self._strong_comparatives = self.language.hints("strong_comparative_hints")
        self._metric_keywords = self.language.metric_keywords()
        self._anchor_dimensions = self.language.anchor_dimensions()
        self._scope_dimensions = self.language.scope_dimensions()
        self.topic_prompts = self.language.topic_prompts()

    def run(self, question: str) -> PlanResult:
        q     = question.strip()
        lower = q.lower()

        # One key per declared identifier and per declared dimension, so the
        # extracted entity set is whatever the domain says exists rather than
        # the eight things the FMCG warehouse happened to have.
        entities: dict[str, str | None] = {
            name: self.identifiers.find(name, q) for name in self.identifiers.names()
        }
        for dimension in self.dimensions.dimensions():
            entities[dimension] = self.dimensions.match(dimension, lower)
        entities["timeframe"] = self._extract_timeframe(lower)
        entities["metric"] = self._extract_metric(lower)

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
        has_sku_anchor  = any(entities.get(name) for name in self.identifiers.names())
        has_dim_anchor  = any(entities.get(dim) for dim in self._anchor_dimensions)
        has_time        = bool(entities["timeframe"])
        has_metric      = bool(entities["metric"])
        has_comparative = any(h in lower for h in self._comparative)
        has_diagnostic  = any(h in lower for h in self._diagnostic)

        # Strong A: an explicit identifier with a timeframe -> a point lookup
        if has_sku_anchor and has_time and not (
            any(h in lower for h in self._strong_comparatives)
        ):
            result = PlanResult(
                intent=IntentArchetype.LOOKUP,
                needs_clarification=False,
                clarification_prompt=None,
                entities=entities,
                reasoning=(
                    "Archetype A: explicit identifier anchor with timeframe — "
                    "single-fact retrieval."
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

    def _extract_sku(self, text: str) -> str | None:
        """Deprecated -- SKU extraction now goes through :class:`IdentifierRegistry`.

        The pattern itself lives once, in the semantic layer, so the planner, the
        fallback templates, and the validator all recognise the same identifiers.
        Prefer ``self.identifiers.find("sku", ...)`` directly.
        """
        return self.identifiers.find("sku", text)

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

    def _extract_metric(self, text: str) -> str | None:
        """Which metric family the question names, in the domain's own words.

        First match wins, and the declaration order in the semantic layer is the
        priority order -- so "promotion revenue" resolves to the revenue group
        rather than to the promotion group it also mentions.
        """
        for group, words in self._metric_keywords:
            if any(word in text for word in words):
                return group
        return None

    # -----------------------------------------------------------------------
    # Ambiguity detection
    # -----------------------------------------------------------------------

    def _is_ambiguous(self, text: str, entities: dict[str, str | None]) -> bool:
        # A vague opener with nothing in it that the warehouse measures.
        if any(v in text for v in self._vague_openers) and not any(
            t in text for t in self._grounding
        ):
            return True

        # A domain topic named with no metric, no timeframe, no identifier and no
        # cue that the question is about to scope itself ("by region", "compare
        # ...") -- e.g. a bare "how are promotions doing". The topic's own metric
        # group does not count as a metric here: naming the topic is what made
        # the question ambiguous in the first place.
        for topic in self.topic_prompts:
            if not topic.matches(text):
                continue
            metric = entities.get("metric")
            topic_is_own_metric = bool(metric) and str(metric) in topic.triggers
            if (
                (not metric or topic_is_own_metric)
                and not entities.get("timeframe")
                and not any(entities.get(name) for name in self.identifiers.names())
                and not any(cue in text for cue in self._scoping_cues)
            ):
                return True

        return False

    def _identify_missing_params(self, text: str, entities: dict[str, str | None]) -> list[str]:
        missing = []
        has_metric = any(word in text for _, words in self._metric_keywords for word in words)
        if not has_metric and not entities.get("metric"):
            missing.append(self.language.missing_param_prompt("metric"))
        if not entities.get("timeframe"):
            missing.append(self.language.missing_param_prompt("timeframe"))
        has_scope = any(entities.get(name) for name in self.identifiers.names()) or any(
            entities.get(dim) for dim in self._scope_dimensions
        )
        if not has_scope:
            missing.append(self.language.missing_param_prompt("scope"))
        return missing

    def _build_clarification_prompt(self, text: str, missing: list[str]) -> str:
        for topic in self.topic_prompts:
            if topic.matches(text) and topic.prompt:
                return topic.prompt
        if missing:
            items = "\n".join(f"  \u2022 {p}" for p in missing)
            return (
                f"Your question is missing key parameters:\n{items}\n\n"
                f"{self.language.clarification_example()}"
            )
        return "Please specify: " + "; ".join(
            self.language.missing_param_prompt(slot)
            for slot in ("metric", "timeframe", "scope")
        )

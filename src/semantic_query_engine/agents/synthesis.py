"""Synthesis agent -- turns a result DataFrame into a 5-layer structured response."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pandas as pd

from semantic_query_engine.core.config import PIPELINE, LLMSettings, load_llm_settings
from semantic_query_engine.core.llm_client import ChatClient, build_client
from semantic_query_engine.core.logging import get_logger
from semantic_query_engine.core.results import StructuredResponse
from semantic_query_engine.core.schemas import llm_retry, parse_synthesis_payload
from semantic_query_engine.core.usage import usage_from_response
from semantic_query_engine.prompts.synthesis import SYSTEM_PROMPT, build_user_prompt

logger = get_logger(__name__)

# StructuredResponse now lives in core.results alongside the Clarification and
# Failure variants it forms a union with. Re-exported here because this module is
# where it is produced, and callers import it from both places.
__all__ = ["StructuredResponse", "SynthesisAgent"]


# ---------------------------------------------------------------------------
# SynthesisAgent
# ---------------------------------------------------------------------------

class SynthesisAgent:

    def __init__(
        self,
        client_factory: Callable[[LLMSettings], ChatClient] = build_client,
        settings: LLMSettings | None = None,
    ):
        self._client_factory = client_factory
        # Resolved once, when the agent is built, rather than on every request.
        # Provider configuration is process-level: re-reading the environment per
        # question bought nothing and put an env scan on the hot path of a service
        # that is meant to answer concurrent requests.
        self._settings = settings or load_llm_settings()

    def run(
        self,
        question: str,
        sql: str,
        df: pd.DataFrame,
        intent: str,
        agent_trace: list[str],
        archetype_label: str = "",
        archetype_description: str = "",
        sql_source: str = "",
    ) -> StructuredResponse:

        if df.empty:
            return StructuredResponse(
                narrative_summary=(
                    "No rows matched the query filters. "
                    "The selected combination of entity, timeframe, or product scope returned zero records. "
                    "Try broadening the timeframe or including a wider product scope."
                ),
                key_metric=None,
                comparison_context=None,
                chart_recommendation="bar",
                sql_query=sql,
                result_table=[],
                intent=intent,
                archetype_label=archetype_label,
                archetype_description=archetype_description,
                agent_trace=agent_trace,
                sql_source=sql_source,
            )

        settings = self._settings
        if settings.is_enabled:
            try:
                return self._synthesise_with_llm(
                    question, sql, df, intent, agent_trace, archetype_label, archetype_description,
                    settings, sql_source,
                )
            except Exception as e:
                agent_trace.append(f"LLM Synthesis failed: {e}. Falling back to deterministic rules.")
                logger.warning("LLM synthesis failed (%s): %s", type(e).__name__, e)
        else:
            agent_trace.append("No LLM API key configured. Falling back to deterministic synthesis.")

        return self._synthesise_fallback(
            question, sql, df, intent, agent_trace, archetype_label, archetype_description, sql_source
        )

    # -----------------------------------------------------------------------
    # LLM Synthesis
    # -----------------------------------------------------------------------

    @llm_retry
    def _call_llm(self, settings, user_prompt: str):
        client = self._client_factory(settings)
        return client.chat.completions.create(
            model=settings.synthesizer_model,
            temperature=settings.temperature_synthesis,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )

    def _synthesise_with_llm(
        self, question: str, sql: str, df: pd.DataFrame, intent: str,
        agent_trace: list[str], archetype_label: str, archetype_description: str,
        settings, sql_source: str = "",
    ) -> StructuredResponse:
        user_prompt = build_user_prompt(
            question, archetype_label, sql, df, PIPELINE.synthesis_sample_rows
        )
        response = self._call_llm(settings, user_prompt)
        payload = parse_synthesis_payload(response.choices[0].message.content or "{}")

        agent_trace.append(
            f"Synthesis: LLM generated summary and recommended '{payload.chart_recommendation}' chart."
        )

        return StructuredResponse(
            narrative_summary=payload.narrative_summary,
            key_metric=payload.key_metric,
            comparison_context=payload.comparison_context,
            chart_recommendation=payload.chart_recommendation,
            sql_query=sql,
            # DataFrame columns here are always the SQL result's string aliases,
            # never non-string keys -- pandas-stubs types this generically as
            # dict[Hashable, Any] since a DataFrame can have non-string columns.
            result_table=cast(list[dict[str, Any]], df.to_dict(orient="records")),
            intent=intent,
            archetype_label=archetype_label,
            archetype_description=archetype_description,
            agent_trace=agent_trace,
            sql_source=sql_source,
            # This call's tokens only. The orchestrator replaces it with the
            # run's total, since a caller reading a result wants what the
            # question cost, not what the last agent in the chain cost.
            usage=usage_from_response(response, settings.synthesizer_model, settings.base_url),
        )

    # -----------------------------------------------------------------------
    # Deterministic Fallback Synthesis
    # -----------------------------------------------------------------------

    def _synthesise_fallback(
        self, question: str, sql: str, df: pd.DataFrame, intent: str,
        agent_trace: list[str], archetype_label: str, archetype_description: str,
        sql_source: str = "",
    ) -> StructuredResponse:

        q_lower = question.lower()
        numeric = list(df.select_dtypes(include="number").columns)
        dims    = [c for c in df.columns if c not in numeric]

        # Pick the best value column. Priority: depletion_rate > revenue > units > last numeric
        PRIORITY = ["stock_depletion_rate", "total_revenue", "wow_change",
                    "total_units", "total_units_sold", "units"]
        value_col = next((c for c in PRIORITY if c in numeric), None) or (numeric[-1] if numeric else df.columns[-1])

        # Build a meaningful label from all dimension columns. If there are multiple
        # dimensions (e.g. channel + promotion_flag), concatenate them so the label
        # is unique per row.
        def row_label(row) -> str:
            parts = [str(row[d]) for d in dims if d != "promotion_flag"]
            promo = row.get("promotion_flag", None)
            if promo is not None:
                parts.append("Promoted" if str(promo) in ("1", "1.0", "True") else "Non-promoted")
            return " / ".join(parts) if parts else str(row.iloc[0])

        def format_value(v: Any, col: str = "") -> str:
            if not isinstance(v, (int, float)):
                return str(v)
            if "rate" in col or "depletion" in col:
                return f"{v:.4f}"
            if "revenue" in col:
                if v >= 1_000_000:
                    return f"£{v / 1_000_000:.2f}M"
                return f"£{v:,.2f}"
            if v >= 1_000_000:
                return f"{v / 1_000_000:.2f}M"
            if v >= 1_000:
                return f"{v:,.0f}"
            return f"{v:.2f}"

        top     = df.iloc[0]
        top_lbl = row_label(top)

        if numeric and value_col in df.columns:
            top_val = float(top[value_col])
            top_fmt = format_value(top_val, value_col)

            key_metric = f"{top_fmt} -- {top_lbl}"

            # Narrative -- describe what the query found
            metric_label = value_col.replace("_", " ")
            if len(dims) >= 2:
                dim_label = f"{dims[0].replace('_',' ')} x {dims[1].replace('_',' ')}"
            elif dims:
                dim_label = dims[0].replace("_", " ")
            else:
                dim_label = "record"

            narrative = (
                f"The query returned **{len(df)} {dim_label}** combinations. "
                f"The top result is **{top_lbl}** with **{top_fmt}** in {metric_label}."
            )

            # Add a ranked top-3 sentence if multiple rows
            if len(df) >= 3:
                ranked = [
                    f"**#{i+1} {row_label(df.iloc[i])}** -- {format_value(float(df.iloc[i][value_col]), value_col)}"
                    for i in range(min(3, len(df)))
                ]
                narrative += " Rankings: " + " . ".join(ranked) + "."

            # Comparison context
            comparison = None
            if len(df) >= 2:
                second_lbl = row_label(df.iloc[1])
                second_val = float(df.iloc[1][value_col])
                delta = top_val - second_val
                pct   = (delta / second_val * 100) if second_val else 0
                delta_fmt = format_value(abs(delta), value_col)
                if top_lbl == second_lbl:
                    # Same label means same entity in different rows -- skip self-compare
                    comparison = None
                elif delta >= 0:
                    comparison = (
                        f"**{top_lbl}** leads **{second_lbl}** by {delta_fmt} ({abs(pct):.1f}% higher)."
                    )
                else:
                    comparison = (
                        f"**{top_lbl}** is {delta_fmt} ({abs(pct):.1f}%) below **{second_lbl}**."
                    )
        else:
            key_metric = str(top[df.columns[0]])
            narrative  = f"The query returned: **{key_metric}**."
            comparison = None

        # Chart recommendation based on result shape
        if any(t in q_lower for t in ("year", "month", "week", "trend", "over time", "yoy")):
            chart = "line"
        elif "promotion" in q_lower and "promotion_flag" in df.columns:
            chart = "bar"
        elif len(dims) >= 2 and len(df) <= 30:
            chart = "grouped_bar"
        elif len(df) == 1:
            chart = "bar"
        else:
            chart = "bar"

        return StructuredResponse(
            narrative_summary=narrative,
            key_metric=key_metric,
            comparison_context=comparison,
            chart_recommendation=chart,
            sql_query=sql,
            # DataFrame columns here are always the SQL result's string aliases,
            # never non-string keys -- pandas-stubs types this generically as
            # dict[Hashable, Any] since a DataFrame can have non-string columns.
            result_table=cast(list[dict[str, Any]], df.to_dict(orient="records")),
            intent=intent,
            archetype_label=archetype_label,
            archetype_description=archetype_description,
            agent_trace=agent_trace,
            sql_source=sql_source,
        )

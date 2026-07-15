"""SQL generator agent -- LLM-backed generation with a deterministic fallback.

When an LLM provider is configured, generation and repair go through
:mod:`semantic_query_engine.prompts.sql_generation`. When it is not configured (or the call
fails), :meth:`SQLGeneratorAgent._generate_fallback` matches the question against
a declarative :data:`TEMPLATES` registry covering the FMCG catalog's known query
archetypes. The fallback exists so the app is fully demoable without an API key,
and so a provider outage degrades the product instead of breaking it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from semantic_query_engine.core.config import LLMSettings, load_llm_settings
from semantic_query_engine.core.llm_client import ChatClient, build_client
from semantic_query_engine.core.logging import get_logger
from semantic_query_engine.core.schemas import llm_retry, parse_sql_payload
from semantic_query_engine.domain.registry import MetricRegistry, load_domain_registries
from semantic_query_engine.prompts.sql_generation import (
    REPAIR_ROLE_SUFFIX,
    SYSTEM_ROLE,
    build_repair_prompt,
    build_user_prompt,
)

logger = get_logger(__name__)


@dataclass
class SQLGenerationResult:
    sql: str
    source: str
    prompt_used: str | None = None
    # Bound-parameter values for any ``?`` placeholders in ``sql``. Only the
    # deterministic fallback templates use these -- LLM-generated SQL always
    # carries literal values inline, since the model has no binding channel.
    params: list[Any] = field(default_factory=list)


class SQLGeneratorAgent:
    FORBIDDEN = ("DROP", "DELETE", "INSERT", "UPDATE", "ALTER", "CREATE", "TRUNCATE")

    def __init__(
        self,
        metric_registry: MetricRegistry | None = None,
        client_factory: Callable[[LLMSettings], ChatClient] = build_client,
    ):
        self.metrics = metric_registry or load_domain_registries()[0]
        self._client_factory = client_factory

    def run(
        self,
        question: str,
        schema_context: str,
        intent: str,
        metrics: list[dict] | None = None,
    ) -> SQLGenerationResult:
        settings = load_llm_settings()
        if settings.is_enabled:
            try:
                return self._generate_with_llm(question, schema_context, intent, settings, metrics or [])
            except Exception as e:
                logger.warning("LLM SQL generation failed (%s): %s. Using deterministic fallback.", type(e).__name__, e)
        return self._generate_fallback(question)

    def repair(
        self,
        question: str,
        schema_context: str,
        intent: str,
        failed_sql: str,
        validation_errors: list[str],
        metrics: list[dict] | None = None,
    ) -> SQLGenerationResult:
        """Produce one corrected SQL candidate from explicit validator feedback.

        Without an LLM, there is nothing that can act on ``validation_errors``:
        ``_generate_fallback`` is a pure function of ``question`` alone, so calling
        it again here would silently reproduce the exact SQL that just failed --
        burning a repair attempt for nothing (see CHANGELOG.md). Report that
        explicitly via ``source`` instead of pretending a repair happened, so the
        orchestrator can stop retrying rather than loop on an identical query.
        """
        settings = load_llm_settings()
        if settings.is_enabled:
            try:
                return self._repair_with_llm(
                    question, schema_context, intent, failed_sql, validation_errors, settings, metrics or []
                )
            except Exception as e:
                logger.warning("LLM SQL repair failed (%s): %s. Using deterministic fallback.", type(e).__name__, e)
        result = self._generate_fallback(question)
        if result.sql == failed_sql:
            logger.info(
                "No LLM available to repair; the deterministic fallback reproduced the same SQL "
                "that failed validation. Reporting as unrepairable rather than retrying it."
            )
            return SQLGenerationResult(sql=result.sql, source="fallback_unrepairable", params=result.params)
        return SQLGenerationResult(sql=result.sql, source="fallback_repair", params=result.params)

    @llm_retry
    def _generate_with_llm(
        self,
        question: str,
        schema_context: str,
        intent: str,
        settings,
        metrics: list[dict],
    ) -> SQLGenerationResult:
        client = self._client_factory(settings)
        user_prompt = build_user_prompt(question, schema_context, intent, metrics)

        response = client.chat.completions.create(
            model=settings.generator_model,
            temperature=settings.temperature_generation,  # Precision task -- minimise variance
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_ROLE},
                {"role": "user", "content": user_prompt},
            ],
        )
        payload = parse_sql_payload(response.choices[0].message.content or "{}")
        sql = self._clean_sql(payload.sql)
        return SQLGenerationResult(sql=sql, source="llm", prompt_used=user_prompt)

    @llm_retry
    def _repair_with_llm(
        self,
        question: str,
        schema_context: str,
        intent: str,
        failed_sql: str,
        validation_errors: list[str],
        settings,
        metrics: list[dict],
    ) -> SQLGenerationResult:
        client = self._client_factory(settings)
        full_prompt = build_repair_prompt(
            question, schema_context, intent, failed_sql, validation_errors, metrics
        )

        response = client.chat.completions.create(
            model=settings.generator_model,
            temperature=settings.temperature_generation,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_ROLE + REPAIR_ROLE_SUFFIX},
                {"role": "user", "content": full_prompt},
            ],
        )
        payload = parse_sql_payload(response.choices[0].message.content or "{}")
        return SQLGenerationResult(
            sql=self._clean_sql(payload.sql),
            source="llm_repair",
            prompt_used=full_prompt,
        )

    # -----------------------------------------------------------------------
    # Deterministic fallback: a declarative template registry (no LLM / API key)
    # -----------------------------------------------------------------------

    def _generate_fallback(self, question: str) -> SQLGenerationResult:
        lower = question.lower()
        ctx = _FallbackContext(question=question, lower=lower, metrics=self.metrics)
        for template in TEMPLATES:
            if template.matches(ctx):
                return template.render(ctx)
        return _render_generic(ctx)

    def _clean_sql(self, sql: str) -> str:
        sql = sql.strip().strip("`").strip()
        if sql.lower().startswith("```"):
            sql = re.sub(r"^```(?:sql)?", "", sql, flags=re.IGNORECASE).strip()
            sql = sql.rstrip("`").strip()
        return sql.rstrip(";")


# ---------------------------------------------------------------------------
# Template registry
#
# Each template is an independently readable, independently testable unit:
# a `matches` predicate over the question and a `render` function that returns
# a SQLGenerationResult. Priority is list order -- the first match wins.
# ---------------------------------------------------------------------------


@dataclass
class _FallbackContext:
    question: str
    lower: str
    metrics: MetricRegistry


@dataclass
class QueryTemplate:
    name: str
    matches: Callable[[_FallbackContext], bool]
    render: Callable[[_FallbackContext], SQLGenerationResult]


def _revenue_expr(metrics: MetricRegistry) -> str:
    return f"ROUND({metrics.get('total_revenue').formula}, 2)"


def _stock_expr(metrics: MetricRegistry) -> str:
    return f"ROUND({metrics.get('stock_depletion_rate').formula}, 4)"


def _detect_group_by(lower: str) -> str | None:
    """Return primary GROUP BY column from question keywords."""
    if "brand" in lower:
        return "brand"
    if "category" in lower:
        return "category"
    if "channel" in lower:
        return "channel"
    if "pack" in lower or "packaging" in lower:
        return "pack_type"
    if "segment" in lower:
        return "segment"
    if "sku" in lower:
        return "sku"
    if "region" in lower:
        return "region"
    for r in ("pl-north", "pl-south", "pl-central", "north", "south", "central"):
        if r in lower:
            return "region"
    return None


def _year_where(lower: str) -> str:
    """Return a safe filter for a year selected in the UI or typed by the user."""
    match = re.search(r"\b(20\d{2})\b", lower)
    if not match:
        return ""
    year = match.group(1)
    return f" WHERE CAST(date AS DATE) BETWEEN '{year}-01-01' AND '{year}-12-31'"


def _detect_secondary_dim(lower: str, primary: str) -> str | None:
    """Return a second dimension to cross-tab when the question hints at it."""
    order = ["region", "channel", "brand", "category", "pack_type"]
    for dim in order:
        if dim != primary and dim in lower:
            return dim
    return None


def _detect_metric_expr(lower: str, metrics: MetricRegistry) -> tuple[str, str]:
    """Return (SQL expression, alias) for the primary metric, using the certified formula."""
    if "revenue" in lower:
        return _revenue_expr(metrics), "total_revenue"
    if "depletion" in lower or "stock" in lower:
        return _stock_expr(metrics), "stock_depletion_rate"
    return "SUM(units_sold)", "total_units"


# -- Template predicates -----------------------------------------------------


def _is_lifecycle(ctx: _FallbackContext) -> bool:
    return "lifecycle" in ctx.lower


def _is_sku_lookup(ctx: _FallbackContext) -> bool:
    return bool(re.search(r"\bmi-006\b", ctx.lower) or re.search(r"\b[A-Z]{2}-\d{3}\b", ctx.question))


def _is_yoy(ctx: _FallbackContext) -> bool:
    return any(t in ctx.lower for t in ("year over year", "yoy", "annual growth", "yearly"))


def _is_promo(ctx: _FallbackContext) -> bool:
    return any(t in ctx.lower for t in ("promotion", "promo", "uplift"))


def _is_stock(ctx: _FallbackContext) -> bool:
    return any(t in ctx.lower for t in ("stock", "depletion", "inventory"))


def _is_wow(ctx: _FallbackContext) -> bool:
    return any(t in ctx.lower for t in ("week-on-week", "wow", "week over week"))


def _is_trend(ctx: _FallbackContext) -> bool:
    return any(t in ctx.lower for t in ("growth", "trend", "monthly", "by month", "month over month"))


def _is_category_channel(ctx: _FallbackContext) -> bool:
    return "category" in ctx.lower and "channel" in ctx.lower


def _is_pack_category(ctx: _FallbackContext) -> bool:
    return any(t in ctx.lower for t in ("pack", "packaging")) and "category" in ctx.lower


def _is_brand_channel(ctx: _FallbackContext) -> bool:
    return "brand" in ctx.lower and "channel" in ctx.lower


def _has_dim(name: str) -> Callable[[_FallbackContext], bool]:
    return lambda ctx: name in ctx.lower


def _has_pack(ctx: _FallbackContext) -> bool:
    return any(t in ctx.lower for t in ("pack", "packaging"))


def _mentions_revenue(ctx: _FallbackContext) -> bool:
    return "revenue" in ctx.lower


# -- Template renderers -------------------------------------------------------


def _render_lifecycle(ctx: _FallbackContext) -> SQLGenerationResult:
    # weekly_modeling_data is schema-complete but was previously never queried by any
    # fallback template, few-shot example, or gold case -- see ARCHITECTURE_REVIEW.md §12.
    sql = (
        "SELECT lifecycle_stage, ROUND(AVG(units_sold), 2) AS avg_units_sold "
        "FROM weekly_modeling_data "
        "GROUP BY lifecycle_stage ORDER BY avg_units_sold DESC"
    )
    return SQLGenerationResult(sql=sql, source="fallback")


def _render_sku_lookup(ctx: _FallbackContext) -> SQLGenerationResult:
    sku_match = re.search(r"\b([A-Z]{2}-\d{3})\b", ctx.question)
    sku = sku_match.group(1) if sku_match else "MI-006"

    filters = ["sku = ?"]
    params: list[Any] = [sku]

    region_map = {
        "pl-north": "PL-North", "north": "PL-North",
        "pl-south": "PL-South", "south": "PL-South",
        "pl-central": "PL-Central", "central": "PL-Central",
    }
    for key, val in region_map.items():
        if key in ctx.lower:
            filters.append("region = ?")
            params.append(val)
            break

    if "last week of january 2024" in ctx.lower:
        filters.append("date BETWEEN ? AND ?")
        params.extend(["2024-01-22", "2024-01-28"])
    elif re.search(r"january|jan", ctx.lower) and "2024" in ctx.lower:
        filters.append("date BETWEEN ? AND ?")
        params.extend(["2024-01-01", "2024-01-31"])

    sql = (
        "SELECT region, channel, SUM(units_sold) AS total_units, "
        f"{_revenue_expr(ctx.metrics)} AS total_revenue "
        f"FROM fmcg_sales WHERE {' AND '.join(filters)} "
        "GROUP BY region, channel ORDER BY total_units DESC"
    )
    return SQLGenerationResult(sql=sql, source="fallback", params=params)


def _render_yoy(ctx: _FallbackContext) -> SQLGenerationResult:
    sql = (
        f"SELECT year, {_revenue_expr(ctx.metrics)} AS total_revenue, "
        "SUM(units_sold) AS total_units "
        "FROM (SELECT EXTRACT(YEAR FROM CAST(date AS DATE)) AS year, "
        "units_sold, price_unit FROM fmcg_sales) t "
        "GROUP BY year ORDER BY year"
    )
    return SQLGenerationResult(sql=sql, source="fallback")


def _render_promo(ctx: _FallbackContext) -> SQLGenerationResult:
    group_by = _detect_group_by(ctx.lower)
    year_where = _year_where(ctx.lower)
    gb_select = f"{group_by}, " if group_by else ""
    gb_clause = f"GROUP BY {group_by}, promotion_flag " if group_by else "GROUP BY promotion_flag "
    ob_clause = f"ORDER BY {group_by}, promotion_flag" if group_by else "ORDER BY promotion_flag"
    sql = (
        f"SELECT {gb_select}promotion_flag, "
        "SUM(units_sold) AS total_units, "
        f"{_revenue_expr(ctx.metrics)} AS total_revenue "
        f"FROM fmcg_sales{year_where} {gb_clause}"
        f"{ob_clause}"
    )
    return SQLGenerationResult(sql=sql, source="fallback")


def _render_stock(ctx: _FallbackContext) -> SQLGenerationResult:
    group_by = _detect_group_by(ctx.lower)
    year_where = _year_where(ctx.lower)
    gb_select = f"{group_by}, " if group_by else ""
    gb_clause = f"GROUP BY {group_by} " if group_by else ""
    sql = (
        f"SELECT {gb_select}{_stock_expr(ctx.metrics)} AS stock_depletion_rate, "
        "SUM(stock_available) AS total_stock, "
        "SUM(units_sold) AS total_units_sold "
        f"FROM fmcg_sales{year_where} {gb_clause}"
        "ORDER BY stock_depletion_rate DESC"
    )
    return SQLGenerationResult(sql=sql, source="fallback")


def _render_wow(ctx: _FallbackContext) -> SQLGenerationResult:
    group_by = _detect_group_by(ctx.lower)
    date_filter = "date BETWEEN '2024-01-01' AND '2024-12-31'"
    if re.search(r"march|mar", ctx.lower):
        date_filter = "date BETWEEN '2024-03-01' AND '2024-03-31'"
    elif re.search(r"january|jan", ctx.lower):
        date_filter = "date BETWEEN '2024-01-01' AND '2024-01-31'"

    gb_select = f"{group_by}, " if group_by else ""
    part_clause = f"PARTITION BY {group_by} " if group_by else ""
    gb_clause = f"{group_by}, " if group_by else ""

    sql = (
        "WITH weekly AS ("
        f"SELECT {gb_select}DATE_TRUNC('week', CAST(date AS DATE)) AS week_start, "
        "SUM(units_sold) AS units "
        f"FROM fmcg_sales WHERE {date_filter} "
        f"GROUP BY {gb_clause}DATE_TRUNC('week', CAST(date AS DATE))"
        "), growth AS ("
        f"SELECT {gb_select}week_start, units, "
        f"LAG(units) OVER ({part_clause}ORDER BY week_start) AS prev_units "
        "FROM weekly"
        f") SELECT {gb_select}week_start, units, prev_units, "
        "(units - prev_units) AS wow_change "
        "FROM growth WHERE prev_units IS NOT NULL "
        "ORDER BY wow_change DESC LIMIT 10"
    )
    return SQLGenerationResult(sql=sql, source="fallback")


def _render_trend(ctx: _FallbackContext) -> SQLGenerationResult:
    group_by = _detect_group_by(ctx.lower)
    year_where = _year_where(ctx.lower)
    gb_select = f"{group_by}, " if group_by else ""
    gb_clause = f"{group_by}, " if group_by else ""
    sql = (
        f"SELECT {gb_select}DATE_TRUNC('month', CAST(date AS DATE)) AS month_start, "
        f"{_revenue_expr(ctx.metrics)} AS total_revenue, "
        "SUM(units_sold) AS total_units "
        f"FROM fmcg_sales{year_where} GROUP BY {gb_clause}DATE_TRUNC('month', CAST(date AS DATE)) "
        f"ORDER BY month_start{', ' + group_by if group_by else ''}"
    )
    return SQLGenerationResult(sql=sql, source="fallback")


def _render_cross_tab(dim_a: str, dim_b: str) -> Callable[[_FallbackContext], SQLGenerationResult]:
    def render(ctx: _FallbackContext) -> SQLGenerationResult:
        year_where = _year_where(ctx.lower)
        metric_col, metric_alias = _detect_metric_expr(ctx.lower, ctx.metrics)
        sql = (
            f"SELECT {dim_a}, {dim_b}, {metric_col} AS {metric_alias} "
            f"FROM fmcg_sales{year_where} "
            f"GROUP BY {dim_a}, {dim_b} ORDER BY {dim_a}, {metric_alias} DESC"
        )
        return SQLGenerationResult(sql=sql, source="fallback")

    return render


def _render_single_dim(dim: str, limit: int | None = None) -> Callable[[_FallbackContext], SQLGenerationResult]:
    def render(ctx: _FallbackContext) -> SQLGenerationResult:
        year_where = _year_where(ctx.lower) if dim not in ("pack_type", "segment") else ""
        metric_col, metric_alias = _detect_metric_expr(ctx.lower, ctx.metrics)
        limit_clause = f" LIMIT {limit}" if limit else ""
        sql = (
            f"SELECT {dim}, {metric_col} AS {metric_alias} "
            f"FROM fmcg_sales{year_where} "
            f"GROUP BY {dim} ORDER BY {metric_alias} DESC{limit_clause}"
        )
        return SQLGenerationResult(sql=sql, source="fallback")

    return render


def _render_revenue(ctx: _FallbackContext) -> SQLGenerationResult:
    group_by = _detect_group_by(ctx.lower)
    year_where = _year_where(ctx.lower)
    if not group_by:
        sql = f"SELECT {_revenue_expr(ctx.metrics)} AS total_revenue FROM fmcg_sales{year_where}"
        return SQLGenerationResult(sql=sql, source="fallback")
    secondary = _detect_secondary_dim(ctx.lower, group_by)
    group_cols = f"{group_by}, {secondary}" if secondary else group_by
    sql = (
        f"SELECT {group_cols}, {_revenue_expr(ctx.metrics)} AS total_revenue "
        f"FROM fmcg_sales{year_where} GROUP BY {group_cols} ORDER BY total_revenue DESC LIMIT 20"
    )
    return SQLGenerationResult(sql=sql, source="fallback")


def _render_generic(ctx: _FallbackContext) -> SQLGenerationResult:
    group_by = _detect_group_by(ctx.lower)
    year_where = _year_where(ctx.lower)
    metric_col, metric_alias = _detect_metric_expr(ctx.lower, ctx.metrics)
    if not group_by:
        sql = f"SELECT {metric_col} AS {metric_alias} FROM fmcg_sales{year_where}"
        return SQLGenerationResult(sql=sql, source="fallback")
    secondary = _detect_secondary_dim(ctx.lower, group_by)
    group_cols = f"{group_by}, {secondary}" if secondary else group_by
    sql = (
        f"SELECT {group_cols}, {metric_col} AS {metric_alias} "
        f"FROM fmcg_sales{year_where} GROUP BY {group_cols} ORDER BY {metric_alias} DESC LIMIT 20"
    )
    return SQLGenerationResult(sql=sql, source="fallback")


# Priority order == list order. The first matching template wins.
TEMPLATES: list[QueryTemplate] = [
    QueryTemplate("lifecycle_stage", matches=_is_lifecycle, render=_render_lifecycle),
    QueryTemplate("sku_lookup", matches=_is_sku_lookup, render=_render_sku_lookup),
    QueryTemplate("year_over_year", matches=_is_yoy, render=_render_yoy),
    QueryTemplate("promotion", matches=_is_promo, render=_render_promo),
    QueryTemplate("stock_depletion", matches=_is_stock, render=_render_stock),
    QueryTemplate("week_on_week", matches=_is_wow, render=_render_wow),
    QueryTemplate("monthly_trend", matches=_is_trend, render=_render_trend),
    QueryTemplate("category_x_channel", matches=_is_category_channel, render=_render_cross_tab("category", "channel")),
    QueryTemplate("pack_x_category", matches=_is_pack_category, render=_render_cross_tab("pack_type", "category")),
    QueryTemplate("brand_x_channel", matches=_is_brand_channel, render=_render_cross_tab("brand", "channel")),
    QueryTemplate("by_brand", matches=_has_dim("brand"), render=_render_single_dim("brand", limit=14)),
    QueryTemplate("by_category", matches=_has_dim("category"), render=_render_single_dim("category")),
    QueryTemplate("by_channel", matches=_has_dim("channel"), render=_render_single_dim("channel")),
    QueryTemplate("by_pack_type", matches=_has_pack, render=_render_single_dim("pack_type")),
    QueryTemplate("by_segment", matches=_has_dim("segment"), render=_render_single_dim("segment", limit=13)),
    QueryTemplate("revenue_smart_group", matches=_mentions_revenue, render=_render_revenue),
]

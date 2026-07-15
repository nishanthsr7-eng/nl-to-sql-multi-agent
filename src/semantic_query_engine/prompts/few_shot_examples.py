"""Curated few-shot (question -> DuckDB SQL) pairs used by the SQL generator prompt.

Each pair is a verified example covering one query archetype found in the FMCG
dataset: point lookups, single- and cross-dimension aggregation, window-function
deltas, certified-metric formulas, and time-bucketed trends. These are injected
into the LLM prompt verbatim — see semantic_query_engine.prompts.sql_generation.
"""

from __future__ import annotations

from typing import TypedDict


class FewShotExample(TypedDict):
    question: str
    sql: str


FEW_SHOT_EXAMPLES: list[FewShotExample] = [
    # --- 1. SKU-level point lookup ---
    {
        "question": "What were total units sold for SKU MI-006 in the PL-South region in the last week of January 2024?",
        "sql": (
            "SELECT region, channel, SUM(units_sold) AS total_units_sold\n"
            "FROM fmcg_sales\n"
            "WHERE sku = 'MI-006'\n"
            "  AND region = 'PL-South'\n"
            "  AND date BETWEEN '2024-01-22' AND '2024-01-28'\n"
            "GROUP BY region, channel\n"
            "ORDER BY total_units_sold DESC"
        ),
    },
    # --- 2. Revenue aggregated by a single dimension (brand) ---
    {
        "question": "Compare total revenue across all brands.",
        "sql": (
            "SELECT brand,\n"
            "       ROUND(SUM(units_sold * price_unit), 2) AS total_revenue\n"
            "FROM fmcg_sales\n"
            "GROUP BY brand\n"
            "ORDER BY total_revenue DESC"
        ),
    },
    # --- 3. Cross-dimensional aggregation (category × channel) ---
    {
        "question": "Show units sold by category across each sales channel.",
        "sql": (
            "SELECT category, channel,\n"
            "       SUM(units_sold) AS total_units\n"
            "FROM fmcg_sales\n"
            "GROUP BY category, channel\n"
            "ORDER BY category, total_units DESC"
        ),
    },
    # --- 4. Promotion split using CASE WHEN (certified metric) ---
    {
        "question": "Compare units sold during promotion versus non-promotion periods by channel.",
        "sql": (
            "SELECT channel, promotion_flag,\n"
            "       SUM(units_sold) AS total_units,\n"
            "       ROUND(SUM(units_sold * price_unit), 2) AS total_revenue\n"
            "FROM fmcg_sales\n"
            "GROUP BY channel, promotion_flag\n"
            "ORDER BY channel, promotion_flag"
        ),
    },
    # --- 5. Stock depletion rate (certified metric) ---
    {
        "question": "What is the stock depletion rate by category?",
        "sql": (
            "SELECT category,\n"
            "       ROUND(SUM(units_sold) * 1.0 / NULLIF(SUM(stock_available), 0), 4) AS stock_depletion_rate,\n"
            "       SUM(stock_available) AS total_stock,\n"
            "       SUM(units_sold) AS total_units_sold\n"
            "FROM fmcg_sales\n"
            "GROUP BY category\n"
            "ORDER BY stock_depletion_rate DESC"
        ),
    },
    # --- 6. Week-on-week growth with CTE + LAG window function ---
    {
        "question": "Which brand had the highest week-on-week growth in units sold in 2024?",
        "sql": (
            "WITH weekly AS (\n"
            "    SELECT brand,\n"
            "           DATE_TRUNC('week', CAST(date AS DATE)) AS week_start,\n"
            "           SUM(units_sold) AS units\n"
            "    FROM fmcg_sales\n"
            "    WHERE date BETWEEN '2024-01-01' AND '2024-12-31'\n"
            "    GROUP BY brand, DATE_TRUNC('week', CAST(date AS DATE))\n"
            "),\n"
            "growth AS (\n"
            "    SELECT brand, week_start, units,\n"
            "           LAG(units) OVER (PARTITION BY brand ORDER BY week_start) AS prev_units\n"
            "    FROM weekly\n"
            ")\n"
            "SELECT brand, week_start, units, prev_units,\n"
            "       (units - prev_units) AS wow_change\n"
            "FROM growth\n"
            "WHERE prev_units IS NOT NULL\n"
            "ORDER BY wow_change DESC\n"
            "LIMIT 10"
        ),
    },
    # --- 7. Year-over-year revenue trend ---
    {
        "question": "Show total revenue year over year.",
        "sql": (
            "SELECT EXTRACT(YEAR FROM CAST(date AS DATE)) AS year,\n"
            "       ROUND(SUM(units_sold * price_unit), 2) AS total_revenue,\n"
            "       SUM(units_sold) AS total_units\n"
            "FROM fmcg_sales\n"
            "GROUP BY year\n"
            "ORDER BY year"
        ),
    },
    # --- 8. Pack type × category cross-tab ---
    {
        "question": "Compare revenue by pack type across categories.",
        "sql": (
            "SELECT pack_type, category,\n"
            "       ROUND(SUM(units_sold * price_unit), 2) AS total_revenue\n"
            "FROM fmcg_sales\n"
            "GROUP BY pack_type, category\n"
            "ORDER BY pack_type, total_revenue DESC"
        ),
    },
    # --- 9. Month-over-month trend by channel ---
    {
        "question": "Show monthly revenue trend by channel.",
        "sql": (
            "SELECT channel,\n"
            "       DATE_TRUNC('month', CAST(date AS DATE)) AS month_start,\n"
            "       ROUND(SUM(units_sold * price_unit), 2) AS total_revenue\n"
            "FROM fmcg_sales\n"
            "GROUP BY channel, DATE_TRUNC('month', CAST(date AS DATE))\n"
            "ORDER BY month_start, total_revenue DESC"
        ),
    },
    # --- 10. Promotional uplift using the certified formula ---
    {
        "question": "Which category had the highest promotional uplift?",
        "sql": (
            "SELECT category,\n"
            "       SUM(CASE WHEN promotion_flag = 1 THEN units_sold ELSE 0 END) AS promo_units,\n"
            "       SUM(CASE WHEN promotion_flag = 0 THEN units_sold ELSE 0 END) AS non_promo_units,\n"
            "       ROUND(\n"
            "           (\n"
            "               SUM(CASE WHEN promotion_flag = 1 THEN units_sold ELSE 0 END)\n"
            "               - SUM(CASE WHEN promotion_flag = 0 THEN units_sold ELSE 0 END)\n"
            "           ) * 1.0\n"
            "           / NULLIF(SUM(CASE WHEN promotion_flag = 0 THEN units_sold ELSE 0 END), 0),\n"
            "           4\n"
            "       ) AS promotional_uplift_rate\n"
            "FROM fmcg_sales\n"
            "GROUP BY category\n"
            "ORDER BY promotional_uplift_rate DESC"
        ),
    },
    # --- 11. weekly_modeling_data -- product lifecycle analysis ---
    {
        "question": "What is the average units sold by lifecycle stage?",
        "sql": (
            "SELECT lifecycle_stage,\n"
            "       ROUND(AVG(units_sold), 2) AS avg_units_sold\n"
            "FROM weekly_modeling_data\n"
            "GROUP BY lifecycle_stage\n"
            "ORDER BY avg_units_sold DESC"
        ),
    },
]

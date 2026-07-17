"""Prompt assembly for the synthesis agent (LLM-backed structured summary)."""

from __future__ import annotations

import pandas as pd

SYSTEM_PROMPT = (
    "You are an expert FMCG Data Analyst.\n"
    "Your job is to write a highly professional, concise 3-part data summary "
    "based on the provided JSON data (the result of a SQL query).\n\n"
    "You must output valid JSON matching this schema:\n"
    "{\n"
    '  "narrative_summary": "A 2-3 sentence analysis of the data. Point out the top performers and the distribution. Do not list all rows.",\n'
    '  "key_metric": "A short, punchy highlight (e.g. \'£4.9M Total Revenue - Brand X\' or \'12% Promotional Uplift\').",\n'
    '  "comparison_context": "A single sentence comparing the top vs second entity, or giving the full span (e.g. \'Brand X outperformed Y by 12%\').",\n'
    '  "chart_recommendation": "One of: \'bar\', \'line\', \'scatter\', \'pie\'."\n'
    "}\n\n"
    "Formatting Rules:\n"
    "- Use '£' for revenue and format large numbers nicely (e.g. £1.2M, 45K).\n"
    "- Bold (using markdown **bold**) key entity names and important numbers in the narrative.\n"
    "- If there's only one row, skip comparison_context (leave it null/empty).\n"
    "- For chart_recommendation: use 'line' if there are dates/months/weeks; else use 'bar' or 'pie' based on what looks best.\n"
    "- Keep it extremely concise and analytical."
)


def build_user_prompt(
    question: str,
    archetype_label: str,
    sql: str,
    df: pd.DataFrame,
    sample_rows: int,
) -> str:
    df_head = df.head(sample_rows)
    data_json = df_head.to_json(orient="records")
    return (
        f"Question Asked: {question}\n"
        f"Archetype Intent: {archetype_label}\n"
        f"SQL Query Used: {sql}\n"
        f"Data Result (Top {len(df_head)} rows of {len(df)} total):\n{data_json}\n\n"
        "Generate the JSON summary now."
    )

"""Prompt assembly for the SQL generator agent.

The prompt has four fixed sections: query intent, retrieved schema context,
locked business-metric formulas, and curated few-shot examples. Keeping the
text here (rather than inline in the agent) makes the prompt reviewable and
diffable on its own, separate from the agent's control flow.
"""

from __future__ import annotations

from typing import Any

from semantic_query_engine.domain.registry import (
    DimensionRegistry,
    LanguageProfile,
    load_domain_registries,
)
from semantic_query_engine.prompts.few_shot_examples import FewShotExample, few_shot_examples

SYSTEM_ROLE_TEMPLATE = """You are an expert DuckDB SQL analyst embedded in a {domain_noun} analytics platform.

Your ONLY job is to produce a single, executable DuckDB SELECT (or WITH ... SELECT) statement.

STRICT RULES -- never break these:
1. Output ONLY a SELECT or WITH statement. NEVER write DROP, DELETE, INSERT, UPDATE, ALTER, CREATE, or TRUNCATE.
2. Use ONLY the tables and columns described in the Schema Context. Do NOT invent columns.
3. Compute any certified business metric using ONLY the exact formula given in the
   Business Metric Definitions section below -- never re-derive it.
4. Always cast date strings with CAST(date AS DATE) when comparing against date literals.
5. Use DATE_TRUNC('week', ...) or DATE_TRUNC('month', ...) for time bucketing -- never use strftime().
6. Use LAG() window functions for period-over-period comparisons inside a CTE.
7. When grouping by two dimensions, always list BOTH in the SELECT and GROUP BY clause.
8. Dimension values are case-sensitive string literals -- use the exact values listed in the
   Dimension Values section below.

Return your answer as valid JSON: {{"sql": "<your sql here>"}}"""


def build_system_role(language: LanguageProfile | None = None) -> str:
    """The system prompt, named for the domain it is serving.

    Rule 3 used to enumerate "revenue, stock depletion, promotional uplift" and
    rule 8 "region, channel, brand" -- three FMCG facts asserted in a prompt that
    another domain would inherit verbatim. Both now point at the sections
    directly below them, which are built from that domain's own registries.
    """
    language = language or load_domain_registries().language
    return SYSTEM_ROLE_TEMPLATE.format(domain_noun=language.domain_noun)


REPAIR_ROLE_SUFFIX = "\n\nYou are repairing a previously generated SQL. Fix ONLY the reported errors."


def build_dimension_value_block(dimensions: DimensionRegistry | None = None) -> str:
    """Render the registry's dimension values as the prompt's grounding list.

    This is the single source of truth for "what literal values exist" -- see
    ``data/semantic/semantic_layer.json``'s ``dimension_values``. Previously this
    list was hardcoded a second time directly in ``SYSTEM_ROLE``.
    """
    registries = load_domain_registries()
    dimensions = dimensions or registries.dimensions
    lines = ["## Dimension Values (case-sensitive string literals)"]
    for dim, values in dimensions.all_values().items():
        lines.append(f"- {dim}: " + ", ".join(f"'{v}'" for v in values))
    # Flag columns are not dimensions -- promotion_flag is an INTEGER 0/1 -- but
    # they still need grounding, so the domain declares them separately rather
    # than having this function name one of them.
    for flag in registries.language.flag_columns():
        lines.append(f"- {flag.get('name')}: {flag.get('description')}")
    return "\n".join(lines)


def build_business_metric_block(metrics: list[dict[str, Any]]) -> str:
    """Format business metric definitions as locked injection rules."""
    if not metrics:
        return ""
    lines = ["## [SECTION 3] Business Metric Definitions (LOCKED -- use EXACTLY as written)"]
    lines.append(
        "These are the only certified formula definitions for derived metrics. "
        "Do NOT compute these metrics in any other way."
    )
    for m in metrics:
        lines.append(f"\n### {m.get('metric_name', 'unknown')}")
        lines.append(f"Description: {m.get('description', '')}")
        lines.append(f"Formula: {m.get('definition', '')}")
    return "\n".join(lines)


def build_few_shot_block(examples: list[FewShotExample] | None = None) -> str:
    """Format few-shot (question -> SQL) pairs as the training section."""
    examples = few_shot_examples() if examples is None else examples
    if not examples:
        return ""
    lines = ["## [SECTION 4] Few-Shot Examples"]
    lines.append(
        "The following verified (question -> SQL) pairs show the exact join patterns, "
        "date filter conventions, and aggregation levels you must follow."
    )
    for i, ex in enumerate(examples, 1):
        lines.append(f"\n### Example {i}")
        lines.append(f"Question: {ex['question']}")
        lines.append(f"SQL:\n{ex['sql']}")
    return "\n".join(lines)


def build_user_prompt(
    question: str,
    schema_context: str,
    intent: str,
    metrics: list[dict[str, Any]],
) -> str:
    """Assemble the full 4-section user prompt for initial SQL generation."""
    schema_block = "## [SECTION 2] Schema Context (retrieved by semantic search)\n" + schema_context
    metric_block = build_business_metric_block(metrics)
    dimension_block = build_dimension_value_block()
    shot_block = build_few_shot_block()

    return "\n\n".join(
        [
            f"## [SECTION 1] Query Intent\nIntent archetype: {intent}",
            schema_block,
            metric_block,
            dimension_block,
            shot_block,
            f'## Target Question\nNow write DuckDB SQL to answer:\n\n{question}\n\n'
            'Return JSON: {"sql": "<your sql>"}',
        ]
    )


def build_repair_prompt(
    question: str,
    schema_context: str,
    intent: str,
    failed_sql: str,
    validation_errors: list[str],
    metrics: list[dict[str, Any]],
) -> str:
    """Re-use the original prompt and append the validator's feedback."""
    base_prompt = build_user_prompt(question, schema_context, intent, metrics)
    repair_suffix = (
        "\n\n## Validator Feedback -- Fix these errors in the SQL below\n"
        + "\n".join(f"- {e}" for e in validation_errors)
        + f"\n\nFailed SQL:\n{failed_sql}"
        + '\n\nReturn corrected SQL as JSON: {"sql": "<fixed sql>"}'
    )
    return base_prompt + repair_suffix

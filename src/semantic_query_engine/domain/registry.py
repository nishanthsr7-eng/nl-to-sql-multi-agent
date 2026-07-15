"""Business-formula and dimension-value registries.

Both the certified metric formulas and the allowed dimension values used to
live independently in the prompt, the validator, the planner, and the SQL
fallback templates -- four hand-written copies of the same facts that could
silently drift apart. They now live once, in ``data/semantic/semantic_layer.json``,
and every consumer builds its view (a prompt block, a validation rule, an
entity extractor, a SQL expression) from the registries defined here.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlglot import exp, parse_one

from semantic_query_engine.semantic.layer import SemanticLayer, load_semantic_layer


@dataclass(frozen=True)
class MetricDefinition:
    """A certified business metric, plus what its formula structurally requires."""

    name: str
    formula: str
    description: str
    triggers: tuple[str, ...]
    required_columns: frozenset[str]
    multiplication_column_sets: tuple[frozenset[str], ...]
    division_column_sets: tuple[frozenset[str], ...]

    def matches_question(self, question_lower: str) -> bool:
        return any(trigger in question_lower for trigger in self.triggers)

    def is_satisfied_by(self, columns: set[str]) -> bool:
        """True when ``columns`` (a query's referenced columns) honour this formula.

        For metrics defined via multiplication/division (revenue, stock depletion),
        the same columns must be combined with the same operator -- naming the right
        columns without the right arithmetic doesn't count. Metrics with neither
        (e.g. a CASE-based aggregate) only require the referenced columns to be present.
        """
        if not self.required_columns.issubset(columns):
            return False
        if self.multiplication_column_sets:
            if not any(pair.issubset(columns) for pair in self.multiplication_column_sets):
                return False
        if self.division_column_sets:
            if not any(pair.issubset(columns) for pair in self.division_column_sets):
                return False
        return True


class MetricRegistry:
    """The single source of truth for certified business-metric formulas."""

    def __init__(self, metrics: list[dict]):
        self._definitions: dict[str, MetricDefinition] = {}
        for raw in metrics:
            definition = self._build_definition(raw)
            self._definitions[definition.name] = definition

    @staticmethod
    def _build_definition(raw: dict) -> MetricDefinition:
        name = raw["metric_name"]
        formula = raw["definition"]
        expr = parse_one(formula, read="duckdb")

        required_columns = frozenset(c.name.lower() for c in expr.find_all(exp.Column))
        mul_sets = tuple(
            frozenset(c.name.lower() for c in node.find_all(exp.Column))
            for node in expr.find_all(exp.Mul)
        )
        div_sets = tuple(
            frozenset(c.name.lower() for c in node.find_all(exp.Column))
            for node in expr.find_all(exp.Div)
        )
        triggers = tuple(raw.get("triggers") or [name.split("_")[0]])

        return MetricDefinition(
            name=name,
            formula=formula,
            description=raw.get("description", ""),
            triggers=triggers,
            required_columns=required_columns,
            multiplication_column_sets=mul_sets,
            division_column_sets=div_sets,
        )

    def get(self, name: str) -> MetricDefinition:
        return self._definitions[name]

    def all(self) -> list[MetricDefinition]:
        return list(self._definitions.values())

    def referenced_by(self, question: str) -> list[MetricDefinition]:
        """Metrics whose trigger keywords appear in ``question``."""
        lower = question.lower()
        return [m for m in self._definitions.values() if m.matches_question(lower)]

    def raw_metrics(self) -> list[dict[str, str]]:
        """Metric dicts in the shape prompts/retrieval already expect."""
        return [
            {"metric_name": m.name, "definition": m.formula, "description": m.description}
            for m in self._definitions.values()
        ]


class DimensionRegistry:
    """The single source of truth for a dimension column's allowed literal values."""

    def __init__(self, values: dict[str, list[str]], aliases: dict[str, dict[str, str]]):
        self._values = values
        self._aliases = aliases

    def dimensions(self) -> list[str]:
        return list(self._values.keys())

    def values(self, dimension: str) -> list[str]:
        return self._values.get(dimension, [])

    def match(self, dimension: str, text_lower: str) -> str | None:
        """Return the canonical value for ``dimension`` found in ``text_lower``, if any.

        Checks explicit aliases first (multi-word or abbreviated forms like "ready
        meal" -> "ReadyMeal"), then falls back to a direct case-insensitive substring
        match against the canonical values themselves.
        """
        for alias, canonical in self._aliases.get(dimension, {}).items():
            if alias in text_lower:
                return canonical
        for value in self._values.get(dimension, []):
            if value.lower() in text_lower:
                return value
        return None

    def all_values(self) -> dict[str, list[str]]:
        return dict(self._values)


def load_domain_registries(layer: SemanticLayer | None = None) -> tuple[MetricRegistry, DimensionRegistry]:
    """Build both registries from the semantic layer (loading it if not given)."""
    layer = layer or load_semantic_layer()
    return (
        MetricRegistry(layer.metrics),
        DimensionRegistry(layer.dimension_values, layer.dimension_aliases),
    )

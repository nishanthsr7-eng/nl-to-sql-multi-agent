"""Business-formula and dimension-value registries.

Both the certified metric formulas and the allowed dimension values used to
live independently in the prompt, the validator, the planner, and the SQL
fallback templates -- four hand-written copies of the same facts that could
silently drift apart. They now live once, in ``data/semantic/semantic_layer.json``,
and every consumer builds its view (a prompt block, a validation rule, an
entity extractor, a SQL expression) from the registries defined here.
"""

from __future__ import annotations

import re
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

    def match_all(self, dimension: str, text_lower: str) -> list[str]:
        """Every canonical value of ``dimension`` named in ``text_lower``, deduplicated.

        :meth:`match` answers "which value is this filtered to"; this answers "how
        many values did the user name at all". The distinction matters to the
        validator: one named value means the query should *filter* to it, but two
        or more means the user is comparing them, and grouping by the dimension
        without any literal filter is the correct SQL (see
        ``ValidatorAgent._validate_requested_entities``).
        """
        found: list[str] = []
        for alias, canonical in self._aliases.get(dimension, {}).items():
            if alias in text_lower and canonical not in found:
                found.append(canonical)
        for value in self._values.get(dimension, []):
            if value.lower() in text_lower and value not in found:
                found.append(value)
        return found

    def all_values(self) -> dict[str, list[str]]:
        return dict(self._values)


class IdentifierRegistry:
    r"""The single source of truth for high-cardinality identifier patterns.

    A dimension like ``region`` has three values that can be enumerated; an
    identifier like ``sku`` has hundreds, so it is recognised by shape instead.
    The patterns live in ``data/semantic/semantic_layer.json`` so the planner's
    entity extraction, the SQL fallback templates, and the validator's
    dropped-filter check all recognise the *same* identifiers -- they previously
    carried three independent copies of the same ``[A-Z]{2}-\d{3}`` regex.
    """

    def __init__(self, patterns: dict[str, str]):
        # Identifier codes are conventionally upper-case (MI-006), while questions
        # arrive in mixed case, so every pattern is applied case-insensitively and
        # the captured value is normalised to upper-case.
        self._patterns = {
            name: re.compile(pattern, re.IGNORECASE) for name, pattern in patterns.items()
        }

    def names(self) -> list[str]:
        return list(self._patterns)

    def find(self, name: str, text: str) -> str | None:
        """The first value of identifier ``name`` appearing in ``text``, if any."""
        match = self._patterns[name].search(text) if name in self._patterns else None
        if not match:
            return None
        # Prefer an explicit capture group when the pattern declares one, so a
        # pattern may match surrounding context without including it in the value.
        return (match.group(1) if match.groups() else match.group(0)).upper()

    def find_all(self, text: str) -> dict[str, str]:
        """Every identifier found in ``text``, keyed by identifier name."""
        found = {name: self.find(name, text) for name in self._patterns}
        return {name: value for name, value in found.items() if value}


@dataclass(frozen=True)
class TableGrain:
    """What one row of a table means, and which of its measures may be summed."""

    table: str
    key_columns: frozenset[str]
    additive_measures: frozenset[str]
    level_measures: frozenset[str]
    # A slowly-changing dimension's validity window, when it declares one: the
    # pair of columns that, together with the equality keys, identify one row.
    # Empty for an ordinary table.
    validity_from: str = ""
    validity_to: str = ""

    @property
    def validity_columns(self) -> frozenset[str]:
        return frozenset(c for c in (self.validity_from, self.validity_to) if c)

    def is_covered_by(self, join_keys: set[str], bounded_columns: set[str] | None = None) -> bool:
        """True when the join pins this table to at most one row per match.

        This is the whole fan-out test. If a join's keys cover a table's full
        grain, that table contributes at most one row per row of the other side,
        so nothing is multiplied.

        ``bounded_columns`` exists for slowly-changing dimensions, whose grain is
        only closed by a *range* predicate: ``dim_aircraft`` is one row per
        (tail_number, valid_from), and the query that selects exactly one of a
        re-registered tail's rows writes ``f.flight_date >= ac.valid_from AND
        f.flight_date < ac.valid_to`` -- an inequality, which carries no
        equality key. Counting only equalities made the *correct* query a
        fan-out, which is worse than the bug it was guarding: it trades a wrong
        answer for a refusal to answer at all.

        The relaxation is deliberately narrow. Both ends of the window must be
        declared in the semantic layer *and* constrained by the query; one end
        alone leaves the row open and still multiplies.
        """
        if not self.key_columns:
            return False
        covered = set(join_keys)
        bounded = bounded_columns or set()
        if self.validity_from and self.validity_to and {
            self.validity_from, self.validity_to
        } <= bounded:
            covered |= self.validity_columns
        return self.key_columns.issubset(covered)


class GrainRegistry:
    """Declared grain, join paths and date-column roles for every table.

    The three together are what make a fan-out decidable without executing
    anything: grain says how many rows a match can produce, the relationships say
    which joins were designed for, and the roles say whether two date columns
    denote the same kind of instant. All three are read from
    ``semantic_layer.json`` -- a grain hardcoded here would drift from the
    warehouse the first time a column moved between tables.
    """

    def __init__(self, tables: list[dict], relationships: list[dict]):
        self._grains: dict[str, TableGrain] = {}
        self._roles: dict[tuple[str, str], str] = {}
        self._columns: dict[str, frozenset[str]] = {}

        for table in tables:
            name = str(table.get("table_name", "")).lower()
            if not name:
                continue
            grain = table.get("grain") or {}
            validity = grain.get("validity") or {}
            self._grains[name] = TableGrain(
                table=name,
                key_columns=frozenset(c.lower() for c in grain.get("columns", [])),
                additive_measures=frozenset(c.lower() for c in grain.get("additive_measures", [])),
                level_measures=frozenset(c.lower() for c in grain.get("level_measures", [])),
                validity_from=str(validity.get("from", "")).lower(),
                validity_to=str(validity.get("to", "")).lower(),
            )
            columns = set()
            for column in table.get("columns", []):
                column_name = str(column.get("name", "")).lower()
                columns.add(column_name)
                role = column.get("role")
                if role:
                    self._roles[(name, column_name)] = str(role)
            self._columns[name] = frozenset(columns)

        # Stored unordered: a join is supported in either direction, and the
        # model writes them both ways.
        self._join_paths: set[frozenset[tuple[str, str]]] = set()
        for rel in relationships:
            left = self._split_reference(rel.get("from"))
            right = self._split_reference(rel.get("to"))
            if left and right:
                self._join_paths.add(frozenset({left, right}))

    @staticmethod
    def _split_reference(reference: object) -> tuple[str, str] | None:
        if not isinstance(reference, str) or "." not in reference:
            return None
        table, column = reference.rsplit(".", 1)
        return table.lower(), column.lower()

    def grain(self, table: str) -> TableGrain | None:
        return self._grains.get(table.lower())

    def owns_column(self, table: str, column: str) -> bool:
        return column.lower() in self._columns.get(table.lower(), frozenset())

    def any_table_owns_column(self, column: str) -> bool:
        """True when *some* table in the warehouse declares this column.

        Used to tell a metric formula's real column references apart from the
        SQL keywords the formula is tokenised into (SUM, CASE, END). Asking
        "does any table have it" is the only way to make that distinction
        without a keyword list: a keyword belongs to no table, a column belongs
        to at least one -- including, crucially, a table other than the one
        being tested.
        """
        return any(column.lower() in columns for columns in self._columns.values())

    def role(self, table: str, column: str) -> str | None:
        """The temporal role of a date column -- ``day``, ``week_start``, ...

        Two date columns with different roles denote different things, so
        equating them is a category error, not a narrow join.
        """
        return self._roles.get((table.lower(), column.lower()))

    def is_declared_join(self, left: str, left_column: str, right: str, right_column: str) -> bool:
        pair = frozenset({(left.lower(), left_column.lower()), (right.lower(), right_column.lower())})
        return pair in self._join_paths

    def has_any_path(self, left: str, right: str) -> bool:
        """True when any declared relationship connects these two tables."""
        left, right = left.lower(), right.lower()
        return any(
            {table for table, _ in pair} == {left, right} for pair in self._join_paths
        )

    def tables(self) -> list[str]:
        return list(self._grains)


@dataclass(frozen=True)
class TopicPrompt:
    """One trigger-word group and the clarification it earns.

    The planner used to special-case the word "promotion" in three places, with
    an FMCG-specific menu of follow-up questions written inline. That menu is a
    fact about the retail domain, not about planning, so it moved into the
    semantic layer next to the dimension values -- a domain whose ambiguous
    topic is "delay" rather than "promotion" needs no code change to say so.
    """

    triggers: tuple[str, ...]
    prompt: str

    def matches(self, question_lower: str) -> bool:
        return any(trigger in question_lower for trigger in self.triggers)


class LanguageProfile:
    """The domain's vocabulary for the rule-based planner and the prompts.

    Everything here was a literal tuple in ``agents/planner.py``,
    ``prompts/sql_generation.py`` or ``core/catalog.py``: comparative hints,
    metric keywords, which dimensions count as a scope anchor, what a
    clarification should offer. Those literals are the reason a second domain
    would have needed a code change, and therefore the reason the "the semantic
    layer is the contract" claim could not be checked. They now come out of the
    ``language`` block of the domain's semantic layer.

    Every accessor has a defensible empty default rather than a retail one, so a
    half-written domain degrades to "the planner recognises less" instead of
    silently classifying an airline question with grocery keywords.
    """

    def __init__(self, language: dict):
        self._raw = language or {}

    # --- prompt-facing -----------------------------------------------------

    @property
    def domain_noun(self) -> str:
        """What the generator's system prompt calls this warehouse."""
        return str(self._raw.get("domain_noun", "analytics"))

    def flag_columns(self) -> list[dict[str, str]]:
        """Columns whose allowed values are a type, not a dimension vocabulary.

        ``promotion_flag`` is an INTEGER 0/1: it belongs in the prompt's value
        grounding, but it is not a dimension and enumerating it as one would put
        "0, 1" in a list of case-sensitive string literals.
        """
        return [dict(entry) for entry in self._raw.get("flag_columns", [])]

    # --- planner -----------------------------------------------------------

    def hints(self, name: str) -> tuple[str, ...]:
        """One keyword group -- comparative, diagnostic, vague_openers, ..."""
        return tuple(str(item).lower() for item in self._raw.get(name, []))

    def metric_keywords(self) -> list[tuple[str, tuple[str, ...]]]:
        """Metric-group name -> the words that name it, in declaration order.

        Order is load-bearing: "promotion revenue" is a revenue question, and
        the retail layer lists the revenue group first for exactly that reason.
        A dict preserves insertion order in the JSON, so the file reads as the
        priority list it is.
        """
        return [
            (str(group), tuple(str(word).lower() for word in words))
            for group, words in (self._raw.get("metric_keywords") or {}).items()
        ]

    def anchor_dimensions(self) -> tuple[str, ...]:
        """Dimensions specific enough that naming one anchors a lookup."""
        return tuple(str(d) for d in self._raw.get("anchor_dimensions", []))

    def scope_dimensions(self) -> tuple[str, ...]:
        """Dimensions that count as "the question says what it is about"."""
        return tuple(str(d) for d in self._raw.get("scope_dimensions", []))

    def missing_param_prompt(self, slot: str) -> str:
        """How to ask for a missing metric / timeframe / scope, in this domain."""
        return str((self._raw.get("missing_param_prompts") or {}).get(slot, slot))

    def topic_prompts(self) -> list[TopicPrompt]:
        return [
            TopicPrompt(
                triggers=tuple(str(t).lower() for t in entry.get("triggers", [])),
                prompt=str(entry.get("prompt", "")),
            )
            for entry in self._raw.get("topic_prompts", [])
        ]

    def clarification_example(self) -> str:
        return str(self._raw.get("clarification_example", ""))

    # --- failure recovery --------------------------------------------------

    def recovery_ideas(self, question_lower: str) -> list[str]:
        """Working alternatives to offer when a guarded query cannot run."""
        for entry in self._raw.get("recovery_ideas", []):
            triggers = [str(t).lower() for t in entry.get("triggers", [])]
            if triggers and any(t in question_lower for t in triggers):
                return [str(s) for s in entry.get("suggestions", [])]
        return [str(s) for s in self._raw.get("default_recovery_ideas", [])]


@dataclass(frozen=True)
class DomainRegistries:
    """The registries built from one semantic layer.

    Returned as a named bundle rather than a tuple so call sites read as
    ``registries.dimensions`` instead of ``load_domain_registries()[1]``.
    """

    metrics: MetricRegistry
    dimensions: DimensionRegistry
    identifiers: IdentifierRegistry
    grains: GrainRegistry
    language: LanguageProfile


def load_domain_registries(layer: SemanticLayer | None = None) -> DomainRegistries:
    """Build every registry from the semantic layer (loading it if not given)."""
    layer = layer or load_semantic_layer()
    return DomainRegistries(
        metrics=MetricRegistry(layer.metrics),
        dimensions=DimensionRegistry(layer.dimension_values, layer.dimension_aliases),
        identifiers=IdentifierRegistry(layer.identifier_patterns),
        grains=GrainRegistry(layer.tables, layer.relationships),
        language=LanguageProfile(layer.language),
    )

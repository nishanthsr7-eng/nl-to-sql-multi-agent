"""Parser-backed SQL safety, schema, and metric validation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, cast

import duckdb
from sqlglot import exp, parse_one

from semantic_query_engine.core.config import PIPELINE
from semantic_query_engine.domain.registry import MetricRegistry, load_domain_registries


@dataclass
class ValidationResult:
    is_valid: bool
    sanitized_sql: str
    errors: list[str]


class ValidatorAgent:
    """Validate query structure before it reaches DuckDB.

    sqlglot is used rather than regex to inspect the parsed query tree. DuckDB's EXPLAIN
    remains the final live-schema and query-plan check, but execution happens only once in
    the orchestrator after validation succeeds.
    """

    ALLOWED_TABLES = PIPELINE.allowed_tables
    MAX_LIMIT = PIPELINE.max_result_rows
    MIN_ANALYTICAL_ROWS = PIPELINE.min_analytical_rows

    def __init__(self, metric_registry: MetricRegistry | None = None):
        self.metrics = metric_registry or load_domain_registries()[0]

    def run(
        self,
        sql: str,
        conn: duckdb.DuckDBPyConnection,
        question: str = "",
        params: list[Any] | None = None,
    ) -> ValidationResult:
        params = params or []
        cleaned = sql.strip().rstrip(";")
        errors: list[str] = []
        if not cleaned:
            return ValidationResult(False, cleaned, ["SQL statement is empty."])

        try:
            # sqlglot's parse_one() stub returns its internal Expr TypeVar rather
            # than the public Expression type when `into` isn't passed -- cast to
            # what it actually returns at runtime (an Expression) so the rest of
            # this method's calls into `exp.*`-typed helpers type-check.
            tree = cast(exp.Expression, parse_one(cleaned, read="duckdb"))
        except Exception as exc:
            return ValidationResult(False, cleaned, [f"SQL parse error: {exc}"])

        if not isinstance(tree, exp.Select) and not tree.find(exp.Select):
            errors.append("Only SELECT/WITH queries are allowed.")
        if any(tree.find(kind) for kind in (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter)):
            errors.append("Data-definition and data-modification operations are not allowed.")
        if tree.find(exp.Union):
            errors.append("UNION queries are not allowed.")

        tables = list(tree.find_all(exp.Table))
        cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
        physical_tables = {table.name.lower() for table in tables if table.name.lower() not in cte_names}
        unknown = physical_tables - self.ALLOWED_TABLES
        if unknown:
            errors.append(f"Unknown table reference: {', '.join(sorted(unknown))}")

        limit = tree.args.get("limit")
        if limit and isinstance(limit.expression, exp.Literal):
            try:
                limit_value = int(limit.expression.this)
                if limit_value > self.MAX_LIMIT:
                    errors.append(f"LIMIT exceeds the safe maximum of {self.MAX_LIMIT} rows.")
                elif limit_value < self.MIN_ANALYTICAL_ROWS and not self._allows_short_limit(question, limit_value):
                    errors.append(
                        f"Default analytical results must return at least {self.MIN_ANALYTICAL_ROWS} rows; "
                        "use a larger LIMIT unless the user explicitly asks for a top-N result."
                    )
            except (ValueError, TypeError):
                errors.append("LIMIT must be a numeric literal.")

        schema = self._live_schema(conn)
        errors.extend(self._validate_columns(tree, schema, cte_names))
        errors.extend(self._validate_metric_contract(question, tree, self.metrics))
        errors.extend(self._validate_requested_entities(question, cleaned, params))

        if errors:
            return ValidationResult(False, cleaned, errors)
        try:
            if params:
                conn.execute(f"EXPLAIN {cleaned}", params)
            else:
                conn.execute(f"EXPLAIN {cleaned}")
        except Exception as exc:
            return ValidationResult(False, cleaned, [f"SQL syntax/plan error: {exc}"])
        return ValidationResult(True, cleaned, [])

    @staticmethod
    def _allows_short_limit(question: str, limit_value: int) -> bool:
        """A short result is valid only when the question explicitly requests it."""
        lower = question.lower()
        requested_top_n = re.search(r"\btop\s+(\d+)\b", lower)
        if requested_top_n:
            return int(requested_top_n.group(1)) == limit_value
        return limit_value == 1 and any(term in lower for term in ("highest", "lowest", "best", "worst"))

    def _live_schema(self, conn: duckdb.DuckDBPyConnection) -> dict[str, set[str]]:
        return {
            table: {row[0].lower() for row in conn.execute(f"DESCRIBE {table}").fetchall()}
            for table in self.ALLOWED_TABLES
        }

    @staticmethod
    def _validate_columns(tree: exp.Expression, schema: dict[str, set[str]], cte_names: set[str]) -> list[str]:
        errors: list[str] = []
        aliases = {
            table.alias_or_name.lower(): table.name.lower()
            for table in tree.find_all(exp.Table)
            if table.name.lower() in schema
        }
        known_columns = set().union(*schema.values())
        # SELECT aliases and CTE output fields are valid references in ORDER BY and
        # downstream CTEs, even though they are not physical table columns.
        derived_columns = {
            alias.alias.lower() for alias in tree.find_all(exp.Alias) if alias.alias
        }
        for column in tree.find_all(exp.Column):
            name = column.name.lower()
            qualifier = column.table.lower() if column.table else ""
            if qualifier in cte_names:
                continue
            if qualifier in aliases and name not in schema[aliases[qualifier]]:
                errors.append(f"Unknown column '{column.name}' on table '{aliases[qualifier]}'.")
            elif qualifier and qualifier not in aliases:
                # The qualifier may be a CTE alias; its definition is checked by EXPLAIN.
                if qualifier not in cte_names:
                    errors.append(f"Unknown table alias '{column.table}' for column '{column.name}'.")
            elif not qualifier and name not in known_columns and name not in derived_columns:
                errors.append(f"Unknown column '{column.name}'.")
        return list(dict.fromkeys(errors))

    @staticmethod
    def _validate_metric_contract(question: str, tree: exp.Expression, metrics: MetricRegistry) -> list[str]:
        """Any certified metric named in the question must be computed via its registry formula.

        Driven entirely by :class:`~semantic_query_engine.domain.registry.MetricRegistry` -- the formula,
        its trigger keywords, and the columns/operators it requires all come from
        ``data/semantic/semantic_layer.json``, not a second hand-written rule per metric.
        """
        columns = {column.name.lower() for column in tree.find_all(exp.Column)}
        errors: list[str] = []
        for metric in metrics.referenced_by(question):
            uses_metric_alias = metric.name.lower() in columns or any(
                token in columns for token in metric.name.lower().split("_")
            )
            if not (uses_metric_alias or metric.is_satisfied_by(columns)):
                errors.append(
                    f"'{metric.name}' queries must use the certified formula ({metric.formula}) "
                    f"or reference the {metric.name} field."
                )
        return errors

    @staticmethod
    def _validate_requested_entities(question: str, sql: str, params: list[Any]) -> list[str]:
        """Prevent a repair/fallback from silently dropping explicit SKU or region filters.

        Checks both the SQL text and any bound ``?`` parameter values, since fallback
        templates pass literals like the SKU or region through bound params rather
        than inlining them into the SQL string (see ``sql_generator.TEMPLATES``).
        """
        errors: list[str] = []
        param_text = " ".join(str(p).lower() for p in params)
        haystack = f"{sql.lower()} {param_text}"

        sku = re.search(r"\b([A-Z]{2}-\d{3})\b", question.upper())
        if sku and sku.group(1).lower() not in haystack:
            errors.append(f"Requested SKU {sku.group(1)} is missing from the SQL filter.")
        regions = ("north", "south", "east", "west", "central")
        requested_region = next((region for region in regions if re.search(rf"\b{region}\b", question, re.I)), None)
        if requested_region and requested_region not in haystack:
            errors.append(f"Requested region {requested_region.title()} is missing from the SQL filter.")
        return errors

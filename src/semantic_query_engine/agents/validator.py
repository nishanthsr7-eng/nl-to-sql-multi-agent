"""Parser-backed SQL safety, schema, and metric validation.

The validator is the one place that decides whether model-authored SQL is allowed
to touch the warehouse, so it works on the parsed sqlglot tree rather than on the
query text: a regex can be defeated by a comment or a line break, a parse tree
cannot. DuckDB's ``EXPLAIN`` still runs last as the live-schema and query-plan
check, but it runs only after the structural rules have passed, and the query
itself executes exactly once, in the orchestrator, after this returns valid.

Three properties are worth calling out, because they are what the rest of the
system relies on:

* **Column resolution is scope-aware.** Names are resolved against the sources of
  the specific ``SELECT`` they appear in -- not against a flat union of every
  allowed table's columns. A union accepts ``lifecycle_stage`` (a
  ``weekly_modeling_data`` column) in a query that only reads ``fmcg_sales``,
  which is precisely the class of error a multi-table warehouse produces.
* **Every issue carries a machine-readable code.** Rejection *reasons* are the
  interesting signal from a guardrail ("what does the model get wrong, and how
  often?"), and that can only be aggregated if it isn't free text.
* **The result set is bounded.** A query that arrives without a ``LIMIT`` gets one
  injected, rather than being trusted to be small.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, cast

import duckdb
from sqlglot import exp, parse

from semantic_query_engine.core.config import PIPELINE
from semantic_query_engine.core.domains import active_domain
from semantic_query_engine.domain.registry import (
    DimensionRegistry,
    GrainRegistry,
    IdentifierRegistry,
    MetricRegistry,
    load_domain_registries,
)
from semantic_query_engine.governance.masking import (
    MaskedColumn,
    derived_pii_expressions,
    masked_columns,
)
from semantic_query_engine.governance.policy import GovernancePolicy, load_governance_policy
from semantic_query_engine.governance.principals import STEWARD, Principal
from semantic_query_engine.governance.row_security import (
    AppliedPolicy,
    apply_row_policies,
    scope_breaches,
)


class IssueCode:
    """Stable identifiers for every way validation can fail.

    These are written to ``Failure.issue_codes`` and are the group-by key for the
    validator funnel ("N% of first-attempt generations were rejected; here is the
    breakdown by reason"). Codes are append-only -- renaming one silently breaks
    comparison against previously recorded eval runs.
    """

    EMPTY = "empty_sql"
    PARSE_ERROR = "parse_error"
    NOT_A_SELECT = "not_a_select"
    MUTATION = "mutation_not_allowed"
    UNION = "union_not_allowed"
    UNKNOWN_TABLE = "unknown_table"
    UNKNOWN_COLUMN = "unknown_column"
    UNKNOWN_ALIAS = "unknown_table_alias"
    AMBIGUOUS_COLUMN = "ambiguous_column"
    LIMIT_TOO_LARGE = "limit_too_large"
    LIMIT_TOO_SMALL = "limit_too_small"
    LIMIT_NOT_NUMERIC = "limit_not_numeric"
    METRIC_CONTRACT = "metric_contract_violation"
    DROPPED_FILTER = "dropped_entity_filter"
    PLAN_ERROR = "plan_error"
    GRAIN_FANOUT = "grain_fanout"
    GRAIN_KEY_MISMATCH = "grain_key_mismatch"
    UNRELATED_JOIN = "unrelated_join"
    MULTIPLE_STATEMENTS = "multiple_statements"
    PII_DERIVED = "pii_derived_expression"
    ROW_POLICY_BREACH = "row_policy_breach"


# Everything that can change the database, the session, or the filesystem.
#
# Wider than "INSERT/UPDATE/DELETE/DROP/CREATE/ALTER" on purpose, because that
# list is a list of the statements somebody remembered. TRUNCATE was missing
# from it and DuckDB was happy to run one. The rest are the same class reached
# by a different verb: COPY writes files, ATTACH mounts another database, SET
# and PRAGMA change how the session behaves, GRANT changes who may do what.
#
# ``exp.Command`` is the important entry and the one that makes this list stop
# being a list of remembered names: sqlglot parses anything it does not model
# into a Command, so INSTALL, LOAD, CALL and every future verb land there. SQL
# the validator cannot analyse is exactly the thing it must not wave through --
# the whole premise of this module is that the tree is understood.
_MUTATING_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.TruncateTable,
    exp.Merge,
    exp.Copy,
    exp.Attach,
    exp.Detach,
    exp.Grant,
    exp.Set,
    exp.Pragma,
    exp.Use,
    exp.Command,
)


@dataclass(frozen=True)
class ValidationIssue:
    """One reason a query was rejected: a stable code plus a human-readable message."""

    code: str
    message: str


@dataclass
class ValidationResult:
    is_valid: bool
    sanitized_sql: str
    issues: list[ValidationIssue] = field(default_factory=list)
    # Set when the validator injected a LIMIT the model did not write, so callers
    # can tell the user the result was capped rather than implying completeness.
    applied_limit: int | None = None
    # Which row predicates were injected, and which output columns will be
    # masked. Carried on the result rather than applied silently: a caller that
    # cannot see *that* its answer was restricted has been given a number that
    # looks like the whole picture and is not.
    applied_policies: list[AppliedPolicy] = field(default_factory=list)
    masked_columns: list[MaskedColumn] = field(default_factory=list)

    @property
    def errors(self) -> list[str]:
        """Issue messages only -- what gets shown to a user or fed back to a repair."""
        return [issue.message for issue in self.issues]

    @property
    def issue_codes(self) -> list[str]:
        return [issue.code for issue in self.issues]

    @classmethod
    def failed(cls, sql: str, *issues: ValidationIssue) -> ValidationResult:
        return cls(is_valid=False, sanitized_sql=sql, issues=list(issues))


@dataclass(frozen=True)
class _Source:
    """One thing a ``SELECT`` reads from: a physical table, a CTE, or a subquery.

    ``opaque`` marks a source whose output columns cannot be determined statically
    -- a CTE or subquery that does ``SELECT *``. Columns are not resolved against
    an opaque source; ``EXPLAIN`` is left to catch mistakes there, because guessing
    would mean rejecting valid SQL.
    """

    alias: str
    name: str
    columns: frozenset[str]
    opaque: bool = False


@dataclass(frozen=True)
class _Scope:
    """The names visible inside one ``SELECT``."""

    sources: tuple[_Source, ...]
    derived: frozenset[str]        # aliases defined by this SELECT's own output list
    using_columns: frozenset[str]  # join keys merged by USING/NATURAL, never ambiguous

    def source_for_alias(self, alias: str) -> _Source | None:
        return next((s for s in self.sources if s.alias == alias), None)

    def visible_columns(self) -> set[str]:
        columns: set[str] = set()
        for source in self.sources:
            columns |= source.columns
        return columns

    def has_opaque_source(self) -> bool:
        return any(source.opaque for source in self.sources)


@dataclass(frozen=True)
class _JoinLink:
    """Two sources in one ``SELECT`` and the equality keys tying them together.

    Built from every equality that constrains *every* row of the join -- ``ON``,
    ``USING``, and top-level ``WHERE`` conjunctions alike, because a model that
    writes ``FROM a, b WHERE a.id = b.id`` has written the same join as one that
    spells out ``JOIN ... ON``, and the row multiplication is identical.
    """

    left_alias: str
    right_alias: str
    left_table: str
    right_table: str
    column_pairs: frozenset[tuple[str, str]]
    # Columns of each side compared to the *other* side with an inequality
    # (``>=``, ``<``, ...). Not join keys -- a range does not pin a row on its
    # own -- but the two ends of a declared validity window together do, which
    # is how a correct slowly-changing-dimension join is told from a fan-out.
    left_bounded: frozenset[str] = frozenset()
    right_bounded: frozenset[str] = frozenset()

    @property
    def left_keys(self) -> set[str]:
        return {left for left, _ in self.column_pairs}

    @property
    def right_keys(self) -> set[str]:
        return {right for _, right in self.column_pairs}


class ValidatorAgent:
    """Validate query structure before it reaches DuckDB."""

    MAX_LIMIT = PIPELINE.max_result_rows
    MIN_ANALYTICAL_ROWS = PIPELINE.min_analytical_rows

    def __init__(
        self,
        metric_registry: MetricRegistry | None = None,
        dimension_registry: DimensionRegistry | None = None,
        identifier_registry: IdentifierRegistry | None = None,
        grain_registry: GrainRegistry | None = None,
        governance: GovernancePolicy | None = None,
    ):
        registries = load_domain_registries()
        # Captured at construction, like the registries and LLMSettings before
        # it: a validator that re-read the active domain per call could be asked
        # to police an airline query with a retail table list.
        self.allowed_tables = active_domain().allowed_tables
        self.metrics = metric_registry or registries.metrics
        self.dimensions = dimension_registry or registries.dimensions
        self.identifiers = identifier_registry or registries.identifiers
        self.grains = grain_registry or registries.grains
        # Captured like the registries, and for the same reason: the policy is a
        # property of the domain. The *principal* deliberately is not -- it is a
        # property of one request, arrives as an argument to :meth:`run`, and an
        # API server sharing one validator across callers depends on that.
        self.governance = governance or load_governance_policy()
        # The warehouse schema does not change while the process runs, so the
        # DESCRIBE round-trip per allowed table is done once per agent rather than
        # once per validation -- this method used to run on every single query.
        self._schema_cache: dict[str, set[str]] | None = None

    # -----------------------------------------------------------------------
    # Entry point
    # -----------------------------------------------------------------------

    @staticmethod
    def _parse_single(cleaned: str) -> tuple[exp.Expression | None, ValidationIssue | None]:
        """Parse exactly one statement, or say why that was not possible.

        The plural :func:`sqlglot.parse` rather than ``parse_one``, and the
        count is checked, because ``parse_one`` folds ``a; b`` into a single
        ``Block`` node -- and every structural check below then analyses the
        statement that block *starts* with. That was a live hole: DuckDB
        executes ``TRUNCATE TABLE fmcg_sales; SELECT 1`` in full, while the
        validator saw a ``Block`` containing a ``SELECT`` and passed it. Found
        by the property tests in tests/unit/test_validator_properties.py, which
        is the class of bug they exist for.

        Rejecting the second statement outright, rather than validating each
        one, is the right shape: one question produces one query, so a
        semicolon in model output is never something to accommodate.
        """
        try:
            statements = parse(cleaned, read="duckdb")
        except Exception as exc:
            return None, ValidationIssue(IssueCode.PARSE_ERROR, f"SQL parse error: {exc}")

        present = [statement for statement in statements if statement is not None]
        if not present:
            return None, ValidationIssue(IssueCode.EMPTY, "SQL statement is empty.")
        if len(present) > 1:
            return None, ValidationIssue(
                IssueCode.MULTIPLE_STATEMENTS,
                f"Only one statement is allowed; {len(present)} were given. "
                "Everything after the first semicolon would still execute.",
            )
        # sqlglot types parse() with its internal Expr TypeVar rather than the
        # public Expression; cast to what it returns at runtime.
        return cast(exp.Expression, present[0]), None

    def safety_only(self, sql: str, principal: Principal | None = None) -> ValidationResult:
        """The mutation check alone -- everything else passes.

        This is what the evaluation ladder's un-validated rungs run. It exists so
        that "no validator" means "no schema, metric or bound checks" rather than
        "arbitrary model output goes straight to the warehouse": scoring a
        baseline is not a reason to execute a generated ``DROP TABLE``, and the
        claim the ladder makes is about the semantic checks, not about whether a
        ``DELETE`` reaches disk.

        Deliberately shares :meth:`_validate_statement_kind` with :meth:`run`
        rather than re-deriving the rule, so the safety floor cannot drift
        between the measured configuration and the shipped one.

        Row policies are **not** ablatable. A restricted principal is refused
        outright here rather than served an unfiltered query, because "we
        switched the guardrails off to score a baseline" is a defensible
        experimental choice about semantic checks and an indefensible one about
        access control. The ladder is only ever run as the unrestricted steward,
        so in practice this never fires -- which is the point of asserting it.
        """
        principal = principal or STEWARD
        if not principal.unrestricted and self.governance.row_policies:
            return ValidationResult.failed(
                sql.strip().rstrip(";"),
                ValidationIssue(
                    IssueCode.ROW_POLICY_BREACH,
                    f"{principal.id} is subject to row policies, which the "
                    "validator-ablated configuration cannot enforce. Run this "
                    "configuration as an unrestricted principal.",
                ),
            )
        cleaned = sql.strip().rstrip(";")
        if not cleaned:
            return ValidationResult.failed(
                cleaned, ValidationIssue(IssueCode.EMPTY, "SQL statement is empty.")
            )
        tree, problem = self._parse_single(cleaned)
        if tree is None:
            return ValidationResult.failed(cleaned, cast(ValidationIssue, problem))

        issues = self._validate_statement_kind(tree)
        if issues:
            return ValidationResult(is_valid=False, sanitized_sql=cleaned, issues=issues)
        return ValidationResult(is_valid=True, sanitized_sql=cleaned)

    def run(
        self,
        sql: str,
        conn: duckdb.DuckDBPyConnection,
        question: str = "",
        params: list[Any] | None = None,
        principal: Principal | None = None,
    ) -> ValidationResult:
        """Validate ``sql`` and return it bounded and governed, or the reasons it was rejected.

        ``principal`` defaults to the unrestricted steward, so every existing
        caller and every recorded evaluation keeps the behaviour it was measured
        with; restriction is something a caller asks for.
        """
        principal = principal or STEWARD
        params = params or []
        cleaned = sql.strip().rstrip(";")
        if not cleaned:
            return ValidationResult.failed(cleaned, ValidationIssue(IssueCode.EMPTY, "SQL statement is empty."))

        tree, problem = self._parse_single(cleaned)
        if tree is None:
            return ValidationResult.failed(cleaned, cast(ValidationIssue, problem))

        issues: list[ValidationIssue] = []
        issues.extend(self._validate_statement_kind(tree))
        issues.extend(self._validate_tables(tree))
        issues.extend(self._validate_limit(tree, question))
        issues.extend(self._validate_columns(tree, self._live_schema(conn)))
        issues.extend(self._validate_grain(tree))
        issues.extend(self._validate_metric_contract(question, tree, self.metrics))
        issues.extend(self._validate_requested_entities(question, cleaned, params))
        issues.extend(self._validate_pii_exposure(tree, principal))

        if issues:
            return ValidationResult(is_valid=False, sanitized_sql=cleaned, issues=issues)

        # Governance runs on an otherwise-valid query, and runs *before* the row
        # bound so that EXPLAIN plans the query as it will actually execute. Two
        # things happen in order and both matter: the row predicates are injected,
        # and then the query text is re-derived from the rewritten tree -- a
        # predicate that stayed in the tree while the original string went on to
        # execution would be governance that reads correctly and enforces nothing.
        governed, applied_policies = apply_row_policies(tree, principal, self.governance)
        if applied_policies:
            tree = governed
            cleaned = governed.sql(dialect="duckdb")

        # Only now bound the result set. Doing this before the checks above would
        # mean re-serialising a tree that is about to be rejected anyway, and would
        # put a LIMIT the model never wrote into the SQL echoed back to it during
        # repair.
        bounded_sql, applied_limit = self._apply_row_bound(tree, cleaned)

        # Re-derived from the SQL that is about to run, not from
        # ``applied_policies``, on the same principle as
        # ``report.executed_unsafely()``: the question is what the warehouse will
        # see, not what this method believes it built. Reaching here is a bug in
        # the injector rather than a bad generation, so it is reported as its own
        # code and never repaired -- handing it back to the model would ask a
        # language model to fix a security control.
        breaches = scope_breaches(bounded_sql, principal, self.governance)
        if breaches:
            return ValidationResult.failed(
                bounded_sql,
                *[ValidationIssue(IssueCode.ROW_POLICY_BREACH, breach) for breach in breaches],
            )

        try:
            if params:
                conn.execute(f"EXPLAIN {bounded_sql}", params)
            else:
                conn.execute(f"EXPLAIN {bounded_sql}")
        except Exception as exc:
            return ValidationResult.failed(
                bounded_sql, ValidationIssue(IssueCode.PLAN_ERROR, f"SQL syntax/plan error: {exc}")
            )

        return ValidationResult(
            is_valid=True,
            sanitized_sql=bounded_sql,
            applied_limit=applied_limit,
            applied_policies=applied_policies,
            masked_columns=masked_columns(tree, principal, self.governance),
        )

    def _validate_pii_exposure(
        self, tree: exp.Expression, principal: Principal
    ) -> list[ValidationIssue]:
        """Reject projections that compute over personal data in an unmaskable way.

        Masking handles a tagged column that is selected; nothing can mask
        ``UPPER(crew_email)``, because the disclosure happened in the warehouse
        before any result row existed. A principal cleared for personal data is
        not subject to this -- there is nothing to protect them from.
        """
        if principal.sees_unmasked_pii:
            return []
        return [
            ValidationIssue(
                IssueCode.PII_DERIVED,
                f"{offence}. Select the column itself (it will be masked), or aggregate it with COUNT.",
            )
            for offence in derived_pii_expressions(tree, self.governance)
        ]

    # -----------------------------------------------------------------------
    # Statement shape and table allow-list
    # -----------------------------------------------------------------------

    @staticmethod
    def _validate_statement_kind(tree: exp.Expression) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        if not isinstance(tree, exp.Select) and not tree.find(exp.Select):
            issues.append(ValidationIssue(IssueCode.NOT_A_SELECT, "Only SELECT/WITH queries are allowed."))
        if any(tree.find(kind) for kind in _MUTATING_NODES):
            issues.append(
                ValidationIssue(
                    IssueCode.MUTATION,
                    "Data-definition and data-modification operations are not allowed.",
                )
            )
        if tree.find(exp.Union):
            issues.append(ValidationIssue(IssueCode.UNION, "UNION queries are not allowed."))
        return issues

    def _validate_tables(self, tree: exp.Expression) -> list[ValidationIssue]:
        cte_names = _cte_names(tree)
        physical = {
            table.name.lower()
            for table in tree.find_all(exp.Table)
            if table.name.lower() not in cte_names
        }
        unknown = physical - self.allowed_tables
        if unknown:
            return [
                ValidationIssue(
                    IssueCode.UNKNOWN_TABLE, f"Unknown table reference: {', '.join(sorted(unknown))}"
                )
            ]
        return []

    # -----------------------------------------------------------------------
    # Result-set bounds
    # -----------------------------------------------------------------------

    def _validate_limit(self, tree: exp.Expression, question: str) -> list[ValidationIssue]:
        limit = tree.args.get("limit")
        if not limit or not isinstance(limit.expression, exp.Literal):
            return []
        try:
            limit_value = int(limit.expression.this)
        except (ValueError, TypeError):
            return [ValidationIssue(IssueCode.LIMIT_NOT_NUMERIC, "LIMIT must be a numeric literal.")]

        if limit_value > self.MAX_LIMIT:
            return [
                ValidationIssue(
                    IssueCode.LIMIT_TOO_LARGE, f"LIMIT exceeds the safe maximum of {self.MAX_LIMIT} rows."
                )
            ]
        if limit_value < self.MIN_ANALYTICAL_ROWS and not self._allows_short_limit(question, limit_value):
            return [
                ValidationIssue(
                    IssueCode.LIMIT_TOO_SMALL,
                    f"Default analytical results must return at least {self.MIN_ANALYTICAL_ROWS} rows; "
                    "use a larger LIMIT unless the user explicitly asks for a top-N result.",
                )
            ]
        return []

    def _apply_row_bound(self, tree: exp.Expression, cleaned: str) -> tuple[str, int | None]:
        """Ensure the query cannot return more than ``MAX_LIMIT`` rows.

        ``_validate_limit`` only polices a LIMIT the model chose to write; a query
        with none was previously unbounded, which made ``max_result_rows`` a
        setting the system advertised but never enforced. An aggregate over a
        high-cardinality dimension is an ordinary way to produce far more rows than
        anything downstream can render.

        The original SQL text is returned untouched when a LIMIT is already present,
        so the common path never pays for an AST round-trip and the SQL shown to the
        user stays exactly as generated.
        """
        if tree.args.get("limit") is not None or not isinstance(tree, exp.Select):
            return cleaned, None
        bounded = tree.copy().limit(self.MAX_LIMIT)
        return bounded.sql(dialect="duckdb"), self.MAX_LIMIT

    @staticmethod
    def _allows_short_limit(question: str, limit_value: int) -> bool:
        """A short result is valid only when the question explicitly requests it."""
        lower = question.lower()
        requested_top_n = re.search(r"\btop\s+(\d+)\b", lower)
        if requested_top_n:
            return int(requested_top_n.group(1)) == limit_value
        return limit_value == 1 and any(term in lower for term in ("highest", "lowest", "best", "worst"))

    # -----------------------------------------------------------------------
    # Schema
    # -----------------------------------------------------------------------

    def _live_schema(self, conn: duckdb.DuckDBPyConnection) -> dict[str, set[str]]:
        if self._schema_cache is None:
            self._schema_cache = {
                table: {row[0].lower() for row in conn.execute(f"DESCRIBE {table}").fetchall()}
                for table in self.allowed_tables
            }
        return self._schema_cache

    def _validate_columns(
        self, tree: exp.Expression, schema: dict[str, set[str]]
    ) -> list[ValidationIssue]:
        """Resolve every column against the sources of the SELECT it appears in.

        The previous implementation compared unqualified columns against the union
        of every allowed table's columns, which cannot distinguish "this column
        exists somewhere in the warehouse" from "this column exists in the table
        this query reads". With one fact table that was invisible; with a star
        schema it silently admits cross-table nonsense.
        """
        cte_columns = _cte_columns(tree, schema)
        # Keyed by ``id`` rather than by the node itself: sqlglot's Expression
        # defines structural equality and hashing, so two textually identical
        # subqueries in one statement would otherwise collapse to a single key and
        # be resolved against each other's sources.
        scopes = {
            id(select): _build_scope(select, schema, cte_columns)
            for select in tree.find_all(exp.Select)
        }

        issues: list[ValidationIssue] = []
        for column in tree.find_all(exp.Column):
            select = _enclosing_select(column)
            if select is None:
                continue
            visible = _visible_scopes(select, scopes)
            if not visible:
                continue
            issues.extend(_resolve_column(column, visible))

        # Deduplicate by message: one mistake repeated across SELECT, GROUP BY and
        # ORDER BY is one thing for the model to fix, not three.
        unique: dict[str, ValidationIssue] = {}
        for issue in issues:
            unique.setdefault(issue.message, issue)
        return list(unique.values())

    # -----------------------------------------------------------------------
    # Business rules
    # -----------------------------------------------------------------------

    # -----------------------------------------------------------------------
    # Grain
    # -----------------------------------------------------------------------

    def _validate_grain(self, tree: exp.Expression) -> list[ValidationIssue]:
        """Reject joins that multiply rows under an aggregate, and date-role mixes.

        This is the only check here that guards against a query which is
        syntactically perfect, passes ``EXPLAIN``, executes in milliseconds and
        returns a *wrong number*. Every other rule catches SQL that fails or
        refers to something that does not exist; a fan-out join fails silently,
        which is why it has to be decided structurally before execution rather
        than noticed afterwards.

        The rule, per ``SELECT``:

        1. Collect the equality keys tying each pair of sources together, from
           ``ON``, from ``USING``, and from top-level ``WHERE`` conjunctions --
           models write implicit joins, and a join predicate parked in the
           ``WHERE`` clause constrains rows exactly as an ``ON`` does.
        2. A pair is **grain-safe** when those keys cover the full declared grain
           of at least one side: that side then contributes at most one row per
           row of the other, so nothing is multiplied.
        3. When neither grain is covered, both sides are multiplied. That is only
           an *error* if an additive measure from a multiplied source is summed,
           so the aggregate list decides it. ``SELECT DISTINCT`` over a
           many-to-many join is legitimate and stays legitimate.

        Equalities under an ``OR`` are ignored on purpose: they do not constrain
        every row, so treating them as join keys would declare an unsafe join
        safe -- the one direction this check must never fail in.
        """
        issues: list[ValidationIssue] = []
        for select in tree.find_all(exp.Select):
            issues.extend(self._grain_issues_for_select(select))
        return issues

    def _grain_issues_for_select(self, select: exp.Select) -> list[ValidationIssue]:
        joins = select.args.get("joins") or []
        if not joins:
            return []

        sources = _base_table_sources(select)
        if len(sources) < 2:
            return []

        links = _join_links(select, sources)
        issues: list[ValidationIssue] = []
        issues.extend(self._cross_join_issues(joins, sources, links))
        issues.extend(self._role_mismatch_issues(links))

        summed = _additively_aggregated_columns(select, sources, self.grains)
        for link in links:
            left_grain = self.grains.grain(link.left_table)
            right_grain = self.grains.grain(link.right_table)
            if left_grain is None or right_grain is None:
                continue

            # Which side is multiplied is decided by the *other* side's grain,
            # and getting that backwards is what this used to do: covering one
            # side's grain was treated as making the whole join safe. It does
            # not. Pinning the right side to one row per key protects the left
            # from being duplicated -- and leaves the left free to match many
            # right rows, so it is the *right* side's rows that get multiplied.
            #
            # In a star that difference never showed, because the covered side
            # is always a dimension and dimensions declare no additive measures.
            # It showed the moment a second fact joined a first: fact_fuel is one
            # row per (tail, day) and fact_flights is several, so joining them on
            # the tail and the date covers fuel's grain, passes the old check,
            # and inflates SUM(fuel_litres) by 64%.
            left_multiplied = not right_grain.is_covered_by(
                link.right_keys, set(link.right_bounded)
            )
            right_multiplied = not left_grain.is_covered_by(
                link.left_keys, set(link.left_bounded)
            )
            if not left_multiplied and not right_multiplied:
                continue

            multiplied = [
                alias
                for alias, is_multiplied in (
                    (link.left_alias, left_multiplied),
                    (link.right_alias, right_multiplied),
                )
                if is_multiplied and alias in summed
            ]
            if not multiplied:
                # Rows are multiplied, but nothing additive is being summed over
                # them -- a DISTINCT listing or a pure dimension lookup. Fan-out
                # is only wrong when something counts the extra rows.
                continue

            measures = ", ".join(sorted({summed[alias] for alias in multiplied}))
            written_keys = ", ".join(sorted(link.left_keys)) or "nothing"
            inflated = ", ".join(
                sorted(
                    {
                        link.left_table if alias == link.left_alias else link.right_table
                        for alias in multiplied
                    }
                )
            )
            issues.append(
                ValidationIssue(
                    IssueCode.GRAIN_FANOUT,
                    f"Joining '{link.left_table}' to '{link.right_table}' on {written_keys} "
                    f"multiplies the rows of '{inflated}': {link.left_table} is one row per "
                    f"{', '.join(sorted(left_grain.key_columns))} and {link.right_table} is one row "
                    f"per {', '.join(sorted(right_grain.key_columns))}, and the join keys do not "
                    f"pin it to one row. The aggregate over {measures} therefore counts rows more "
                    "than once. Aggregate each table separately before joining, or join on the "
                    "full grain of the table whose measures you are summing.",
                )
            )
        return issues

    def _cross_join_issues(
        self, joins: list[exp.Expression], sources: dict[str, str], links: list[_JoinLink]
    ) -> list[ValidationIssue]:
        """A join with no join condition anywhere between two known tables.

        Reported separately from fan-out because there is nothing to repair
        towards: the model did not write a bad key, it wrote no key at all.

        A comma join whose condition sits in the ``WHERE`` clause is not this --
        it is an ordinary join written in older syntax, and ``links`` already
        carries its keys. Only a table that no equality reaches is flagged here,
        or the same mistake would be reported twice under two different codes.
        """
        linked = {alias for link in links for alias in (link.left_alias, link.right_alias)}
        issues: list[ValidationIssue] = []
        for join in joins:
            target = join.this
            if not isinstance(target, exp.Table):
                continue
            alias = (target.alias or target.name).lower()
            if alias not in sources or self.grains.grain(sources[alias]) is None:
                continue
            if join.args.get("on") or join.args.get("using") or alias in linked:
                continue
            issues.append(
                ValidationIssue(
                    IssueCode.UNRELATED_JOIN,
                    f"'{sources[alias]}' is joined without a join condition, which pairs every row "
                    "with every other row. Add the declared key, or remove the table.",
                )
            )
        return issues

    def _role_mismatch_issues(self, links: list[_JoinLink]) -> list[ValidationIssue]:
        """Equating two date columns that denote different kinds of instant.

        ``fmcg_sales.date = weekly_modeling_data.week`` parses, plans and runs.
        It also matches only Mondays, silently discarding six sevenths of the
        sales -- a wrong answer that looks like a right one. The declared role of
        each column (``day`` vs ``week_start``) is what makes it decidable
        without executing anything.
        """
        issues: list[ValidationIssue] = []
        for link in links:
            for left_column, right_column in sorted(link.column_pairs):
                left_role = self.grains.role(link.left_table, left_column)
                right_role = self.grains.role(link.right_table, right_column)
                if left_role and right_role and left_role != right_role:
                    issues.append(
                        ValidationIssue(
                            IssueCode.GRAIN_KEY_MISMATCH,
                            f"'{link.left_table}.{left_column}' is a {left_role} and "
                            f"'{link.right_table}.{right_column}' is a {right_role}; equating them "
                            "keeps only the rows where the two happen to coincide and silently drops "
                            "the rest. Bridge through dim_calendar instead.",
                        )
                    )
        return issues

    @staticmethod
    def _validate_metric_contract(
        question: str, tree: exp.Expression, metrics: MetricRegistry
    ) -> list[ValidationIssue]:
        """Any certified metric named in the question must use its registry formula.

        Driven entirely by :class:`~semantic_query_engine.domain.registry.MetricRegistry` --
        the formula, its trigger keywords, and the columns/operators it requires all
        come from ``data/semantic/semantic_layer.json``, not a second hand-written
        rule per metric.
        """
        columns = {column.name.lower() for column in tree.find_all(exp.Column)}
        issues: list[ValidationIssue] = []
        for metric in metrics.referenced_by(question):
            uses_metric_alias = metric.name.lower() in columns or any(
                token in columns for token in metric.name.lower().split("_")
            )
            if not (uses_metric_alias or metric.is_satisfied_by(columns)):
                issues.append(
                    ValidationIssue(
                        IssueCode.METRIC_CONTRACT,
                        f"'{metric.name}' queries must use the certified formula ({metric.formula}) "
                        f"or reference the {metric.name} field.",
                    )
                )
        return issues

    def _validate_requested_entities(
        self, question: str, sql: str, params: list[Any]
    ) -> list[ValidationIssue]:
        """Prevent a repair or fallback from silently dropping an explicit filter.

        A dropped filter is the most dangerous failure this system can have: the
        query succeeds, the numbers look plausible, and they answer a different
        question than the one asked. Checks both the SQL text and any bound ``?``
        parameter values, since fallback templates pass literals like the SKU or
        region through bound params rather than inlining them.

        Which entities count as "requested" comes from the registries -- never from
        a literal list of region names, which is what this check used to carry and
        which only worked because the canonical values happened to contain the bare
        word the user typed.

        The planner extracts entities too, but for a different purpose (routing to
        an archetype) and over a wider set of keys -- its dict also carries
        classification tokens like ``metric`` and ``timeframe``, which are not
        literals that could appear in a filter. Deriving the filterable subset here
        keeps that policy where it is enforced; both extractions read the same
        registries, so neither can drift from the semantic layer.
        """
        param_text = " ".join(str(p).lower() for p in params)
        haystack = f"{sql.lower()} {param_text}"

        issues: list[ValidationIssue] = []
        for name, value in sorted(self._extract_entities(question).items()):
            if not value or value.lower() in haystack:
                continue
            issues.append(
                ValidationIssue(
                    IssueCode.DROPPED_FILTER,
                    f"Requested {name.replace('_', ' ')} {value} is missing from the SQL filter.",
                )
            )
        return issues

    def _extract_entities(self, question: str) -> dict[str, str | None]:
        """Entities the question scopes to, derived from the registries.

        A dimension is only treated as a filter when the question names **exactly
        one** of its values. Naming two or more ("compare North and South") is a
        comparison, for which grouping by the dimension with no literal filter is
        the correct SQL -- demanding a filter there would reject a good query.
        """
        lower = question.lower()
        entities: dict[str, str | None] = {
            name: self.identifiers.find(name, question) for name in self.identifiers.names()
        }
        for dimension in self.dimensions.dimensions():
            named = self.dimensions.match_all(dimension, lower)
            entities[dimension] = named[0] if len(named) == 1 else None
        return entities


# ---------------------------------------------------------------------------
# Scope construction
# ---------------------------------------------------------------------------


def _base_table_sources(select: exp.Select) -> dict[str, str]:
    """Alias -> physical table name, for the base tables this ``SELECT`` reads.

    CTEs and subqueries are deliberately excluded: their grain is whatever their
    own aggregation produced, which is not declared anywhere and cannot be
    inferred. Aggregating each side of a join in a subquery first is in fact the
    standard *fix* for a fan-out, so treating an unknown grain as unsafe would
    reject the correct rewrite.
    """
    sources: dict[str, str] = {}

    def add(node: exp.Expression | None) -> None:
        if isinstance(node, exp.Table) and node.name:
            sources[(node.alias or node.name).lower()] = node.name.lower()

    from_clause = _from_clause(select)
    if from_clause is not None:
        add(from_clause.this)
    for join in select.args.get("joins") or []:
        add(join.this)
    return sources


def _constraining_equalities(select: exp.Select) -> list[exp.EQ]:
    """Every equality that holds for all rows of this ``SELECT``.

    An equality nested under an ``OR`` does not constrain every row, so counting
    it as a join key would let an unsafe join be declared safe -- the one
    direction this analysis must never be wrong in. Such equalities are dropped
    rather than treated as keys.
    """
    predicates: list[exp.Expression] = []
    for join in select.args.get("joins") or []:
        on_clause = join.args.get("on")
        if isinstance(on_clause, exp.Expression):
            predicates.append(on_clause)
    where = select.args.get("where")
    if isinstance(where, exp.Where) and isinstance(where.this, exp.Expression):
        predicates.append(where.this)

    equalities: list[exp.EQ] = []
    for predicate in predicates:
        for node in predicate.find_all(exp.EQ):
            if not _under_disjunction(node, predicate):
                equalities.append(node)
    return equalities


def _under_disjunction(node: exp.Expression, root: exp.Expression) -> bool:
    parent = cast("exp.Expression | None", node.parent)
    while parent is not None:
        if isinstance(parent, (exp.Or, exp.Not)):
            return True
        if parent is root:
            return False
        parent = cast("exp.Expression | None", parent.parent)
    return False


def _join_links(select: exp.Select, sources: dict[str, str]) -> list[_JoinLink]:
    """Group the join keys of one ``SELECT`` by the pair of tables they connect."""
    pairs: dict[tuple[str, str], set[tuple[str, str]]] = {}
    # alias pair -> {alias: columns of that alias bounded against the other side}
    bounded: dict[tuple[str, str], dict[str, set[str]]] = {}

    def record(left: str, left_column: str, right: str, right_column: str) -> None:
        # Keyed on the alias pair in a fixed order so `a JOIN b` and `b JOIN a`
        # accumulate into one link rather than two half-populated ones.
        if left == right or left not in sources or right not in sources:
            return
        if left > right:
            left, right = right, left
            left_column, right_column = right_column, left_column
        pairs.setdefault((left, right), set()).add((left_column, right_column))

    def record_bound(left: str, left_column: str, right: str, right_column: str) -> None:
        """Note a column constrained by an inequality against the other side.

        Recorded per alias rather than as a pair: what matters downstream is
        whether *both ends* of one table's declared validity window are
        constrained, not which column of the other side did the constraining.
        """
        if left == right or left not in sources or right not in sources:
            return
        key = (left, right) if left < right else (right, left)
        slots = bounded.setdefault(key, {})
        slots.setdefault(left, set()).add(left_column)
        slots.setdefault(right, set()).add(right_column)

    for equality in _constraining_equalities(select):
        left, right = equality.this, equality.expression
        if isinstance(left, exp.Column) and isinstance(right, exp.Column):
            if left.table and right.table:
                record(left.table.lower(), left.name.lower(), right.table.lower(), right.name.lower())

    for comparison in _constraining_comparisons(select):
        left = _qualified_column(comparison.this)
        right = _qualified_column(comparison.expression)
        if left is not None and right is not None:
            record_bound(left[0], left[1], right[0], right[1])

    # USING(col) constrains the same column name on both sides. It names no
    # table, so it applies to the pair the join itself connects.
    join_sources = [
        (join.this.alias or join.this.name).lower()
        for join in select.args.get("joins") or []
        if isinstance(join.this, exp.Table)
    ]
    from_clause = _from_clause(select)
    base = (
        (from_clause.this.alias or from_clause.this.name).lower()
        if from_clause is not None and isinstance(from_clause.this, exp.Table)
        else None
    )
    for join in select.args.get("joins") or []:
        if not isinstance(join.this, exp.Table):
            continue
        right = (join.this.alias or join.this.name).lower()
        using = [u.name.lower() for u in (join.args.get("using") or [])]
        if not using:
            continue
        partners = [alias for alias in ([base] if base else []) + join_sources if alias != right]
        for left in partners[:1]:
            for column in using:
                record(left, column, right, column)

    # A join that has an ON clause but contributed no usable key -- every
    # equality in it sat under an OR -- is recorded as a link with no keys at
    # all rather than dropped. Dropping it would mean "no keys found" silently
    # read as "nothing to check", and the fan-out would pass.
    for join in select.args.get("joins") or []:
        on_clause = join.args.get("on")
        if not isinstance(join.this, exp.Table) or not isinstance(on_clause, exp.Expression):
            continue
        right = (join.this.alias or join.this.name).lower()
        referenced = {
            column.table.lower()
            for column in on_clause.find_all(exp.Column)
            if column.table and column.table.lower() in sources
        }
        for left in sorted(referenced - {right}):
            key = (left, right) if left < right else (right, left)
            pairs.setdefault(key, set())

    return [
        _JoinLink(
            left_alias=left,
            right_alias=right,
            left_table=sources[left],
            right_table=sources[right],
            column_pairs=frozenset(columns),
            left_bounded=frozenset(bounded.get((left, right), {}).get(left, set())),
            right_bounded=frozenset(bounded.get((left, right), {}).get(right, set())),
        )
        for (left, right), columns in pairs.items()
    ]


def _qualified_column(node: exp.Expression) -> tuple[str, str] | None:
    """(alias, column) for a table-qualified column, seeing through a CAST.

    A date comparison is almost always written ``CAST(f.flight_date AS DATE) >=
    CAST(ac.valid_from AS DATE)`` -- the generator prompt instructs it -- so an
    operand check that insisted on a bare Column saw no bound at all and
    rejected the one join that gets the right answer. Only a wrapper with a
    single column inside is unwrapped: an expression over two columns pins
    neither of them.
    """
    while isinstance(node, (exp.Cast, exp.Paren)):
        node = node.this
    if isinstance(node, exp.Column) and node.table:
        return node.table.lower(), node.name.lower()
    return None


def _constraining_comparisons(select: exp.Select) -> list[exp.Binary]:
    """Every inequality between two columns that holds for all rows.

    The same disjunction rule as :func:`_constraining_equalities`, and for the
    same reason: a bound under an ``OR`` does not hold for every row, so
    treating it as closing a validity window would declare an unsafe join safe.
    """
    predicates: list[exp.Expression] = []
    for join in select.args.get("joins") or []:
        on_clause = join.args.get("on")
        if isinstance(on_clause, exp.Expression):
            predicates.append(on_clause)
    where = select.args.get("where")
    if isinstance(where, exp.Where) and isinstance(where.this, exp.Expression):
        predicates.append(where.this)

    comparisons: list[exp.Binary] = []
    for predicate in predicates:
        for node in predicate.find_all(exp.GT, exp.GTE, exp.LT, exp.LTE):
            if not _under_disjunction(cast("exp.Expression", node), predicate):
                comparisons.append(node)
    return comparisons


def _additively_aggregated_columns(
    select: exp.Select, sources: dict[str, str], grains: GrainRegistry
) -> dict[str, str]:
    """Alias -> one additive measure of that source being summed in this SELECT.

    Only additive measures count. ``COUNT(DISTINCT ...)`` is immune to row
    multiplication by construction, and a level measure like ``closing_stock``
    is already wrong to sum across dates whether or not a join fanned it out --
    a different error, and not one this check should claim credit for.
    """
    found: dict[str, str] = {}
    for aggregate in select.find_all(exp.Sum, exp.Avg, exp.Count):
        if isinstance(aggregate, exp.Count) and aggregate.args.get("distinct"):
            continue
        for column in aggregate.find_all(exp.Column):
            alias = column.table.lower() if column.table else _sole_owner(column.name, sources, grains)
            if alias is None or alias not in sources:
                continue
            grain = grains.grain(sources[alias])
            if grain and column.name.lower() in grain.additive_measures:
                found.setdefault(alias, column.name.lower())
    return found


def _sole_owner(column: str, sources: dict[str, str], grains: GrainRegistry) -> str | None:
    """The one source owning ``column``, or None when zero or several do.

    An unqualified column that several sources provide is already reported as
    ambiguous by the column checks; attributing it to a guess here would produce
    a second, worse-worded rejection for the same mistake.
    """
    owners = [alias for alias, table in sources.items() if grains.owns_column(table, column)]
    return owners[0] if len(owners) == 1 else None


def _cte_names(tree: exp.Expression) -> set[str]:
    return {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}


def _from_clause(select: exp.Select) -> exp.From | None:
    """This SELECT's own FROM clause, across sqlglot's two spellings of the arg key.

    sqlglot renamed the argument from ``from`` to ``from_`` in v26; the project
    supports ``sqlglot>=25``, so both are accepted. ``select.args`` is read rather
    than ``select.find(exp.From)`` deliberately -- ``find`` descends into nested
    subqueries and would attribute an inner FROM to the outer scope, which is
    exactly the confusion this module exists to prevent.
    """
    clause = select.args.get("from_") or select.args.get("from")
    return clause if isinstance(clause, exp.From) else None


def _output_columns(select: exp.Expression) -> tuple[frozenset[str], bool]:
    """The output column names of a SELECT, and whether they are indeterminate.

    ``SELECT *`` expands to whatever the source has, which cannot be known without
    resolving the source first, so such a projection is reported as opaque and its
    consumers skip column resolution rather than guessing.
    """
    if not isinstance(select, exp.Select):
        return frozenset(), True
    names: set[str] = set()
    for projection in select.expressions:
        if isinstance(projection, (exp.Star, exp.Column)) and projection.is_star:
            return frozenset(), True
        alias = projection.alias_or_name
        if not alias:
            return frozenset(), True
        names.add(alias.lower())
    return frozenset(names), False


def _cte_columns(
    tree: exp.Expression, schema: dict[str, set[str]]
) -> dict[str, tuple[frozenset[str], bool]]:
    """Output columns for each CTE, in definition order so later CTEs see earlier ones."""
    resolved: dict[str, tuple[frozenset[str], bool]] = {}
    for cte in tree.find_all(exp.CTE):
        name = cte.alias_or_name.lower()
        # An explicit column list -- WITH t(a, b) AS (...) -- overrides the
        # projection, and is authoritative even when the body is SELECT *.
        alias = cte.args.get("alias")
        declared = [c.name.lower() for c in alias.columns] if alias is not None else []
        if declared:
            resolved[name] = (frozenset(declared), False)
        else:
            resolved[name] = _output_columns(cte.this)
    return resolved


def _build_scope(
    select: exp.Select,
    schema: dict[str, set[str]],
    cte_columns: dict[str, tuple[frozenset[str], bool]],
) -> _Scope:
    """Collect the sources, output aliases and join keys visible inside one SELECT."""
    sources: list[_Source] = []

    def add(node: exp.Expression) -> None:
        if isinstance(node, exp.Table):
            name = node.name.lower()
            alias = (node.alias or node.name).lower()
            if name in schema:
                sources.append(_Source(alias=alias, name=name, columns=frozenset(schema[name])))
            elif name in cte_columns:
                columns, opaque = cte_columns[name]
                sources.append(_Source(alias=alias, name=name, columns=columns, opaque=opaque))
            else:
                # An unknown table is already reported by the allow-list check;
                # treat it as opaque here so it doesn't also produce a cascade of
                # "unknown column" errors for every column it legitimately provides.
                sources.append(_Source(alias=alias, name=name, columns=frozenset(), opaque=True))
        elif isinstance(node, exp.Subquery):
            columns, opaque = _output_columns(node.this)
            alias = node.alias_or_name.lower()
            sources.append(_Source(alias=alias, name=alias or "subquery", columns=columns, opaque=opaque))

    from_clause = _from_clause(select)
    if from_clause is not None:
        add(from_clause.this)
    for join in select.args.get("joins") or []:
        add(join.this)

    using_columns: set[str] = set()
    for join in select.args.get("joins") or []:
        for using in join.args.get("using") or []:
            using_columns.add(using.name.lower())
        if join.args.get("kind") == "NATURAL" or join.args.get("natural"):
            # A natural join merges every shared column; none of them are ambiguous.
            using_columns |= set().union(*(s.columns for s in sources)) if sources else set()

    derived = {
        projection.alias.lower()
        for projection in select.expressions
        if isinstance(projection, exp.Alias) and projection.alias
    }

    return _Scope(
        sources=tuple(sources),
        derived=frozenset(derived),
        using_columns=frozenset(using_columns),
    )


def _enclosing_select(node: exp.Expression) -> exp.Select | None:
    """The nearest SELECT that encloses ``node`` -- the scope its names resolve in."""
    parent = cast("exp.Expression | None", node.parent)
    while parent is not None:
        if isinstance(parent, exp.Select):
            return parent
        parent = cast("exp.Expression | None", parent.parent)
    return None


def _visible_scopes(select: exp.Select, scopes: dict[int, _Scope]) -> list[_Scope]:
    """``select``'s own scope first, then each enclosing one.

    Outer scopes are included so a correlated subquery -- which legitimately refers
    to a column of the query that contains it -- is not reported as an unknown
    column. Ambiguity is only ever judged within the innermost scope, since an
    inner name shadows an outer one rather than conflicting with it.
    """
    visible: list[_Scope] = []
    node: exp.Expression | None = select
    while node is not None:
        scope = scopes.get(id(node))
        if scope is not None:
            visible.append(scope)
        node = cast("exp.Expression | None", node.parent)
    return visible


def _resolve_column(column: exp.Column, visible: list[_Scope]) -> list[ValidationIssue]:
    """Check one column reference against the scopes it can legally resolve in."""
    # `s.*` parses as a Column whose name is the star, not as a name to resolve:
    # it stands for every column the source has, so there is nothing to check and
    # no source can fail to provide it. Without this it is looked up as a column
    # literally named "*" and every qualified star is rejected.
    if isinstance(column.this, exp.Star):
        return []

    name = column.name.lower()
    qualifier = column.table.lower() if column.table else ""

    if qualifier:
        source = next(
            (s for s in (found for sc in visible for found in [sc.source_for_alias(qualifier)]) if s),
            None,
        )
        if source is None:
            return [
                ValidationIssue(
                    IssueCode.UNKNOWN_ALIAS,
                    f"Unknown table alias '{column.table}' for column '{column.name}'.",
                )
            ]
        if source.opaque or name in source.columns:
            return []
        return [
            ValidationIssue(
                IssueCode.UNKNOWN_COLUMN,
                f"Unknown column '{column.name}' on '{source.name}'.",
            )
        ]

    # Unqualified: an output alias of the SELECT it sits in (valid in GROUP BY,
    # HAVING and ORDER BY) or a column of one of the sources in scope.
    for index, current in enumerate(visible):
        if name in current.derived:
            return []
        if current.has_opaque_source():
            return []
        providers = [s for s in current.sources if name in s.columns]
        if not providers:
            continue
        if index == 0 and len(providers) > 1 and name not in current.using_columns:
            return [
                ValidationIssue(
                    IssueCode.AMBIGUOUS_COLUMN,
                    f"Ambiguous column '{column.name}': provided by "
                    f"{', '.join(sorted(s.name for s in providers))}. Qualify it with a table alias.",
                )
            ]
        return []

    return [ValidationIssue(IssueCode.UNKNOWN_COLUMN, f"Unknown column '{column.name}'.")]

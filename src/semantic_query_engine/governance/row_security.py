"""Row-level security by predicate injection into the parsed query.

The rule this module enforces: a principal scoped by a policy may read a row only
if that row falls inside their grants, whatever SQL they got the model to write.

Why the tree and not the text
-----------------------------
For the same reason the validator works on the tree. Appending ``AND region IN
(...)`` to the query string is defeated by a trailing comment, by an existing
``ORDER BY``, and by a ``UNION``; rewriting the parsed query cannot be defeated
by formatting. It is also the only way to reach the places that actually leak.

Four properties, each of which is a way this is usually got wrong:

1. **Every ``SELECT``, not just the outermost one.** ``WITH x AS (SELECT * FROM
   fmcg_sales) SELECT * FROM x`` reads the whole fact table through a CTE whose
   body the outer predicate never touches. Each ``SELECT`` in the tree is scoped
   against its own base tables, so a nested read is restricted where the read
   happens.
2. **Facts are scoped through their key, not through a join.** "Filter on
   ``dim_store.region`` when the query joins ``dim_store``" leaks every query
   that does not join it. See :mod:`semantic_query_engine.governance.policy`.
   When the scoping dimension is slowly-changing, the semi-join is also closed on
   its validity window: "did this key ever belong to a granted scope" is a wider
   question than "did it belong to one when this row happened", and an airframe
   that transfers operator otherwise hands its whole fuel history to both of
   them. Declared with ``valid_from`` / ``valid_to`` / ``as_of`` on the
   ``through`` block, and the window is half-open so the changeover date belongs
   to exactly one scope.
3. **No grants means no rows.** A principal granted an empty list gets ``FALSE``,
   not an absent predicate. An empty ``IN`` list is a syntax error in most
   dialects, and the reflex fix -- "skip the predicate when there is nothing to
   filter on" -- turns the least privileged principal into the most privileged.
4. **The result is re-derived, not asserted.** :func:`scope_breaches` reads the
   final SQL back and reports any governed table that reached execution without
   its predicate. It deliberately does not trust the bookkeeping from
   :func:`apply_row_policies`, on the same principle as
   ``report.executed_unsafely()``: the interesting question is what ran, not what
   the code believed it did.

An outer join whose null-extended side is governed is narrowed by a ``WHERE``
predicate, which makes the query *more* restrictive than the model wrote. That is
a correctness wart the caller can see in the applied-policy list, and it is the
safe direction; the alternative is deciding per join whether to push the
predicate into the ``ON`` clause, which is a place to introduce a leak.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from sqlglot import exp, parse_one

from semantic_query_engine.governance.policy import GovernancePolicy, ScopedTable
from semantic_query_engine.governance.principals import Principal


@dataclass(frozen=True)
class AppliedPolicy:
    """One predicate that was injected, for the trace, the result and the audit log."""

    policy: str
    table: str
    alias: str
    predicate: str

    def to_dict(self) -> dict[str, str]:
        return {
            "policy": self.policy,
            "table": self.table,
            "alias": self.alias,
            "predicate": self.predicate,
        }


def _scope_predicate(
    alias: str, scope: ScopedTable, values: tuple[str, ...]
) -> exp.Expression:
    """The predicate restricting one aliased table to ``values``.

    ``values`` empty is not a special case to skip -- it is a principal who has
    been granted nothing, and the honest translation of that is ``FALSE``.
    """
    if not values:
        return exp.false()

    literals = [exp.Literal.string(value) for value in values]
    if scope.is_semijoin:
        inner_filter = exp.column(scope.through_filter_column, table=scope.through_table)
        granted = exp.In(this=inner_filter, expressions=literals)

        if scope.is_temporal:
            # A correlated EXISTS rather than an IN, because the dimension row
            # has to be matched on the key *and* on the window the fact row
            # falls in, and an uncorrelated subquery cannot see the fact's date.
            #
            # The window is half-open -- ``>= valid_from AND < valid_to`` -- which
            # is what the dimension's own grain declares and the only reading
            # under which a transfer date belongs to exactly one operator.
            # Closing it at both ends would put the changeover day in both
            # scopes, which is an over-grant of precisely the kind this predicate
            # exists to remove.
            outer_key = exp.column(scope.key, table=alias)
            as_of = exp.column(scope.as_of, table=alias)
            condition = exp.and_(
                exp.EQ(
                    this=exp.column(scope.through_column, table=scope.through_table),
                    expression=outer_key,
                ),
                granted,
                exp.GTE(
                    this=as_of,
                    expression=exp.column(scope.through_valid_from, table=scope.through_table),
                ),
                exp.LT(
                    this=as_of,
                    expression=exp.column(scope.through_valid_to, table=scope.through_table),
                ),
            )
            return exp.Exists(
                this=exp.select(exp.Literal.number(1))
                .from_(scope.through_table)
                .where(condition)
                .subquery()
                .this
            )

        inner_key = exp.column(scope.through_column, table=scope.through_table)
        subquery = (
            exp.select(inner_key)
            .from_(scope.through_table)
            .where(granted)
            .subquery()
        )
        return exp.In(this=exp.column(scope.key, table=alias), query=subquery)
    return exp.In(this=exp.column(scope.key, table=alias), expressions=literals)


def _base_tables(select: exp.Select) -> dict[str, str]:
    """Alias -> physical table name for the tables this ``SELECT`` reads directly.

    A CTE reference looks like a table here and is filtered out by the caller,
    which holds the set of CTE names: the CTE's own body is a ``SELECT`` in the
    same tree and is scoped there, so scoping the reference as well would demand
    a ``region`` column the CTE may not project.
    """
    tables: dict[str, str] = {}

    def add(node: exp.Expression | None) -> None:
        if isinstance(node, exp.Table) and node.name:
            tables[(node.alias or node.name).lower()] = node.name.lower()

    from_clause = select.args.get("from_") or select.args.get("from")
    if isinstance(from_clause, exp.From):
        add(from_clause.this)
    for join in select.args.get("joins") or []:
        add(join.this)
    return tables


def _cte_names(tree: exp.Expression) -> set[str]:
    return {
        cte.alias_or_name.lower()
        for cte in tree.find_all(exp.CTE)
        if cte.alias_or_name
    }


def required_predicates(
    tree: exp.Expression, principal: Principal, policy: GovernancePolicy
) -> list[tuple[exp.Select, AppliedPolicy, exp.Expression]]:
    """Every predicate this principal's grants require of this query.

    Shared by :func:`apply_row_policies` and :func:`scope_breaches` so the two
    cannot disagree about what "scoped" means -- the injector and the check that
    the injector worked derive the requirement from one function, and only the
    *observation* differs between them.
    """
    required: list[tuple[exp.Select, AppliedPolicy, exp.Expression]] = []
    if principal.unrestricted or not policy.row_policies:
        return required

    cte_names = _cte_names(tree)
    for select in tree.find_all(exp.Select):
        for alias, table in _base_tables(select).items():
            if table in cte_names:
                continue
            for row_policy, scope in policy.policies_for(table):
                if not principal.is_scoped_by(row_policy.grant_key):
                    continue
                predicate = _scope_predicate(
                    alias, scope, principal.grant_values(row_policy.grant_key)
                )
                required.append(
                    (
                        select,
                        AppliedPolicy(
                            policy=row_policy.name,
                            table=table,
                            alias=alias,
                            predicate=predicate.sql(dialect="duckdb"),
                        ),
                        predicate,
                    )
                )
    return required


def _conjuncts(select: exp.Select) -> set[str]:
    """The top-level ``AND`` terms of this ``SELECT``'s ``WHERE``, as SQL text.

    Only top-level terms count. A predicate that survived into the query under an
    ``OR`` does not restrict every row, so treating it as present would let
    ``WHERE <injected> OR 1=1`` read as scoped -- the one direction this check
    must never be wrong in, exactly as with the validator's join-key analysis.
    """
    where = select.args.get("where")
    if not isinstance(where, exp.Where) or not isinstance(where.this, exp.Expression):
        return set()
    root = where.this
    terms = root.flatten() if isinstance(root, exp.And) else [root]
    return {term.sql(dialect="duckdb") for term in terms}


def apply_row_policies(
    tree: exp.Expression, principal: Principal, policy: GovernancePolicy
) -> tuple[exp.Expression, list[AppliedPolicy]]:
    """Return ``tree`` with this principal's row predicates injected.

    The input tree is never mutated: the validator holds it for the checks that
    run either side of this call, and a caller that handed in a tree and got back
    a silently rewritten one would have no way to report what the model wrote.
    """
    required = required_predicates(tree, principal, policy)
    if not required:
        return tree, []

    # The predicates were located against the original tree, so they are
    # re-derived against the copy: node identity does not survive ``copy()``.
    working = tree.copy()
    applied: list[AppliedPolicy] = []
    for select, record, predicate in required_predicates(working, principal, policy):
        # Skip a predicate this SELECT already carries as a top-level term.
        # Without it the operation is not idempotent: the repair loop can hand
        # the validator SQL that has already been governed, and a predicate that
        # stacked on every pass would grow the query on each round trip --
        # reaching a plan the warehouse refuses by doing the right thing twice.
        # Found by the property test in tests/unit/test_validator_properties.py.
        if record.predicate in _conjuncts(select):
            continue
        select.where(predicate, copy=False)
        applied.append(record)
    return working, applied


def scope_breaches(sql: str, principal: Principal, policy: GovernancePolicy) -> list[str]:
    """Governed tables that this SQL reads without the predicate they require.

    Re-parsed from the SQL string a caller is about to execute (or already has),
    not from the tree the injector produced, so it catches a rewrite that lost a
    predicate on its way to the warehouse as well as one that never got it. An
    empty list is the only acceptable result for a restricted principal; a
    non-empty one is a breach, not a metric.
    """
    try:
        tree = cast(exp.Expression, parse_one(sql, read="duckdb"))
    except Exception:
        # Unparseable SQL never reaches execution -- the validator rejects it
        # first -- but reporting "no breaches" for something this function could
        # not read would be a false clearance.
        return ["unparseable SQL could not be checked for row policies"]

    breaches: list[str] = []
    for select, record, predicate in required_predicates(tree, principal, policy):
        if predicate.sql(dialect="duckdb") not in _conjuncts(select):
            breaches.append(
                f"{record.table} (as {record.alias}) was read without the "
                f"{record.policy} predicate"
            )
    return breaches


__all__ = [
    "AppliedPolicy",
    "apply_row_policies",
    "required_predicates",
    "scope_breaches",
]

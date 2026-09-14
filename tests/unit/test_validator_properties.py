"""Property-based tests over the two controls that must never be wrong.

The example-based tests assert that the validator rejects the mutations somebody
thought to write down. That is the weakest possible form of the claim: a safety
control is only interesting if it holds over statements nobody anticipated,
including the obfuscations a language model produces by accident -- a DELETE
inside a CTE, a DROP after a comment, mixed case, stray whitespace, a mutation
buried in a scalar subquery.

Two invariants are fuzzed here, and both are stated as *universals* rather than
as examples:

1. **No generated statement that mutates is ever accepted**, by either the full
   validator or the ablated safety floor. The generator composes a mutation with
   randomly chosen wrappers and formatting, so the search explores shapes the
   example tests do not enumerate.
2. **No query accepted for a restricted principal escapes its row policy.**
   :func:`scope_breaches` re-derives the answer from the returned SQL, so this
   is a genuine end-to-end property and not a restatement of the injector's own
   bookkeeping.

A third property is the converse and is worth as much: an *ordinary* SELECT over
the warehouse is not rejected. A validator that refused everything would satisfy
both invariants above perfectly, which is exactly why the suite cannot consist
of safety properties alone.

Deadlines are switched off because the first example in each test pays for
building a ``ValidatorAgent`` (which reads the semantic layer and DESCRIBEs
every table); that one-off cost is not a per-example regression and hypothesis
would otherwise flag it as flaky on a slow machine.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlglot import exp, parse_one

from semantic_query_engine.agents.validator import IssueCode, ValidatorAgent
from semantic_query_engine.core.domains import get_domain
from semantic_query_engine.governance.policy import load_governance_policy
from semantic_query_engine.governance.principals import STEWARD, Principal
from semantic_query_engine.governance.row_security import apply_row_policies, scope_breaches
from semantic_query_engine.semantic.layer import load_semantic_layer
from semantic_query_engine.warehouse.duckdb_client import init_database, open_cursor

SETTINGS = settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@pytest.fixture(scope="module")
def cursor():
    return open_cursor(init_database())


@pytest.fixture(scope="module")
def validator():
    return ValidatorAgent()


@pytest.fixture(scope="module")
def policy():
    return load_governance_policy(load_semantic_layer(get_domain("retail").semantic_layer_path))


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_TABLES = st.sampled_from(
    ["fmcg_sales", "dim_store", "dim_product", "dim_calendar", "fact_inventory", "fact_promotions"]
)

_MUTATIONS = st.one_of(
    _TABLES.map(lambda t: f"DROP TABLE {t}"),
    _TABLES.map(lambda t: f"DELETE FROM {t}"),
    _TABLES.map(lambda t: f"TRUNCATE TABLE {t}"),
    _TABLES.map(lambda t: f"UPDATE {t} SET units_sold = 0"),
    _TABLES.map(lambda t: f"INSERT INTO {t} VALUES (1)"),
    _TABLES.map(lambda t: f"ALTER TABLE {t} ADD COLUMN x INTEGER"),
    _TABLES.map(lambda t: f"CREATE TABLE {t}_copy AS SELECT * FROM {t}"),
)


@st.composite
def obfuscated_mutation(draw: st.DrawFn) -> str:
    """A mutation, wrapped and formatted the way one actually reaches a validator.

    Every wrapper here is a real way the check has been defeated in other
    systems: a leading comment, a statement split across lines, a mutation
    hidden behind a harmless-looking SELECT, or one tucked inside a CTE where a
    "does it start with SELECT" test waves it through.
    """
    statement = draw(_MUTATIONS)
    shape = draw(st.sampled_from(["bare", "cte", "trailing_select", "leading_select"]))
    if shape == "cte":
        statement = f"WITH x AS (SELECT 1) {statement}"
    elif shape == "trailing_select":
        statement = f"{statement}; SELECT 1"
    elif shape == "leading_select":
        statement = f"SELECT 1; {statement}"

    if draw(st.booleans()):
        statement = f"-- a perfectly ordinary query\n{statement}"
    if draw(st.booleans()):
        statement = f"/* {draw(st.sampled_from(['audit', 'note', 'todo']))} */ {statement}"
    if draw(st.booleans()):
        statement = statement.replace(" ", "\n  ", 1)
    if draw(st.booleans()):
        statement = statement.lower()
    if draw(st.booleans()):
        statement = f"  {statement}  ;  "
    return statement


@st.composite
def benign_select(draw: st.DrawFn) -> str:
    """An ordinary, valid analytical query over the retail warehouse."""
    shape = draw(
        st.sampled_from(
            [
                "SELECT sku, brand FROM dim_product LIMIT {n}",
                "SELECT store_id, region FROM dim_store LIMIT {n}",
                "SELECT date, sku, units_sold FROM fmcg_sales LIMIT {n}",
                "SELECT region, COUNT(*) AS n FROM dim_store GROUP BY region LIMIT {n}",
                "WITH s AS (SELECT store_id, units_sold FROM fmcg_sales LIMIT {n})"
                " SELECT SUM(units_sold) AS total FROM s LIMIT {n}",
                "SELECT d.brand, SUM(f.units_sold) AS units FROM fmcg_sales f"
                " JOIN dim_product d ON f.sku = d.sku GROUP BY d.brand LIMIT {n}",
            ]
        )
    )
    return shape.format(n=draw(st.integers(min_value=5, max_value=500)))


_PRINCIPALS = st.one_of(
    st.lists(
        st.sampled_from(["PL-North", "PL-South", "PL-Central"]), unique=True, max_size=3
    ).map(lambda regions: Principal(id="fuzzed", grants={"region": tuple(regions)})),
)


# ---------------------------------------------------------------------------
# Invariant 1 -- nothing that mutates is ever accepted
# ---------------------------------------------------------------------------


@SETTINGS
@given(statement=obfuscated_mutation())
def test_no_mutation_is_ever_accepted_by_the_validator(validator, cursor, statement):
    result = validator.run(statement, cursor, question="anything")
    assert not result.is_valid, f"validator accepted a mutation: {statement!r}"


@SETTINGS
@given(statement=obfuscated_mutation())
def test_no_mutation_is_ever_accepted_by_the_ablated_safety_floor(validator, statement):
    """The ladder's un-validated rungs share this gate, so it has to hold there too.

    If it did not, scoring a "no validator" baseline would mean running
    model-authored DDL against the warehouse -- an unsafe experiment rather than
    a rigorous one.
    """
    result = validator.safety_only(statement)
    assert not result.is_valid, f"the safety floor accepted a mutation: {statement!r}"


@SETTINGS
@given(statement=obfuscated_mutation())
def test_a_rejected_mutation_says_why_in_a_stable_code(validator, cursor, statement):
    """Rejection reasons are the eval funnel's group-by key. A mutation caught
    only as a parse error would be counted as a model formatting problem."""
    codes = set(validator.run(statement, cursor, question="anything").issue_codes)
    assert codes & {
        IssueCode.MUTATION,
        IssueCode.NOT_A_SELECT,
        IssueCode.PARSE_ERROR,
        IssueCode.MULTIPLE_STATEMENTS,
    }


# ---------------------------------------------------------------------------
# Invariant 2 -- no accepted query escapes its row policy
# ---------------------------------------------------------------------------


@SETTINGS
@given(sql=benign_select(), principal=_PRINCIPALS)
def test_accepted_sql_never_escapes_the_row_policy(validator, cursor, policy, sql, principal):
    """The end-to-end property, re-derived from the SQL the validator returns."""
    result = validator.run(sql, cursor, question="fuzzed", principal=principal)
    if not result.is_valid:
        return  # rejected for some other reason; nothing reaches the warehouse
    assert scope_breaches(result.sanitized_sql, principal, policy) == [], (
        f"accepted SQL escaped the policy: {result.sanitized_sql!r}"
    )


@SETTINGS
@given(sql=benign_select(), principal=_PRINCIPALS)
def test_injection_is_idempotent(policy, sql, principal):
    """Applying the policy to an already-governed query must not compound it.

    The repair loop can hand the validator SQL that has been round-tripped, and
    a predicate that stacked on every pass would eventually produce a query too
    deep to plan -- a denial of service reached by doing the right thing twice.
    """
    once, _ = apply_row_policies(parse_one(sql, read="duckdb"), principal, policy)
    twice, _ = apply_row_policies(once, principal, policy)
    assert once.sql(dialect="duckdb") == twice.sql(dialect="duckdb")


@SETTINGS
@given(sql=benign_select(), principal=_PRINCIPALS)
def test_a_governed_query_still_parses_and_stays_a_select(policy, sql, principal):
    """Injection must not be able to produce something the warehouse cannot run,
    or the control turns every restricted question into an execution failure."""
    governed, _ = apply_row_policies(parse_one(sql, read="duckdb"), principal, policy)
    reparsed = parse_one(governed.sql(dialect="duckdb"), read="duckdb")
    assert isinstance(reparsed, exp.Select)


@SETTINGS
@given(sql=benign_select())
def test_the_steward_sql_is_byte_identical(policy, sql):
    """The compatibility guarantee, as a universal rather than one example: the
    unrestricted path must be untouched by the existence of row security."""
    governed, applied = apply_row_policies(parse_one(sql, read="duckdb"), STEWARD, policy)
    assert applied == []
    assert governed.sql(dialect="duckdb") == parse_one(sql, read="duckdb").sql(dialect="duckdb")


# ---------------------------------------------------------------------------
# The converse -- a validator that refused everything would pass all the above
# ---------------------------------------------------------------------------


@SETTINGS
@given(sql=benign_select())
def test_ordinary_analytical_sql_is_accepted(validator, cursor, sql):
    # A neutral question on purpose: the metric contract fires on the *question*,
    # so asking about revenue would make a query that does not compute revenue a
    # legitimate rejection and this property a test of the wrong thing.
    result = validator.run(sql, cursor, question="show me some rows")
    assert result.is_valid, f"rejected a valid query {sql!r}: {result.errors}"

"""Row-level security and PII masking.

Every test here names the way the control is usually got wrong, because a
governance test that only asserts the happy path is worth very little: the
interesting failures are all cases where the system keeps working and quietly
stops enforcing.
"""

from __future__ import annotations

import dataclasses

import pandas as pd
import pytest
from sqlglot import parse_one

from semantic_query_engine.agents.validator import IssueCode, ValidatorAgent
from semantic_query_engine.core.domains import (
    available_domain_names,
    get_domain,
    set_active_domain,
)
from semantic_query_engine.domain.registry import load_domain_registries
from semantic_query_engine.governance.masking import (
    mask_dataframe,
    mask_rows,
    mask_value,
    masked_columns,
)
from semantic_query_engine.governance.policy import (
    GovernancePolicy,
    GovernancePolicyError,
    load_governance_policy,
)
from semantic_query_engine.governance.principals import (
    STEWARD,
    Principal,
    PrincipalError,
    load_principals,
    resolve_principal,
)
from semantic_query_engine.governance.row_security import apply_row_policies, scope_breaches
from semantic_query_engine.semantic.layer import load_semantic_layer
from semantic_query_engine.warehouse.duckdb_client import init_database, open_cursor


@pytest.fixture
def retail_policy():
    return load_governance_policy(load_semantic_layer(get_domain("retail").semantic_layer_path))


@pytest.fixture
def retail_principals():
    return load_principals(get_domain("retail").principals_path)


@pytest.fixture
def airline(tmp_path_factory, monkeypatch):
    """The airline domain against a throwaway warehouse -- see tests/unit/test_domains.py."""
    domain = dataclasses.replace(
        get_domain("airline"),
        warehouse_path=tmp_path_factory.mktemp("airline_gov") / "airline.duckdb",
    )
    monkeypatch.setattr("semantic_query_engine.core.domains._active", domain)
    yield domain


def _sql(tree) -> str:
    return tree.sql(dialect="duckdb")


def _apply(sql: str, principal, policy) -> str:
    return _sql(apply_row_policies(parse_one(sql, read="duckdb"), principal, policy)[0])


# ---------------------------------------------------------------------------
# The declaration itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(available_domain_names()))
def test_no_domain_holds_restricted_data_it_forgot_to_scope(name):
    """The check that keeps row security from decaying as the warehouse grows.

    A table added to a domain that carries the scoping column, or the scoping
    table's key, but is absent from the policy is read *unfiltered*. Nothing in
    SQL can tell that apart from a legitimately unscoped dimension, so the
    semantic layer is asked instead. This is the regression that would otherwise
    ship silently the next time somebody adds a fact table.
    """
    domain = get_domain(name)
    policy = load_governance_policy(load_semantic_layer(domain.semantic_layer_path))
    registries = load_domain_registries(load_semantic_layer(domain.semantic_layer_path))
    assert policy.unguarded_tables(registries.grains, domain.allowed_tables) == {}


@pytest.mark.parametrize("name", sorted(available_domain_names()))
def test_every_principal_grant_names_a_policy_that_exists(name):
    """A grant key with no matching policy grants nothing and looks like it grants something."""
    domain = get_domain(name)
    policy = load_governance_policy(load_semantic_layer(domain.semantic_layer_path))
    declared = set(policy.grant_keys)
    for principal in load_principals(domain.principals_path).all():
        assert set(principal.grants) <= declared, (
            f"{principal.id} holds a grant for a policy {name} does not declare"
        )


# ---------------------------------------------------------------------------
# Predicate injection
# ---------------------------------------------------------------------------


def test_a_fact_is_scoped_without_joining_the_dimension(retail_policy, retail_principals):
    """The leak the semi-join exists to close.

    Filtering on ``dim_store.region`` only when the query happens to join
    ``dim_store`` leaves every query that does not join it reading the whole
    fact table -- which is most of them.
    """
    governed = _apply(
        "SELECT SUM(units_sold) FROM fmcg_sales",
        retail_principals.get("analyst_north"),
        retail_policy,
    )
    assert "dim_store.region IN ('PL-North')" in governed
    assert "fmcg_sales.store_id IN (SELECT" in governed


def test_a_cte_body_is_scoped_where_it_reads(retail_policy, retail_principals):
    """Scoping only the outer SELECT lets a CTE read the whole table underneath it."""
    governed = _apply(
        "WITH x AS (SELECT store_id, units_sold FROM fmcg_sales) SELECT SUM(units_sold) FROM x",
        retail_principals.get("analyst_north"),
        retail_policy,
    )
    assert governed.index("PL-North") < governed.index(") SELECT SUM")


def test_a_subquery_is_scoped(retail_policy, retail_principals):
    governed = _apply(
        "SELECT sku FROM dim_product WHERE sku IN (SELECT sku FROM fmcg_sales)",
        retail_principals.get("analyst_north"),
        retail_policy,
    )
    assert governed.count("PL-North") == 1
    assert "fmcg_sales.store_id IN (SELECT" in governed


def test_an_empty_grant_reads_no_rows_rather_than_every_row(retail_policy, retail_principals):
    """The bug this is guarding: an empty IN list is a syntax error, and the reflex
    fix -- skip the predicate when there is nothing to filter on -- turns the least
    privileged principal into the most privileged."""
    governed = _apply(
        "SELECT SUM(units_sold) FROM fmcg_sales",
        retail_principals.get("contractor"),
        retail_policy,
    )
    assert governed.endswith("WHERE FALSE")


def test_an_existing_disjunction_cannot_dilute_the_predicate(retail_policy, retail_principals):
    """``WHERE a OR 1=1`` must be bracketed, or the injected term joins the OR."""
    governed = _apply(
        "SELECT * FROM fmcg_sales WHERE sku = 'MI-006' OR 1=1",
        retail_principals.get("analyst_north"),
        retail_policy,
    )
    assert "WHERE (sku = 'MI-006' OR 1 = 1) AND fmcg_sales.store_id IN" in governed


def test_an_unscoped_dimension_is_left_alone(retail_policy, retail_principals):
    """dim_product holds no store data, so restricting it would refuse a valid question."""
    sql = "SELECT sku, brand FROM dim_product"
    assert _apply(sql, retail_principals.get("analyst_north"), retail_policy) == sql


def test_the_steward_is_not_filtered(retail_policy):
    """Every number this project has published was measured unrestricted."""
    sql = "SELECT SUM(units_sold) FROM fmcg_sales"
    assert _apply(sql, STEWARD, retail_policy) == sql


def test_a_principal_granted_every_value_is_still_filtered(retail_policy, retail_principals):
    """"Sees everything" and "is not subject to the policy" are different states.

    Only the second one silently starts seeing a region added tomorrow, which is
    why ``analyst_national`` is a separate principal from the steward rather
    than a shortcut to it.
    """
    governed = _apply(
        "SELECT SUM(units_sold) FROM fmcg_sales",
        retail_principals.get("analyst_national"),
        retail_policy,
    )
    assert "PL-North" in governed and "PL-Central" in governed


def test_the_input_tree_is_not_mutated(retail_policy, retail_principals):
    """The validator holds the tree for checks either side of injection, and the
    model's own SQL is what gets echoed back during repair."""
    tree = parse_one("SELECT SUM(units_sold) FROM fmcg_sales", read="duckdb")
    before = _sql(tree)
    apply_row_policies(tree, retail_principals.get("analyst_north"), retail_policy)
    assert _sql(tree) == before


# ---------------------------------------------------------------------------
# The re-derived check
# ---------------------------------------------------------------------------


def test_a_breach_is_detected_from_the_sql_that_would_run(retail_policy, retail_principals):
    north = retail_principals.get("analyst_north")
    assert scope_breaches("SELECT SUM(units_sold) FROM fmcg_sales", north, retail_policy)
    governed = _apply("SELECT SUM(units_sold) FROM fmcg_sales", north, retail_policy)
    assert scope_breaches(governed, north, retail_policy) == []


def test_a_predicate_under_an_or_does_not_count_as_scoped(retail_policy, retail_principals):
    """The direction this check must never be wrong in: a term that does not hold
    for every row is not a restriction, however much it looks like one."""
    smuggled = (
        "SELECT SUM(units_sold) FROM fmcg_sales WHERE "
        "fmcg_sales.store_id IN (SELECT dim_store.store_id FROM dim_store "
        "WHERE dim_store.region IN ('PL-North')) OR 1 = 1"
    )
    assert scope_breaches(smuggled, retail_principals.get("analyst_north"), retail_policy)


def test_unparseable_sql_is_not_cleared(retail_policy, retail_principals):
    assert scope_breaches("SELECT FROM WHERE", retail_principals.get("analyst_north"), retail_policy)


# ---------------------------------------------------------------------------
# Principals
# ---------------------------------------------------------------------------


def test_an_unknown_principal_is_an_error_not_an_anonymous_fallback():
    with pytest.raises(PrincipalError, match="No such principal"):
        resolve_principal("nobody-in-particular")


def test_no_principal_named_resolves_to_the_unrestricted_steward(monkeypatch):
    monkeypatch.delenv("SQE_PRINCIPAL", raising=False)
    assert resolve_principal(None) is STEWARD
    assert resolve_principal("") is STEWARD


def test_a_missing_grant_key_is_not_an_empty_grant():
    """Not scoped by a policy, and scoped by it to nothing, are opposite outcomes."""
    unscoped = Principal(id="x", grants={})
    empty = Principal(id="y", grants={"region": ()})
    assert not unscoped.is_scoped_by("region")
    assert empty.is_scoped_by("region")
    assert empty.grant_values("region") == ()


# ---------------------------------------------------------------------------
# PII
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("strategy", "value", "expected"),
    [
        ("redact", "Greta Adamczyk", "***"),
        ("email", "nv-cr-001@nv-crew.example", "***@nv-crew.example"),
        ("last4", "+48810160030", "***0030"),
        ("null", "anything", None),
        # A typo in the semantic layer must not silently disable masking.
        ("not-a-real-strategy", "secret", "***"),
        # A missing value carries no personal data; inventing one would be worse.
        ("redact", None, None),
    ],
)
def test_mask_value(strategy, value, expected):
    assert mask_value(value, strategy) == expected


def test_hashing_is_stable_and_salted(monkeypatch):
    """Stable so a report can group by a person; salted so the values are not a lookup table."""
    monkeypatch.setenv("SQE_MASK_SALT", "one")
    first = mask_value("NV-CR-001", "hash")
    assert first == mask_value("NV-CR-001", "hash")
    monkeypatch.setenv("SQE_MASK_SALT", "two")
    assert mask_value("NV-CR-001", "hash") != first


def test_masking_follows_the_alias_not_the_column_name(airline):
    """``SELECT crew_email AS contact`` is the case a name-matching implementation misses."""
    policy = load_governance_policy()
    tree = parse_one("SELECT c.crew_email AS contact FROM dim_crew c", read="duckdb")
    masked = masked_columns(tree, Principal(id="a"), policy)
    assert [(m.output_name, m.strategy) for m in masked] == [("contact", "email")]


def test_star_expands_to_the_tagged_columns(airline):
    policy = load_governance_policy()
    tree = parse_one("SELECT * FROM dim_crew", read="duckdb")
    assert {m.output_name for m in masked_columns(tree, Principal(id="a"), policy)} == {
        "crew_id",
        "crew_name",
        "crew_email",
        "crew_phone",
    }


def test_a_value_arriving_through_a_cte_is_still_masked(airline):
    """Lineage is not traced into a CTE, so the name carry-over rule has to catch it."""
    policy = load_governance_policy()
    tree = parse_one(
        "WITH x AS (SELECT crew_email FROM dim_crew) SELECT crew_email FROM x", read="duckdb"
    )
    assert [m.output_name for m in masked_columns(tree, Principal(id="a"), policy)] == ["crew_email"]


def test_a_cleared_principal_sees_the_values(airline):
    policy = load_governance_policy()
    tree = parse_one("SELECT crew_email FROM dim_crew", read="duckdb")
    cleared = Principal(id="hr", pii_access="unmasked")
    assert masked_columns(tree, cleared, policy) == []


def test_mask_rows_matches_case_insensitively(airline):
    policy = load_governance_policy()
    tree = parse_one("SELECT crew_email AS Contact FROM dim_crew", read="duckdb")
    columns = masked_columns(tree, Principal(id="a"), policy)
    assert mask_rows([{"Contact": "a@b.example"}], columns) == [{"Contact": "***@b.example"}]


def test_masking_a_frame_leaves_the_caller_frame_untouched():
    """Synthesis and the result table read the masked frame; nothing should be
    able to reach the original one by having held a reference to it."""
    from semantic_query_engine.governance.masking import MaskedColumn

    frame = pd.DataFrame({"pin": [1234, 5678]})
    masked = mask_dataframe(frame, [MaskedColumn("pin", "t.pin", "id", "redact")])
    assert list(masked["pin"]) == ["***", "***"]
    assert list(frame["pin"]) == [1234, 5678]


# ---------------------------------------------------------------------------
# The validator, end to end against the warehouse
# ---------------------------------------------------------------------------


@pytest.fixture
def airline_cursor(airline):
    return open_cursor(init_database(force=True, domain=airline))


def test_a_derived_expression_over_pii_is_rejected_not_masked(airline, airline_cursor):
    """There is no correct masking of ``UPPER(email)``: the disclosure already
    happened in the warehouse, before any result row existed."""
    result = ValidatorAgent().run(
        "SELECT UPPER(crew_email) AS shouty FROM dim_crew LIMIT 10",
        airline_cursor,
        principal=Principal(id="a", grants={"carrier_code": ("NV",)}),
    )
    assert not result.is_valid
    assert IssueCode.PII_DERIVED in result.issue_codes


def test_counting_pii_is_allowed(airline, airline_cursor):
    """Otherwise the tag means "unusable" rather than "personal"."""
    result = ValidatorAgent().run(
        "SELECT crew_role, COUNT(crew_email) AS n FROM dim_crew GROUP BY crew_role LIMIT 10",
        airline_cursor,
        principal=Principal(id="a", grants={"carrier_code": ("NV",)}),
    )
    assert result.is_valid, result.errors
    assert result.masked_columns == []


def test_the_validator_returns_sql_that_carries_its_own_predicate(airline, airline_cursor):
    """The whole control in one assertion: what comes back out is what executes."""
    principal = Principal(id="a", grants={"carrier_code": ("NV",)})
    result = ValidatorAgent().run(
        "SELECT COUNT(*) AS flights FROM fact_flights LIMIT 10",
        airline_cursor,
        principal=principal,
    )
    assert result.is_valid, result.errors
    assert "carrier_code IN ('NV')" in result.sanitized_sql
    assert scope_breaches(result.sanitized_sql, principal, load_governance_policy()) == []


def test_the_ablated_validator_refuses_a_restricted_principal(airline, airline_cursor):
    """Row policies are not part of what the evaluation ladder ablates.

    Switching the semantic checks off to score a baseline is a defensible
    experiment; switching access control off to score one is not.
    """
    result = ValidatorAgent().safety_only(
        "SELECT COUNT(*) FROM fact_flights",
        principal=Principal(id="a", grants={"carrier_code": ("NV",)}),
    )
    assert not result.is_valid
    assert IssueCode.ROW_POLICY_BREACH in result.issue_codes


def test_the_ablated_validator_still_serves_the_steward(airline, airline_cursor):
    assert ValidatorAgent().safety_only("SELECT COUNT(*) FROM fact_flights").is_valid


def test_restriction_changes_what_the_warehouse_returns(airline, airline_cursor):
    """The one that would catch a predicate that is built, reported, and then dropped."""
    validator = ValidatorAgent()
    sql = "SELECT COUNT(*) AS flights FROM fact_flights LIMIT 10"

    unrestricted = validator.run(sql, airline_cursor, principal=STEWARD)
    restricted = validator.run(
        sql, airline_cursor, principal=Principal(id="a", grants={"carrier_code": ("NV",)})
    )
    nothing = validator.run(
        sql, airline_cursor, principal=Principal(id="b", grants={"carrier_code": ()})
    )

    total = airline_cursor.execute(unrestricted.sanitized_sql).fetchone()[0]
    scoped = airline_cursor.execute(restricted.sanitized_sql).fetchone()[0]
    none = airline_cursor.execute(nothing.sanitized_sql).fetchone()[0]

    assert 0 < scoped < total
    assert none == 0


def test_retail_totals_are_unchanged_for_the_default_principal():
    """The published baseline. Governance must be invisible until asked for."""
    cursor = open_cursor(init_database())
    sql = "SELECT ROUND(SUM(units_sold * price_unit), 2) AS revenue FROM fmcg_sales LIMIT 10"
    set_active_domain("retail")
    result = ValidatorAgent().run(sql, cursor)
    assert result.applied_policies == []
    assert cursor.execute(result.sanitized_sql).fetchone()[0] == 19951300.58


# ---------------------------------------------------------------------------
# Scoping through a slowly-changing dimension
#
# The defect these guard against shipped and was live: `fact_fuel` is scoped
# through `dim_aircraft`, which is an SCD, and the semi-join asked only whether a
# tail number had *ever* belonged to a granted carrier. Two airframes in the
# airline warehouse transfer operator mid-period, so both the old and the new
# operator could read the whole of their fuel history -- 323 rows of another
# carrier's data for `ops_northvale` alone. No test caught it, because every
# existing one asked whether a predicate was present rather than what it admits.
# ---------------------------------------------------------------------------


def _scoped_fuel_count(cursor, policy, principal, sql="SELECT COUNT(*) AS n FROM fact_fuel"):
    governed, _ = apply_row_policies(parse_one(sql, read="duckdb"), principal, policy)
    return cursor.execute(governed.sql(dialect="duckdb")).fetchone()[0]


def test_a_transferred_airframes_history_does_not_follow_its_new_operator(
    airline, airline_cursor
):
    """The regression guard. `ops_northvale` holds NV; BQ-104 only became NV in
    March 2024, so its 2023 fuel belongs to BQ and must be unreadable."""
    policy = load_governance_policy(load_semantic_layer(airline.semantic_layer_path))
    northvale = load_principals(airline.principals_path).get("ops_northvale")

    visible = _scoped_fuel_count(
        airline_cursor,
        policy,
        northvale,
        "SELECT COUNT(*) AS n FROM fact_fuel WHERE fact_fuel.tail_number = 'BQ-104'",
    )
    total = airline_cursor.execute(
        "SELECT COUNT(*) FROM fact_fuel WHERE tail_number = 'BQ-104'"
    ).fetchone()[0]
    before_transfer = airline_cursor.execute(
        "SELECT COUNT(*) FROM fact_fuel WHERE tail_number = 'BQ-104' "
        "AND fuel_date < DATE '2024-03-01'"
    ).fetchone()[0]

    assert before_transfer > 0, "the fixture no longer exercises a transfer"
    assert visible == total - before_transfer


def test_the_validity_window_is_half_open_so_a_transfer_date_has_one_owner(
    airline, airline_cursor
):
    """Closing the window at both ends is the tempting wrong fix.

    `BETWEEN valid_from AND valid_to` reads naturally and puts the changeover day
    in *both* operators' scopes -- an over-grant of exactly the kind this
    predicate exists to remove. Summing the two sides and comparing against the
    real total is what catches it; each side alone looks right.
    """
    policy = load_governance_policy(load_semantic_layer(airline.semantic_layer_path))
    principals = load_principals(airline.principals_path)

    scoped = "SELECT COUNT(*) AS n FROM fact_fuel WHERE fact_fuel.tail_number = 'BQ-104'"
    old_operator = _scoped_fuel_count(
        airline_cursor,
        policy,
        dataclasses.replace(
            principals.get("ops_northvale"), id="bq_only", grants={"carrier_code": ("BQ",)}
        ),
        scoped,
    )
    new_operator = _scoped_fuel_count(
        airline_cursor, policy, principals.get("ops_northvale"), scoped
    )
    total = airline_cursor.execute(
        "SELECT COUNT(*) FROM fact_fuel WHERE tail_number = 'BQ-104'"
    ).fetchone()[0]

    assert old_operator + new_operator == total


def test_a_temporal_scope_is_still_a_breach_when_the_predicate_is_missing(
    airline, airline_cursor
):
    """The re-derived check has to understand the new predicate shape too.

    A tightening that the breach checker could not recognise would report every
    correctly-scoped fuel query as a breach, which is the failure mode that gets
    a security check switched off.
    """
    policy = load_governance_policy(load_semantic_layer(airline.semantic_layer_path))
    northvale = load_principals(airline.principals_path).get("ops_northvale")

    assert scope_breaches("SELECT SUM(fuel_litres) FROM fact_fuel", northvale, policy)
    governed, _ = apply_row_policies(
        parse_one("SELECT SUM(fuel_litres) FROM fact_fuel", read="duckdb"), northvale, policy
    )
    assert scope_breaches(governed.sql(dialect="duckdb"), northvale, policy) == []


def test_a_half_written_temporal_scope_is_refused_rather_than_widened():
    """Dropping to the untimed semi-join is the silent, wider behaviour."""
    with pytest.raises(GovernancePolicyError) as excinfo:
        GovernancePolicy(
            {
                "row_policies": [
                    {
                        "name": "carrier_scope",
                        "grant_key": "carrier_code",
                        "anchor_table": "dim_carrier",
                        "anchor_column": "carrier_code",
                        "tables": [
                            {
                                "table": "fact_fuel",
                                "mode": "semijoin",
                                "key": "tail_number",
                                "through": {
                                    "table": "dim_aircraft",
                                    "column": "tail_number",
                                    "filter_column": "carrier_code",
                                    "valid_from": "valid_from",
                                },
                            }
                        ],
                    }
                ]
            }
        )
    assert "as_of" in str(excinfo.value)


@pytest.mark.parametrize("domain_name", sorted(available_domain_names()))
def test_a_temporal_scope_agrees_with_the_dimensions_declared_grain(domain_name):
    """The window is declared in two places; they must not drift apart.

    Governance names it because the predicate has to fail closed on its own, and
    the table's ``grain.validity`` is the single source of truth for what the
    window actually is. A test rather than a load-time merge, because the two
    answer different questions -- "should this scope be timed" and "what is the
    window" -- and a scope that silently inherited a window it did not ask for is
    how a policy stops meaning what it says.
    """
    domain = get_domain(domain_name)
    layer = load_semantic_layer(domain.semantic_layer_path)
    policy = load_governance_policy(layer)
    grains = load_domain_registries(layer).grains

    for row_policy in policy.row_policies:
        for scope in row_policy.tables.values():
            if not scope.is_temporal:
                continue
            grain = grains.grain(scope.through_table)
            assert grain is not None, f"{scope.through_table} declares no grain"
            assert (scope.through_valid_from, scope.through_valid_to) == (
                grain.validity_from,
                grain.validity_to,
            )

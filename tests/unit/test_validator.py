"""Validator agent: SQL safety, scope-aware schema resolution, and business rules."""

from __future__ import annotations

import pytest

from semantic_query_engine.agents.validator import IssueCode, ValidatorAgent
from semantic_query_engine.core.config import PIPELINE
from semantic_query_engine.warehouse.duckdb_client import init_database


@pytest.fixture(scope="module")
def conn():
    return init_database()


@pytest.fixture()
def validator():
    return ValidatorAgent()


# ---------------------------------------------------------------------------
# Statement safety
# ---------------------------------------------------------------------------


def test_validator_blocks_ddl(validator, conn):
    result = validator.run("DROP TABLE fmcg_sales", conn)
    assert not result.is_valid
    assert IssueCode.MUTATION in result.issue_codes


def test_validator_blocks_unknown_tables(validator, conn):
    result = validator.run("SELECT * FROM secrets", conn)
    assert not result.is_valid
    assert IssueCode.UNKNOWN_TABLE in result.issue_codes


# ---------------------------------------------------------------------------
# Scope-aware column resolution
#
# The regression these guard is specific: resolving columns against a flat union
# of every allowed table accepts a column that exists *somewhere* in the
# warehouse but not in the table the query actually reads.
# ---------------------------------------------------------------------------


def test_column_from_another_table_is_rejected(validator, conn):
    """lifecycle_stage exists on weekly_modeling_data, never on fmcg_sales."""
    result = validator.run(
        "SELECT lifecycle_stage, SUM(units_sold) AS total_units FROM fmcg_sales GROUP BY lifecycle_stage",
        conn,
    )
    assert not result.is_valid
    assert IssueCode.UNKNOWN_COLUMN in result.issue_codes
    assert any("lifecycle_stage" in error for error in result.errors)


def test_the_same_column_is_accepted_on_the_table_that_has_it(validator, conn):
    result = validator.run(
        "SELECT lifecycle_stage, AVG(units_sold) AS avg_units FROM weekly_modeling_data GROUP BY lifecycle_stage",
        conn,
    )
    assert result.is_valid, result.errors


def test_qualified_column_is_checked_against_its_own_table(validator, conn):
    result = validator.run(
        "SELECT f.lifecycle_stage FROM fmcg_sales AS f",
        conn,
    )
    assert not result.is_valid
    assert IssueCode.UNKNOWN_COLUMN in result.issue_codes


def test_unknown_table_alias_is_reported(validator, conn):
    result = validator.run("SELECT x.region FROM fmcg_sales AS f", conn)
    assert not result.is_valid
    assert IssueCode.UNKNOWN_ALIAS in result.issue_codes


def test_unqualified_column_shared_by_two_joined_tables_is_ambiguous(validator, conn):
    """Both dim_store and weekly_modeling_data carry `region`, so an unqualified
    reference is genuinely ambiguous -- DuckDB would reject it, and saying so
    with a clear message is more useful to a repair attempt than a raw binder
    error."""
    result = validator.run(
        "SELECT region, COUNT(*) AS n "
        "FROM dim_store AS d JOIN weekly_modeling_data AS w ON d.channel = w.channel "
        "GROUP BY region",
        conn,
    )
    assert not result.is_valid
    assert IssueCode.AMBIGUOUS_COLUMN in result.issue_codes


def test_a_column_merged_by_using_is_not_ambiguous(validator, conn):
    """USING(region) merges the two columns into one, so it needs no qualifier."""
    result = validator.run(
        "SELECT region, SUM(f.units_sold) AS total_units "
        "FROM fmcg_sales AS f JOIN weekly_modeling_data AS w USING (region) "
        "GROUP BY region",
        conn,
    )
    assert IssueCode.AMBIGUOUS_COLUMN not in result.issue_codes


def test_cte_output_columns_resolve_in_the_outer_query(validator, conn):
    result = validator.run(
        "WITH weekly AS ("
        "  SELECT d.region, DATE_TRUNC('week', s.date) AS week_start, "
        "  SUM(s.units_sold) AS units "
        "  FROM fmcg_sales s JOIN dim_store d ON s.store_id = d.store_id "
        "  GROUP BY d.region, DATE_TRUNC('week', s.date)"
        ") "
        "SELECT region, week_start, units - LAG(units) OVER (PARTITION BY region ORDER BY week_start) "
        "AS wow_change FROM weekly",
        conn,
    )
    assert result.is_valid, result.errors


def test_a_column_absent_from_a_cte_is_rejected_in_the_outer_query(validator, conn):
    """The CTE projects region and units only; channel is not among its outputs."""
    result = validator.run(
        "WITH weekly AS ("
        "  SELECT d.region, SUM(s.units_sold) AS units FROM fmcg_sales s "
        "  JOIN dim_store d ON s.store_id = d.store_id GROUP BY d.region"
        ") SELECT region, channel, units FROM weekly",
        conn,
    )
    assert not result.is_valid
    assert IssueCode.UNKNOWN_COLUMN in result.issue_codes


def test_select_star_in_a_subquery_is_treated_as_opaque_not_rejected(validator, conn):
    """Its outputs can't be enumerated statically, so EXPLAIN is left to judge --
    guessing would mean rejecting valid SQL."""
    result = validator.run(
        "SELECT pack_type, SUM(units_sold) AS total_units "
        "FROM (SELECT * FROM fmcg_sales) AS sub GROUP BY pack_type",
        conn,
    )
    assert result.is_valid, result.errors


def test_select_alias_is_usable_in_order_by(validator, conn):
    result = validator.run(
        "SELECT pack_type, SUM(units_sold) AS total_units FROM fmcg_sales "
        "GROUP BY pack_type ORDER BY total_units DESC",
        conn,
    )
    assert result.is_valid, result.errors


# ---------------------------------------------------------------------------
# Result-set bounds
# ---------------------------------------------------------------------------


def test_a_query_without_a_limit_is_capped(validator, conn):
    """max_result_rows was previously advertised but only enforced against a LIMIT
    the model chose to write -- an unbounded query passed straight through."""
    result = validator.run("SELECT sku, date, units_sold FROM fmcg_sales", conn)
    assert result.is_valid, result.errors
    assert result.applied_limit == PIPELINE.max_result_rows
    assert f"LIMIT {PIPELINE.max_result_rows}" in result.sanitized_sql.upper()


def test_an_existing_limit_is_left_exactly_as_written(validator, conn):
    sql = (
        "SELECT pack_type, SUM(units_sold) AS total_units "
        "FROM fmcg_sales GROUP BY pack_type LIMIT 10"
    )
    result = validator.run(sql, conn)
    assert result.is_valid, result.errors
    assert result.applied_limit is None
    assert result.sanitized_sql == sql


def test_the_injected_limit_actually_bounds_the_rows_returned(validator, conn):
    """The cap has to survive into the SQL that executes, not just be recorded."""
    result = validator.run("SELECT sku, date, units_sold FROM fmcg_sales", conn)
    rows = conn.execute(result.sanitized_sql).fetchall()
    assert len(rows) == PIPELINE.max_result_rows


def test_limit_above_the_safe_maximum_is_rejected(validator, conn):
    result = validator.run("SELECT sku FROM fmcg_sales LIMIT 100000", conn)
    assert not result.is_valid
    assert IssueCode.LIMIT_TOO_LARGE in result.issue_codes


def test_validator_rejects_unrequested_three_row_cap(validator, conn):
    result = validator.run(
        "SELECT region, SUM(units_sold) AS total_units FROM fmcg_sales GROUP BY region LIMIT 3",
        conn,
        question="What are total units sold by region?",
    )
    assert not result.is_valid
    assert IssueCode.LIMIT_TOO_SMALL in result.issue_codes


# ---------------------------------------------------------------------------
# Dropped-filter detection (registry-driven)
# ---------------------------------------------------------------------------


def test_validator_rejects_unknown_column_and_missing_requested_filter(validator, conn):
    result = validator.run(
        "SELECT region, SUM(not_a_column) FROM fmcg_sales GROUP BY region",
        conn,
        question="What is total revenue by South region?",
    )
    assert not result.is_valid
    assert IssueCode.UNKNOWN_COLUMN in result.issue_codes
    assert IssueCode.DROPPED_FILTER in result.issue_codes


def test_a_requested_region_is_matched_by_its_canonical_value(validator, conn):
    """"south" resolves to PL-South through the registry, so SQL filtering on the
    canonical value satisfies the check -- the old hardcoded list only worked
    because "south" happens to be a substring of "PL-South"."""
    result = validator.run(
        "SELECT d.channel, SUM(s.units_sold) AS total_units FROM fmcg_sales s "
        "JOIN dim_store d ON s.store_id = d.store_id "
        "WHERE d.region = 'PL-South' GROUP BY d.channel",
        conn,
        question="What are total units sold by channel in the south?",
    )
    assert result.is_valid, result.errors


def test_a_dimension_named_twice_is_a_comparison_not_a_filter(validator, conn):
    """"North vs South" should group by region, not filter to one of them --
    demanding a literal filter here would reject the correct query."""
    result = validator.run(
        "SELECT region, SUM(units_sold) AS total_units FROM fmcg_sales GROUP BY region",
        conn,
        question="Compare units sold in the north versus the south",
    )
    assert IssueCode.DROPPED_FILTER not in result.issue_codes


def test_a_requested_sku_bound_as_a_parameter_counts_as_present(validator, conn):
    """Fallback templates bind literals rather than inlining them, so the check
    has to read the bound params and not only the SQL text."""
    result = validator.run(
        "SELECT region, SUM(units_sold) AS total_units FROM fmcg_sales WHERE sku = ? GROUP BY region",
        conn,
        question="What were total units sold for SKU MI-006?",
        params=["MI-006"],
    )
    assert IssueCode.DROPPED_FILTER not in result.issue_codes


def test_a_dropped_sku_filter_is_caught(validator, conn):
    result = validator.run(
        "SELECT region, SUM(units_sold) AS total_units FROM fmcg_sales GROUP BY region",
        conn,
        question="What were total units sold for SKU MI-006?",
    )
    assert not result.is_valid
    assert IssueCode.DROPPED_FILTER in result.issue_codes
    assert any("MI-006" in error for error in result.errors)


# ---------------------------------------------------------------------------
# Metric contract
# ---------------------------------------------------------------------------


def test_revenue_labelled_as_such_must_use_the_certified_formula(validator, conn):
    """Summing units and calling the column total_revenue is the exact failure the
    metric contract exists to catch: a plausible-looking number that is not the
    metric the business certified."""
    result = validator.run(
        "SELECT region, SUM(units_sold) AS total_revenue FROM fmcg_sales GROUP BY region",
        conn,
        question="What is total revenue by region?",
    )
    assert not result.is_valid
    assert IssueCode.METRIC_CONTRACT in result.issue_codes


def test_the_certified_revenue_formula_passes(validator, conn):
    result = validator.run(
        "SELECT d.region, ROUND(SUM(s.units_sold * s.price_unit), 2) AS total_revenue "
        "FROM fmcg_sales s JOIN dim_store d ON s.store_id = d.store_id "
        "GROUP BY d.region",
        conn,
        question="What is total revenue by region?",
    )
    assert result.is_valid, result.errors


class _CountingConnection:
    """Proxies to a real DuckDB connection, recording the DESCRIBE calls.

    A ``DuckDBPyConnection`` is a C object whose ``execute`` cannot be replaced,
    so the count is taken from a wrapper rather than by monkeypatching.
    """

    def __init__(self, inner):
        self._inner = inner
        self.describes: list[str] = []

    def execute(self, sql, *args, **kwargs):
        if sql.upper().startswith("DESCRIBE"):
            self.describes.append(sql)
        return self._inner.execute(sql, *args, **kwargs)


def test_schema_is_described_once_per_agent_not_once_per_query(validator, conn):
    """The DESCRIBE round-trip per allowed table used to run on every validation."""
    counting = _CountingConnection(conn)

    for _ in range(3):
        validator.run("SELECT region FROM fmcg_sales", counting)

    assert len(counting.describes) == len(validator.allowed_tables)


# ---------------------------------------------------------------------------
# Grain
#
# The regression these guard is the one class of error every other check here
# misses: SQL that parses, plans, executes and returns a confidently wrong
# number. The fan-out case below really does return 21,114,387 units against a
# true 3,799,824 -- a 5.6x overstatement that raises nothing.
# ---------------------------------------------------------------------------


def test_fanout_join_under_a_sum_is_rejected(validator, conn):
    """sku+date does not cover either fact's grain, so the join multiplies rows."""
    result = validator.run(
        "SELECT SUM(s.units_sold) FROM fmcg_sales s "
        "JOIN fact_inventory i ON s.sku = i.sku AND s.date = i.date LIMIT 10",
        conn,
        "total units sold",
    )
    assert not result.is_valid
    assert IssueCode.GRAIN_FANOUT in result.issue_codes


def test_join_covering_the_full_grain_is_accepted(validator, conn):
    """The same two tables joined on all three key columns is one-to-one."""
    result = validator.run(
        "SELECT SUM(s.units_sold) FROM fmcg_sales s "
        "JOIN fact_inventory i ON s.sku = i.sku AND s.date = i.date "
        "AND s.store_id = i.store_id LIMIT 10",
        conn,
        "total units sold",
    )
    assert result.is_valid, result.errors


def test_star_join_to_a_dimension_is_accepted(validator, conn):
    """A many-to-one join to a dimension cannot fan out: sku is dim_product's grain."""
    result = validator.run(
        "SELECT p.category, SUM(s.units_sold) FROM fmcg_sales s "
        "JOIN dim_product p ON s.sku = p.sku GROUP BY 1 LIMIT 10",
        conn,
        "units sold by category",
    )
    assert result.is_valid, result.errors


def test_fanout_without_an_additive_aggregate_is_accepted(validator, conn):
    """Row multiplication is only an error when something counts the extra rows."""
    result = validator.run(
        "SELECT DISTINCT s.sku FROM fmcg_sales s "
        "JOIN fact_promotions f ON s.sku = f.sku LIMIT 10",
        conn,
        "list the distinct skus",
    )
    assert result.is_valid, result.errors


def test_join_condition_in_the_where_clause_is_still_analysed(validator, conn):
    """An implicit comma join fans out exactly as an explicit one does.

    It is also not reported as an unrelated join: the keys are present, just
    written in the older syntax, and one mistake should produce one code.
    """
    result = validator.run(
        "SELECT SUM(s.units_sold) FROM fmcg_sales s, fact_inventory i "
        "WHERE s.sku = i.sku LIMIT 10",
        conn,
        "total units sold",
    )
    assert not result.is_valid
    assert result.issue_codes == [IssueCode.GRAIN_FANOUT]


def test_day_equated_to_week_start_is_rejected(validator, conn):
    """fmcg_sales.date = weekly_modeling_data.week matches only Mondays."""
    result = validator.run(
        "SELECT SUM(s.units_sold) FROM fmcg_sales s "
        "JOIN weekly_modeling_data w ON s.date = w.week AND s.sku = w.sku LIMIT 10",
        conn,
        "total units sold",
    )
    assert not result.is_valid
    assert IssueCode.GRAIN_KEY_MISMATCH in result.issue_codes


def test_join_with_no_condition_is_rejected(validator, conn):
    result = validator.run(
        "SELECT SUM(s.units_sold) FROM fmcg_sales s CROSS JOIN dim_store d LIMIT 10",
        conn,
        "total units sold",
    )
    assert not result.is_valid
    assert IssueCode.UNRELATED_JOIN in result.issue_codes


def test_equality_under_an_or_is_not_treated_as_a_join_key(validator, conn):
    """An OR-ed equality does not constrain every row, so it cannot make a join safe.

    Counting it would let the fan-out through, which is the one direction this
    analysis must never be wrong in.
    """
    result = validator.run(
        "SELECT SUM(s.units_sold) FROM fmcg_sales s JOIN fact_inventory i "
        "ON (s.sku = i.sku AND s.date = i.date AND s.store_id = i.store_id) "
        "OR s.sku = i.sku LIMIT 10",
        conn,
        "total units sold",
    )
    assert not result.is_valid
    assert IssueCode.GRAIN_FANOUT in result.issue_codes


def test_aggregating_before_the_join_is_accepted(validator, conn):
    """The standard fix for a fan-out must not itself be rejected.

    A subquery's grain is whatever its GROUP BY produced and is declared
    nowhere, so an unknown grain is treated as unanalysable rather than unsafe.
    """
    result = validator.run(
        "SELECT d.region, t.units FROM ("
        "  SELECT store_id, SUM(units_sold) AS units FROM fmcg_sales GROUP BY 1"
        ") t JOIN dim_store d ON t.store_id = d.store_id LIMIT 10",
        conn,
        "units sold by region",
    )
    assert result.is_valid, result.errors


# ---------------------------------------------------------------------------
# Stacked statements (found by the property tests, 2026-09-21)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        # The exact case the fuzzer found. TRUNCATE was absent from the mutation
        # list, and parse_one folds "a; b" into a Block whose Select satisfied
        # the is-this-a-SELECT check -- so this was accepted, and DuckDB
        # executes both halves.
        "TRUNCATE TABLE fmcg_sales; SELECT 1",
        "SELECT 1; DROP TABLE dim_store",
        "SELECT sku FROM dim_product LIMIT 10; DELETE FROM fmcg_sales",
    ],
)
def test_a_second_statement_is_refused_outright(validator, conn, statement):
    """Everything after the first semicolon still executes, so validating only
    the first statement is validating the wrong query."""
    result = validator.run(statement, conn, question="anything")
    assert not result.is_valid
    assert IssueCode.MULTIPLE_STATEMENTS in result.issue_codes


@pytest.mark.parametrize(
    "statement",
    [
        "TRUNCATE TABLE fmcg_sales",
        "COPY fmcg_sales TO 'stolen.csv'",
        "ATTACH 'elsewhere.db' AS other",
        "PRAGMA database_list",
        # sqlglot models neither of these, so both parse to exp.Command --
        # which is why Command is in the refused set: SQL the validator cannot
        # analyse is exactly what it must not wave through.
        "INSTALL httpfs",
        "VACUUM",
    ],
)
def test_statements_beyond_the_remembered_six_are_refused(validator, conn, statement):
    """The original list named INSERT/UPDATE/DELETE/DROP/CREATE/ALTER, which is a
    list of the verbs somebody thought of. These reach the same places."""
    assert not validator.run(statement, conn, question="anything").is_valid
    assert not validator.safety_only(statement).is_valid

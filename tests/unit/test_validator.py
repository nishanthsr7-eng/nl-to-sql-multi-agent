"""Validator agent: SQL safety, schema, and metric-contract enforcement."""

from __future__ import annotations

from semantic_query_engine.agents.validator import ValidatorAgent
from semantic_query_engine.warehouse.duckdb_client import init_database


def test_validator_blocks_ddl():
    conn = init_database()
    validator = ValidatorAgent()
    result = validator.run("DROP TABLE fmcg_sales", conn)
    assert not result.is_valid


def test_validator_rejects_unknown_column_and_missing_requested_filter():
    conn = init_database()
    validator = ValidatorAgent()
    result = validator.run(
        "SELECT region, SUM(not_a_column) FROM fmcg_sales GROUP BY region",
        conn,
        question="What is total revenue by South region?",
    )
    assert not result.is_valid
    assert any("Unknown column" in error for error in result.errors)
    assert any("Requested region" in error for error in result.errors)


def test_validator_rejects_unrequested_three_row_cap():
    conn = init_database()
    validator = ValidatorAgent()
    result = validator.run(
        "SELECT region, SUM(units_sold) AS total_units FROM fmcg_sales GROUP BY region LIMIT 3",
        conn,
        question="What are total units sold by region?",
    )
    assert not result.is_valid
    assert any("at least 5 rows" in error for error in result.errors)

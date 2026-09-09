"""Warehouse layer: table construction, resource ceilings, cursors, and timeouts."""

from __future__ import annotations

import threading

import pytest

from semantic_query_engine.core.config import PIPELINE
from semantic_query_engine.core.errors import QueryTimeoutError
from semantic_query_engine.warehouse.duckdb_client import (
    execute_guarded,
    get_connection,
    init_database,
    open_cursor,
)


def test_init_database_builds_both_tables():
    conn = init_database()
    tables = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
    assert {"fmcg_sales", "weekly_modeling_data"} <= tables


def test_fmcg_sales_is_populated():
    conn = get_connection()
    (count,) = conn.execute("SELECT COUNT(*) FROM fmcg_sales").fetchone()
    assert count > 0


def test_connections_carry_the_configured_resource_ceilings():
    """An unbounded model-authored query should hit a limit, not the host's RAM."""
    conn = get_connection()
    (threads,) = conn.execute("SELECT current_setting('threads')").fetchone()
    assert int(threads) == PIPELINE.warehouse_threads
    (memory,) = conn.execute("SELECT current_setting('memory_limit')").fetchone()
    assert memory  # DuckDB normalises the unit, so assert it was set at all


def test_a_cursor_is_independent_of_its_parent_connection():
    """Each request takes its own cursor, because a DuckDB connection is not safe
    to use from two threads at once."""
    conn = get_connection()
    cursor = open_cursor(conn)
    assert cursor is not conn

    # Both see the same database, and using one does not disturb the other's result.
    conn.execute("SELECT 1")
    (rows,) = cursor.execute("SELECT COUNT(*) FROM fmcg_sales").fetchone()
    assert rows > 0
    assert conn.fetchone() == (1,)
    cursor.close()


def test_a_cursor_inherits_the_resource_ceilings():
    cursor = open_cursor(get_connection())
    (threads,) = cursor.execute("SELECT current_setting('threads')").fetchone()
    assert int(threads) == PIPELINE.warehouse_threads
    cursor.close()


def test_execute_guarded_returns_rows_for_a_normal_query():
    cursor = open_cursor(get_connection())
    df = execute_guarded(
        cursor, "SELECT pack_type, COUNT(*) AS n FROM fmcg_sales GROUP BY pack_type"
    )
    assert not df.empty
    assert "pack_type" in df.columns
    cursor.close()


def test_execute_guarded_cancels_a_query_that_outruns_its_budget():
    """DuckDB has no statement timeout, so the bound is enforced by interrupting
    the query from a timer -- this proves the interrupt actually lands and is
    reported as a timeout rather than as a generic execution error."""
    cursor = open_cursor(get_connection())
    # A self-join over the fact table is the shape of an accidentally expensive
    # generation: valid SQL, far too much work.
    runaway = (
        "SELECT COUNT(*) FROM fmcg_sales a, fmcg_sales b, fmcg_sales c "
        "WHERE a.units_sold > 0 AND b.units_sold > 0 AND c.units_sold > 0"
    )
    with pytest.raises(QueryTimeoutError) as excinfo:
        execute_guarded(cursor, runaway, timeout_seconds=1.0)

    assert excinfo.value.timeout_seconds == 1.0
    cursor.close()


def test_a_genuine_sql_error_is_not_reported_as_a_timeout():
    """The interrupt surfaces as an ordinary duckdb.Error, so the two have to be
    told apart by the timer's own flag rather than by the exception type."""
    cursor = open_cursor(get_connection())
    with pytest.raises(Exception) as excinfo:
        execute_guarded(cursor, "SELECT no_such_column FROM fmcg_sales", timeout_seconds=30.0)

    assert not isinstance(excinfo.value, QueryTimeoutError)
    cursor.close()


def test_the_timeout_timer_does_not_outlive_the_query():
    """A timer left armed would interrupt an unrelated later query on the same
    cursor, so it has to be cancelled once the query returns."""
    before = threading.active_count()
    cursor = open_cursor(get_connection())
    for _ in range(5):
        execute_guarded(cursor, "SELECT 1 AS n", timeout_seconds=30.0)
    cursor.close()
    assert threading.active_count() <= before + 1


def test_the_serving_connection_cannot_write_to_the_warehouse():
    """The container serves as a non-root user from a root-owned, mode-644
    warehouse, so a read-write open fails at startup with a bare "Permission
    denied" -- which is how the image shipped broken. It also costs the hardening
    its point: the read-only filesystem is there so a mutation that slipped past
    the validator still cannot land. Asserting the connection refuses a write
    pins both.
    """
    conn = get_connection()
    with pytest.raises(Exception) as excinfo:
        conn.execute("CREATE TABLE should_not_exist AS SELECT 1 AS n")

    assert "read-only" in str(excinfo.value).lower()

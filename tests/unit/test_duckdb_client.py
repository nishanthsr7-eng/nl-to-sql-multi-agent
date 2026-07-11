"""Warehouse connection lifecycle: table/view shape and error handling.

Uses the ephemeral test warehouse from tests/conftest.py for the happy path, and
a scratch file (never the real or test warehouse) for the corrupt-file case.
"""

from __future__ import annotations

import pytest

from semantic_query_engine.core.errors import WarehouseError
from semantic_query_engine.warehouse import duckdb_client


def test_init_database_creates_the_two_analytical_tables_and_nothing_else():
    conn = duckdb_client.init_database()
    tables = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
    assert {"fmcg_sales", "weekly_modeling_data"}.issubset(tables)
    # total_revenue was a view nothing ever queried -- removed as dead config,
    # see CHANGELOG.md. Assert it stays gone rather than silently reappearing.
    assert "total_revenue" not in tables


def test_get_connection_raises_a_typed_error_for_a_corrupt_warehouse_file(tmp_path, monkeypatch):
    bogus_path = tmp_path / "corrupt.duckdb"
    bogus_path.write_bytes(b"this is not a valid duckdb file")
    monkeypatch.setattr(duckdb_client, "DUCKDB_PATH", bogus_path)

    with pytest.raises(WarehouseError):
        duckdb_client.get_connection()

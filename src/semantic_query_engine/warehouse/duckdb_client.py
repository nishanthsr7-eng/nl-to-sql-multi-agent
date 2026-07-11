"""Load CSV datasets into a local DuckDB warehouse and hand out connections."""

from __future__ import annotations

import duckdb

from semantic_query_engine.core.config import DAILY_SALES_CSV, DUCKDB_PATH, WEEKLY_MODELING_CSV
from semantic_query_engine.core.errors import WarehouseError


def init_database(force: bool = False) -> duckdb.DuckDBPyConnection:
    """Create or open the DuckDB file and (re)build the analytical tables.

    Parameters
    ----------
    force:
        When True, delete any existing DuckDB file first so tables are rebuilt
        from the source CSVs rather than reused.

    Raises
    ------
    WarehouseError
        If the file can't be opened (locked by another process, corrupt, or a
        permissions error) or a source CSV is missing/malformed.
    """
    if force and DUCKDB_PATH.exists():
        DUCKDB_PATH.unlink()
    DUCKDB_PATH.parent.mkdir(parents=True, exist_ok=True)

    try:
        conn = duckdb.connect(str(DUCKDB_PATH))

        conn.execute(
            """
            CREATE OR REPLACE TABLE fmcg_sales AS
            SELECT * FROM read_csv_auto(?, header=True)
            """,
            [str(DAILY_SALES_CSV)],
        )

        conn.execute(
            """
            CREATE OR REPLACE TABLE weekly_modeling_data AS
            SELECT * FROM read_csv_auto(?, header=True)
            """,
            [str(WEEKLY_MODELING_CSV)],
        )
    except duckdb.Error as exc:
        raise WarehouseError(f"Could not initialize the DuckDB warehouse at {DUCKDB_PATH}: {exc}") from exc
    except OSError as exc:
        raise WarehouseError(f"Could not read the source CSV files for the warehouse: {exc}") from exc

    return conn


def get_connection() -> duckdb.DuckDBPyConnection:
    """Return a connection to the warehouse, initializing it on first use.

    Raises
    ------
    WarehouseError
        If the DuckDB file exists but can't be opened -- e.g. it's locked by
        another process, or the file is corrupt.
    """
    if not DUCKDB_PATH.exists():
        return init_database()
    try:
        return duckdb.connect(str(DUCKDB_PATH))
    except duckdb.Error as exc:
        raise WarehouseError(
            f"Could not open the DuckDB warehouse at {DUCKDB_PATH}: {exc}. "
            "It may be locked by another process or corrupted -- close any other "
            "connection to this file, or delete it and re-run to rebuild from source CSVs."
        ) from exc

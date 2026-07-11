"""Initialize the local DuckDB warehouse from source CSVs.

Usage:
    python scripts/init_database.py
"""

from __future__ import annotations

try:
    import semantic_query_engine  # noqa: F401
except ImportError:  # pragma: no cover - convenience fallback, not the primary path
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from semantic_query_engine.warehouse.duckdb_client import init_database


def main() -> None:
    conn = init_database(force=True)
    tables = conn.execute("SHOW TABLES").fetchall()
    print("Initialized DuckDB with tables:")
    for (table_name,) in tables:
        count = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
        print(f"  - {table_name}: {count:,} rows")


if __name__ == "__main__":
    main()

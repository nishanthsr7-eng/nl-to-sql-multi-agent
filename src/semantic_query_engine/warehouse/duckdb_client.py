"""Load CSV datasets into a local DuckDB warehouse, and execute queries safely.

Two things beyond opening a connection live here, both because the SQL that
reaches this layer is model-authored and therefore untrusted:

* **Resource ceilings.** Every connection is opened with an explicit memory limit
  and thread count, so one bad generation cannot take the whole process with it.
* **A wall-clock bound.** :func:`execute_guarded` interrupts a query that runs past
  the configured timeout. DuckDB has no ``statement_timeout``; the supported
  mechanism is :meth:`~duckdb.DuckDBPyConnection.interrupt` from another thread,
  which is what the timer here does.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from semantic_query_engine.core.config import PIPELINE
from semantic_query_engine.core.domains import Domain, active_domain
from semantic_query_engine.core.errors import QueryTimeoutError, WarehouseError
from semantic_query_engine.core.logging import get_logger

logger = get_logger(__name__)

# Conservative on purpose: a warehouse table name has no reason to need quoting,
# and the alternative to rejecting the odd one is interpolating it into DDL.
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _apply_resource_limits(conn: duckdb.DuckDBPyConnection) -> duckdb.DuckDBPyConnection:
    """Cap what any single query on this connection may consume.

    Applied to every connection and cursor. Without it, an accidental cross join
    over the fact tables is bounded only by available RAM -- the failure mode is
    the host swapping, not a query that errors and can be reported back to the
    user. A cheap ``SET`` makes that failure loud and local instead.
    """
    conn.execute(f"SET memory_limit='{PIPELINE.warehouse_memory_limit}'")
    conn.execute(f"SET threads={PIPELINE.warehouse_threads}")
    return conn


def _source_csvs(domain: Domain) -> dict[str, Path]:
    """Which CSV backs each warehouse table, per the domain manifest.

    The table names are interpolated into DDL below, which is only safe because
    they come from a manifest on disk -- never from a question, a prompt or a
    generated query. They are checked against a conservative identifier pattern
    first, so a hand-edited manifest cannot smuggle SQL into a CREATE TABLE.
    """
    tables = {}
    for table, csv_path in domain.tables.items():
        if not _SAFE_IDENTIFIER.fullmatch(table):
            raise WarehouseError(
                f"Domain {domain.name!r} declares an unusable table name: {table!r}. "
                "Table names must be plain identifiers."
            )
        tables[table] = csv_path
    return tables


def init_database(force: bool = False, domain: Domain | None = None) -> duckdb.DuckDBPyConnection:
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
    domain = domain or active_domain()
    db_path = domain.warehouse_path
    if force and db_path.exists():
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        conn = _apply_resource_limits(duckdb.connect(str(db_path)))

        for table, csv_path in _source_csvs(domain).items():
            if not csv_path.exists():
                raise WarehouseError(
                    f"Source file for table {table!r} is missing: {csv_path}. "
                    f"Every domain's tables are generated -- run "
                    f"`{domain.build_command}` to produce {domain.name}'s."
                )
            conn.execute(
                f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM read_csv_auto(?, header=True)",
                [str(csv_path)],
            )
    except duckdb.Error as exc:
        raise WarehouseError(f"Could not initialize the DuckDB warehouse at {db_path}: {exc}") from exc
    except OSError as exc:
        raise WarehouseError(f"Could not read the source CSV files for the warehouse: {exc}") from exc

    return conn


def get_connection(domain: Domain | None = None) -> duckdb.DuckDBPyConnection:
    """Return a connection to the warehouse, initializing it on first use.

    Raises
    ------
    WarehouseError
        If the DuckDB file exists but can't be opened -- e.g. it's locked by
        another process, or the file is corrupt.
    """
    domain = domain or active_domain()
    if not domain.warehouse_path.exists():
        return init_database(domain=domain)
    try:
        # read_only, because nothing in the serving path writes: init_database is
        # the only writer and it runs at build time. This is not just belt-and-
        # braces -- duckdb.connect() defaults to read-write, which *demands* write
        # permission on the file even when no write ever happens. The container
        # runs as a non-root user against a root-owned warehouse precisely so a
        # mutation that slipped past the validator would hit a read-only
        # filesystem, and a read-write open turns that hardening into a
        # startup crash.
        return _apply_resource_limits(
            duckdb.connect(str(domain.warehouse_path), read_only=True)
        )
    except duckdb.Error as exc:
        raise WarehouseError(
            f"Could not open the DuckDB warehouse at {domain.warehouse_path}: {exc}. "
            "It may be locked by another process or corrupted -- close any other "
            "connection to this file, or delete it and re-run to rebuild from source CSVs."
        ) from exc


def open_cursor(conn: duckdb.DuckDBPyConnection) -> duckdb.DuckDBPyConnection:
    """A private cursor over the same database, for one unit of work.

    A ``DuckDBPyConnection`` is not safe to use from more than one thread at a
    time, so sharing a single connection across concurrent requests -- which any
    server in front of this pipeline will do -- corrupts results or crashes. A
    cursor is an independent connection to the same database, so each request can
    hold its own, and interrupting one on timeout cancels only that request.
    """
    return _apply_resource_limits(conn.cursor())


def execute_guarded(
    conn: duckdb.DuckDBPyConnection,
    sql: str,
    params: list[Any] | None = None,
    timeout_seconds: float | None = None,
) -> pd.DataFrame:
    """Run ``sql`` and return its rows, or raise if it outruns the timeout.

    DuckDB exposes no per-statement timeout, so the bound is enforced by arming a
    timer that calls ``conn.interrupt()``. The interrupt surfaces as an ordinary
    ``duckdb.Error`` from ``execute``, which is indistinguishable from any other
    query error -- hence the ``timed_out`` flag, set by the timer itself, to tell
    "this query was cancelled" apart from "this query was wrong".

    Raises
    ------
    QueryTimeoutError
        The query was still running when the timeout elapsed.
    duckdb.Error
        The query failed for any other reason; the caller reports it as an
        execution failure.
    """
    timeout = PIPELINE.query_timeout_seconds if timeout_seconds is None else timeout_seconds
    timed_out = threading.Event()

    def _interrupt() -> None:
        timed_out.set()
        try:
            conn.interrupt()
        except Exception:  # pragma: no cover - interrupting a finished query is a no-op
            logger.debug("Interrupt fired after the query had already completed.")

    timer = threading.Timer(timeout, _interrupt)
    timer.daemon = True
    timer.start()
    try:
        result = conn.execute(sql, params) if params else conn.execute(sql)
        return result.fetchdf()
    except Exception as exc:
        if timed_out.is_set():
            raise QueryTimeoutError(sql, timeout) from exc
        raise
    finally:
        timer.cancel()

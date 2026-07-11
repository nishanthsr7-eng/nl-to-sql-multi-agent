"""Shared pytest fixtures for the Semantic Query Engine test suite.

Sets ``SQE_DUCKDB_PATH`` to a private, session-scoped temp file *before*
any ``semantic_query_engine`` module is imported, so running the test suite never rebuilds
or otherwise touches the developer's real warehouse file at
``data/warehouse/semantic_query_engine.duckdb`` (ARCHITECTURE_REVIEW.md §9). This has to
happen at module import time, ahead of the `semantic_query_engine.core.config` import below
and any test module's own imports -- config.py resolves the path once, from
the environment, at first import.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TEST_DB_PATH = Path(tempfile.gettempdir()) / "sqe_pytest" / "test.duckdb"
os.environ["SQE_DUCKDB_PATH"] = str(_TEST_DB_PATH)

import pytest  # noqa: E402

from semantic_query_engine.warehouse.duckdb_client import init_database  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _rebuild_warehouse():
    """Rebuild the ephemeral test warehouse from source CSVs once per session."""
    _TEST_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    init_database(force=True)

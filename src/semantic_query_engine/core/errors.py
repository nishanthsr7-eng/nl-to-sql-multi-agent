"""Typed exceptions for the analytics pipeline.

Agents raise these instead of returning ad-hoc error dictionaries, so callers
(the orchestrator, the UI, tests) can branch on exception type rather than on
string content.
"""

from __future__ import annotations


class SemanticQueryEngineError(Exception):
    """Base class for all application-raised errors."""


class ClarificationNeededError(SemanticQueryEngineError):
    """The question is too ambiguous to plan; the user must be asked to clarify."""

    def __init__(self, prompt: str, missing_params: list[str] | None = None):
        super().__init__(prompt)
        self.prompt = prompt
        self.missing_params = missing_params or []


class SQLValidationError(SemanticQueryEngineError):
    """Generated SQL failed validation and could not be repaired in time."""

    def __init__(self, errors: list[str], sql: str):
        super().__init__("; ".join(errors) or "SQL validation failed")
        self.errors = errors
        self.sql = sql


class QueryExecutionError(SemanticQueryEngineError):
    """Validated SQL failed to execute against the warehouse."""

    def __init__(self, message: str, sql: str):
        super().__init__(message)
        self.sql = sql


class NoDataError(SemanticQueryEngineError):
    """The query executed but returned zero rows."""

    def __init__(self, sql: str):
        super().__init__("No data matched the request")
        self.sql = sql


class WarehouseError(SemanticQueryEngineError):
    """The DuckDB warehouse file could not be opened or (re)built.

    Raised for failures outside the application's control -- a locked file (another
    process has it open for write), a corrupt file, or a permissions error -- so the
    UI can show a readable message instead of an unhandled ``duckdb.Error`` traceback.
    """

    def __init__(self, message: str):
        super().__init__(message)

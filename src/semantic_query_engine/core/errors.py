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

    def __init__(self, errors: list[str], sql: str, issue_codes: list[str] | None = None):
        super().__init__("; ".join(errors) or "SQL validation failed")
        self.errors = errors
        self.sql = sql
        # Stable codes for the issues behind ``errors`` -- see
        # :class:`~semantic_query_engine.agents.validator.IssueCode`. Carried
        # through to the Failure result so rejection reasons can be counted
        # across an evaluation run without parsing the messages.
        self.issue_codes = issue_codes or []


class QueryExecutionError(SemanticQueryEngineError):
    """Validated SQL failed to execute against the warehouse."""

    def __init__(self, message: str, sql: str):
        super().__init__(message)
        self.sql = sql


class QueryTimeoutError(SemanticQueryEngineError):
    """A validated query was still running when the wall-clock budget elapsed.

    Distinct from :class:`QueryExecutionError` because it is not a defect in the
    SQL -- the query was well-formed and simply too expensive. That difference
    matters to the caller (a timeout is worth reporting as its own failure mode
    and counting separately) and to the user, who should be told to narrow the
    question rather than that their question was invalid.
    """

    def __init__(self, sql: str, timeout_seconds: float):
        super().__init__(
            f"Query exceeded the {timeout_seconds:g}s time limit and was cancelled. "
            "Narrow the timeframe or product scope and try again."
        )
        self.sql = sql
        self.timeout_seconds = timeout_seconds


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

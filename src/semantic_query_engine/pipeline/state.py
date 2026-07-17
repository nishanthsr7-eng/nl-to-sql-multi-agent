"""Trace accumulator for one pipeline run.

This is *not* state that flows into agents -- every agent still receives its
inputs as explicit arguments, and that's deliberate: it keeps each agent
independently callable and testable without constructing a shared object first.
``RunTrace`` only accumulates what happened, for the UI's "Details" panel and
for anything (a caller, a test) that wants an audit trail after the fact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RunTrace:
    question: str
    intent: str = ""
    needs_clarification: bool = False
    clarification_prompt: str | None = None
    schema_context: str = ""
    sql: str = ""
    sql_source: str = ""
    validation_errors: list[str] = field(default_factory=list)
    result_rows: list[dict[str, Any]] = field(default_factory=list)
    agent_trace: list[str] = field(default_factory=list)

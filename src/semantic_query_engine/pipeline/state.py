"""Trace accumulator for one pipeline run.

This is *not* state that flows into agents -- every agent still receives its
inputs as explicit arguments, and that's deliberate: it keeps each agent
independently callable and testable without constructing a shared object first.
``RunTrace`` only accumulates what happened, for ``sqe ask --explain`` and for
anything (a caller, a test, the Phase 3 eval harness) that wants an audit trail
after the fact.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from semantic_query_engine.core.usage import NO_USAGE, TokenUsage
from semantic_query_engine.governance.telemetry import SpanRecorder


def new_run_id() -> str:
    """A short, unique identifier for one run.

    Threaded through the structured logs, the trace and the audit record so a
    line in any one of the three can be tied to the other two. Short because it
    is read by humans grepping a log, not by a database.
    """
    return uuid.uuid4().hex[:16]


@dataclass
class RunTrace:
    question: str
    # Ties this run's log lines, spans and audit record together.
    run_id: str = field(default_factory=new_run_id)
    # Who the run executed as, and what governance did to it. Kept on the trace
    # as well as on the result because the audit log is written from the trace:
    # a run that failed still ran as somebody, and "who asked" is the first
    # column of any governance record worth keeping.
    principal: str = "steward"
    role: str = "data_steward"
    applied_policies: list[dict[str, str]] = field(default_factory=list)
    masked_columns: list[dict[str, str]] = field(default_factory=list)
    intent: str = ""
    needs_clarification: bool = False
    clarification_prompt: str | None = None
    schema_context: str = ""
    sql: str = ""
    # The SQL the warehouse actually saw: bounded, and carrying any injected row
    # predicates. Kept apart from ``sql`` (what the model wrote) because on a
    # governed run the two differ, and the audit log has no business recording
    # the one that did not execute.
    executed_sql: str = ""
    sql_source: str = ""
    # Set when the SQL came from the semantic cache rather than a generation.
    # Carried on the trace rather than only in ``sql_source`` so the eval
    # harness can exclude cached cases from an accuracy number without parsing a
    # string: a cached run measures the cache, not the model.
    cache_hit: bool = False
    cache_similarity: float = 0.0
    cache_backend: str = ""
    # Set once the validator has accepted a query; distinguishes "never rejected"
    # from "rejected then repaired" when reading ``rejections``.
    validated: bool = False
    validation_errors: list[str] = field(default_factory=list)
    # One entry per validation attempt that was *rejected*, in order, holding the
    # codes that attempt was rejected for. Attempt 0 is the model's first
    # generation; attempt N is the output of the Nth repair.
    #
    # The shape matters: the funnel's two headline numbers are "what fraction of
    # first-attempt generations were rejected, and for what" and "of those, what
    # fraction were repaired within budget, by attempt number". Both need the
    # rejections kept per attempt. A flat list can answer the first question and
    # not the second, which is why this replaced one.
    rejections: list[list[str]] = field(default_factory=list)
    repair_attempts: int = 0
    result_rows: list[dict[str, Any]] = field(default_factory=list)
    agent_trace: list[str] = field(default_factory=list)
    # Provider spend for the run so far, summed across generation, every repair
    # attempt and synthesis. Accumulated here rather than in a module-level
    # counter so two concurrent API requests cannot bill each other -- the same
    # reason each request gets its own cursor.
    usage: TokenUsage = NO_USAGE
    # Per-stage timings for this run. Alongside ``agent_trace`` rather than
    # replacing it: the prose is the better artefact for one query and is what
    # ``--explain`` renders, but it cannot answer "which stage is slow", because
    # that answer is a distribution over many runs.
    spans: SpanRecorder = field(default_factory=SpanRecorder)

    @property
    def stage_latency_ms(self) -> dict[str, float]:
        """Milliseconds per stage -- the latency breakdown for this run."""
        return self.spans.breakdown()

    @property
    def validation_issue_codes(self) -> list[str]:
        """Every rejection code seen this run, flattened, earliest attempt first.

        Kept as a derived view rather than a second field so the two can never
        disagree. Callers that want to attribute a code to an attempt read
        :attr:`rejections` directly.
        """
        return [code for attempt in self.rejections for code in attempt]

    @property
    def first_attempt_rejected(self) -> bool:
        """True when the model's initial generation did not pass the validator."""
        return bool(self.rejections)

    @property
    def repaired(self) -> bool:
        """True when a rejected generation was subsequently fixed within budget.

        ``rejections`` records only attempts that failed, so a run that was
        rejected at least once and still produced SQL the validator accepted is
        exactly one where a repair worked.
        """
        return self.first_attempt_rejected and self.validated

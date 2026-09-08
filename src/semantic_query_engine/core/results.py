"""The three things a pipeline run can produce, as one discriminated union.

:meth:`~semantic_query_engine.pipeline.orchestrator.AnalyticsPipeline.run` used to
return ``StructuredResponse | dict``, and every caller told the two apart with
``isinstance(response, dict)`` before reaching into untyped keys. That worked, but
it meant the failure and clarification paths -- the two the guardrails exist to
produce -- were the only parts of the system with no schema at all: a typo in
``response.get("clarification_prompt")`` failed silently, and nothing could
enumerate *why* a run failed without string-matching the message.

Each outcome is now its own dataclass carrying a ``kind`` discriminant, so callers
branch on ``result.kind`` and get attribute access, type checking, and
exhaustiveness from mypy. Every variant also serialises via :meth:`to_dict`, which
is what the JSON output mode and the eval harness consume -- the human-readable
renderers are formatters over that same payload rather than a parallel code path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from semantic_query_engine.core.usage import NO_USAGE, TokenUsage

# Why a run ended without an answer. This is a closed set rather than free text
# because it is the natural group-by for the evaluation funnel ("what fraction of
# questions fail, and at which stage?"); a message string cannot be aggregated.
FailureReason = Literal[
    "validation_failed",   # SQL never passed the validator, repair budget exhausted
    "execution_failed",    # validated SQL still errored in DuckDB
    "query_timeout",       # execution exceeded PipelineSettings.query_timeout_seconds
    "no_data",             # executed cleanly, matched zero rows
]


@dataclass(frozen=True)
class GovernanceRecord:
    """What governance did to one run, carried on whichever outcome it produced.

    On every variant rather than only on an answer, because the interesting
    governance events are not all successes: a query refused for exposing
    personal data, or one a restricted principal ran to zero rows, is exactly
    the case a reader of the audit trail is looking for. An empty record --
    the unrestricted default -- serialises to an empty policy list and a
    principal id, so a consumer never has to branch on its absence.
    """

    principal: str = "steward"
    role: str = "data_steward"
    # Row predicates injected for this run; see governance.row_security.
    applied_policies: list[dict[str, str]] = field(default_factory=list)
    # Output columns rendered as masked; see governance.masking.
    masked_columns: list[dict[str, str]] = field(default_factory=list)

    @property
    def restricted(self) -> bool:
        return bool(self.applied_policies)

    def to_dict(self) -> dict[str, Any]:
        return {
            "principal": self.principal,
            "role": self.role,
            "restricted": self.restricted,
            "applied_policies": self.applied_policies,
            "masked_columns": self.masked_columns,
        }


NO_GOVERNANCE = GovernanceRecord()


@dataclass
class StructuredResponse:
    """A successful answer, in five clearly separated output layers."""

    # Layer 1 -- 2-3 sentence plain-English answer
    narrative_summary: str
    # Layer 2 -- primary number prominently surfaced (e.g. "£2.86M total revenue")
    key_metric: str | None
    # Layer 3 -- baseline vs period, or entity vs entity
    comparison_context: str | None
    # Layer 4 -- suggested visualisation type
    chart_recommendation: str
    # Layer 5 -- SQL for analyst validation (explainability trace)
    sql_query: str
    # Supporting fields
    result_table: list[dict[str, Any]] = field(default_factory=list)
    intent: str = ""
    archetype_label: str = ""          # e.g. "B - Comparative Analysis"
    archetype_description: str = ""    # one-liner about the archetype
    agent_trace: list[str] = field(default_factory=list)
    # Transparency metadata -- surfaced so users can see whether an answer came
    # from the LLM or the deterministic fallback, and how long it took.
    sql_source: str = ""
    elapsed_ms: float = 0.0
    # True when the validator capped an unbounded query at max_result_rows, so a
    # caller can say "showing the first N rows" instead of implying completeness.
    truncated: bool = False
    # What the run spent with the provider. Carried on every variant, not just on
    # an answer: a run that burned three generations and still failed validation
    # cost real money, and a cost-per-query figure that counted only the successes
    # would understate the price of the guardrails it is meant to justify.
    usage: TokenUsage = NO_USAGE
    # See GovernanceRecord -- carried on every variant.
    governance: GovernanceRecord = NO_GOVERNANCE
    # Milliseconds per pipeline stage. On every variant because the breakdown
    # of a run that *failed* is the more useful one: "elapsed 9s" says a run
    # was slow, "generation 7.9s" says which stage to go and look at.
    stage_latency_ms: dict[str, float] = field(default_factory=dict)
    kind: Literal["answer"] = "answer"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "narrative_summary": self.narrative_summary,
            "key_metric": self.key_metric,
            "comparison_context": self.comparison_context,
            "chart_recommendation": self.chart_recommendation,
            "sql_query": self.sql_query,
            "result_table": self.result_table,
            "intent": self.intent,
            "archetype_label": self.archetype_label,
            "archetype_description": self.archetype_description,
            "agent_trace": self.agent_trace,
            "sql_source": self.sql_source,
            "elapsed_ms": self.elapsed_ms,
            "truncated": self.truncated,
            "usage": self.usage.to_dict(),
            "governance": self.governance.to_dict(),
            "stage_latency_ms": self.stage_latency_ms,
        }


@dataclass
class Clarification:
    """The question was too ambiguous to plan; the user is asked for the missing scope."""

    prompt: str
    missing_params: list[str] = field(default_factory=list)
    intent: str = ""
    agent_trace: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0
    # See StructuredResponse.usage -- every variant carries it.
    usage: TokenUsage = NO_USAGE
    # See GovernanceRecord -- carried on every variant.
    governance: GovernanceRecord = NO_GOVERNANCE
    # Milliseconds per pipeline stage. On every variant because the breakdown
    # of a run that *failed* is the more useful one: "elapsed 9s" says a run
    # was slow, "generation 7.9s" says which stage to go and look at.
    stage_latency_ms: dict[str, float] = field(default_factory=dict)
    kind: Literal["clarification"] = "clarification"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "prompt": self.prompt,
            "missing_params": self.missing_params,
            "intent": self.intent,
            "agent_trace": self.agent_trace,
            "elapsed_ms": self.elapsed_ms,
            "usage": self.usage.to_dict(),
            "governance": self.governance.to_dict(),
            "stage_latency_ms": self.stage_latency_ms,
        }


@dataclass
class Failure:
    """The run was stopped by a guardrail, an execution error, or an empty result."""

    reason: FailureReason
    message: str
    details: list[str] = field(default_factory=list)
    sql_query: str = ""
    agent_trace: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0
    # Machine-readable codes for the validator issues that caused a
    # ``validation_failed`` run, so rejection reasons can be counted across an
    # eval run without parsing the human-readable messages in ``details``.
    issue_codes: list[str] = field(default_factory=list)
    # See StructuredResponse.usage -- every variant carries it.
    usage: TokenUsage = NO_USAGE
    # See GovernanceRecord -- carried on every variant.
    governance: GovernanceRecord = NO_GOVERNANCE
    # Milliseconds per pipeline stage. On every variant because the breakdown
    # of a run that *failed* is the more useful one: "elapsed 9s" says a run
    # was slow, "generation 7.9s" says which stage to go and look at.
    stage_latency_ms: dict[str, float] = field(default_factory=dict)
    kind: Literal["failure"] = "failure"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "reason": self.reason,
            "message": self.message,
            "details": self.details,
            "sql_query": self.sql_query,
            "agent_trace": self.agent_trace,
            "elapsed_ms": self.elapsed_ms,
            "issue_codes": self.issue_codes,
            "usage": self.usage.to_dict(),
            "governance": self.governance.to_dict(),
            "stage_latency_ms": self.stage_latency_ms,
        }


# The full result type of one pipeline run. Callers branch on ``result.kind``;
# mypy narrows each branch to the matching dataclass.
QueryResult = StructuredResponse | Clarification | Failure


__all__ = [
    "NO_GOVERNANCE",
    "Clarification",
    "GovernanceRecord",
    "Failure",
    "FailureReason",
    "QueryResult",
    "StructuredResponse",
    "TokenUsage",
]

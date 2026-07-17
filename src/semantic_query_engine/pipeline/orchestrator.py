"""Multi-agent orchestrator for NL analytics.

Chains Planner -> SchemaRetriever -> SQLGenerator -> Validator (with a bounded
repair loop) -> execution -> Synthesis, appending a human-readable trace at
each step. Internally, each failure mode raises one of the typed exceptions in
:mod:`semantic_query_engine.core.errors` -- :meth:`run` is the single place that catches them
and converts them to the plain dict the UI renders (``isinstance(response, dict)``
tells a clarification/error response apart from a successful
:class:`~semantic_query_engine.agents.synthesis.StructuredResponse`).
"""

from __future__ import annotations

import time

import duckdb

from semantic_query_engine.agents.planner import PlannerAgent
from semantic_query_engine.agents.schema_retriever import SchemaRetrieverAgent
from semantic_query_engine.agents.sql_generator import SQLGeneratorAgent
from semantic_query_engine.agents.synthesis import StructuredResponse, SynthesisAgent
from semantic_query_engine.agents.validator import ValidatorAgent
from semantic_query_engine.core.config import PIPELINE
from semantic_query_engine.core.errors import (
    ClarificationNeededError,
    NoDataError,
    QueryExecutionError,
    SQLValidationError,
)
from semantic_query_engine.core.logging import get_logger
from semantic_query_engine.pipeline.state import RunTrace
from semantic_query_engine.warehouse.duckdb_client import get_connection

logger = get_logger(__name__)


class AnalyticsPipeline:
    MAX_REPAIR_ATTEMPTS = PIPELINE.max_repair_attempts

    def __init__(self, conn: duckdb.DuckDBPyConnection | None = None):
        """``conn`` lets a caller share one warehouse connection across multiple
        pipeline instances (e.g. one per Streamlit session) instead of each one
        opening its own via ``get_connection()`` -- see ``ui/app.py``'s
        ``get_shared_connection``, which is process-cached via ``st.cache_resource``.
        """
        self.planner = PlannerAgent()
        self.retriever = SchemaRetrieverAgent()
        self.generator = SQLGeneratorAgent()
        self.validator = ValidatorAgent()
        self.synthesis = SynthesisAgent()
        self.conn = conn or get_connection()

    def run(self, question: str, context: list[str] | None = None) -> StructuredResponse | dict:
        """Run one question through the pipeline.

        ``context`` is prior turns in the conversation (oldest first). It is only
        consulted when this question alone is ambiguous -- see
        :meth:`_effective_question`. Passing it lets a clarification follow-up like
        "revenue, last month" be understood in light of the question that prompted it.
        """
        state = RunTrace(question=question)
        started_at = time.perf_counter()

        def elapsed_ms() -> float:
            return round((time.perf_counter() - started_at) * 1000, 1)

        try:
            response = self._run(question, context or [], state)
            response.elapsed_ms = elapsed_ms()
            return response
        except ClarificationNeededError as exc:
            state.needs_clarification = True
            state.clarification_prompt = exc.prompt
            return {
                "needs_clarification": True,
                "clarification_prompt": exc.prompt,
                "intent": state.intent,
                "agent_trace": state.agent_trace,
                "elapsed_ms": elapsed_ms(),
            }
        except SQLValidationError as exc:
            state.validation_errors = exc.errors
            return {
                "error": "SQL validation failed",
                "details": exc.errors,
                "sql_query": exc.sql,
                "agent_trace": state.agent_trace,
                "elapsed_ms": elapsed_ms(),
            }
        except QueryExecutionError as exc:
            return {
                "error": "Query execution failed",
                "details": [str(exc)],
                "sql_query": exc.sql,
                "agent_trace": state.agent_trace,
                "elapsed_ms": elapsed_ms(),
            }
        except NoDataError as exc:
            return {
                "error": "No data matched the request",
                "details": ["Try broadening the time period or product scope."],
                "sql_query": exc.sql,
                "agent_trace": state.agent_trace,
                "elapsed_ms": elapsed_ms(),
            }

    def _run(self, question: str, context: list[str], state: RunTrace) -> StructuredResponse:
        plan = self.planner.run(question)

        # A clarification follow-up on its own is often still ambiguous (e.g. "revenue,
        # last month" has no product scope). Retry once against the prior turn merged in
        # before giving up and asking again -- see ARCHITECTURE_REVIEW.md §10.
        effective_question = question
        if plan.needs_clarification and context:
            merged = f"{context[-1]}. {question}"
            merged_plan = self.planner.run(merged)
            if not merged_plan.needs_clarification:
                plan = merged_plan
                effective_question = merged
                state.agent_trace.append(f"Planner: merged with prior turn -> \"{merged}\"")

        state.intent = plan.intent.value
        state.agent_trace.append(f"Planner: {plan.reasoning} | archetype={plan.archetype_label}")

        if plan.needs_clarification:
            raise ClarificationNeededError(plan.clarification_prompt or "", plan.missing_params)

        context_result = self.retriever.run(effective_question)
        state.schema_context = context_result.formatted_context
        state.agent_trace.append(
            f"Schema Retriever: tables={[t['table_name'] for t in context_result.tables]}"
        )

        generated = self.generator.run(
            effective_question, context_result.formatted_context, plan.intent.value, metrics=context_result.metrics
        )
        state.sql = generated.sql
        state.sql_source = generated.source
        state.agent_trace.append(
            f"SQL Generator: source={generated.source} | "
            f"metrics_injected={[m.get('metric_name') for m in context_result.metrics]}"
        )

        validation = self.validator.run(generated.sql, self.conn, question=effective_question, params=generated.params)
        repair_attempt = 0
        while not validation.is_valid and repair_attempt < self.MAX_REPAIR_ATTEMPTS:
            repair_attempt += 1
            state.agent_trace.append(
                f"Validator: failed attempt {repair_attempt} -- {'; '.join(validation.errors)}"
            )
            previous_sql = generated.sql
            generated = self.generator.repair(
                question=effective_question,
                schema_context=context_result.formatted_context,
                intent=plan.intent.value,
                failed_sql=generated.sql,
                validation_errors=validation.errors,
                metrics=context_result.metrics,
            )
            state.sql = generated.sql
            state.sql_source = generated.source
            state.agent_trace.append(f"SQL Repair {repair_attempt}: source={generated.source}")
            if generated.sql == previous_sql:
                # No LLM was available to act on the validator's feedback, so the
                # deterministic fallback reproduced the identical failing query --
                # further attempts would just repeat this forever. Stop now instead
                # of burning the remaining repair budget on a query that can't change.
                state.agent_trace.append(
                    "Validator: repair produced identical SQL -- no further attempts would help; stopping."
                )
                break
            validation = self.validator.run(
                generated.sql, self.conn, question=effective_question, params=generated.params
            )

        if not validation.is_valid:
            state.agent_trace.append(f"Validator: failed -- {'; '.join(validation.errors)}")
            raise SQLValidationError(validation.errors, generated.sql)

        state.agent_trace.append("Validator: passed")
        try:
            if generated.params:
                df = self.conn.execute(validation.sanitized_sql, generated.params).fetchdf()
            else:
                df = self.conn.execute(validation.sanitized_sql).fetchdf()
        except Exception as exc:
            logger.warning("Query execution failed: %s", exc)
            state.agent_trace.append(f"Execution failed: {exc}")
            raise QueryExecutionError(str(exc), validation.sanitized_sql) from exc

        if df.empty:
            state.agent_trace.append("Execution: returned 0 rows")
            raise NoDataError(validation.sanitized_sql)

        state.agent_trace.append(f"Execution: returned {len(df)} rows")

        response = self.synthesis.run(
            question=effective_question,
            sql=validation.sanitized_sql,
            df=df,
            intent=plan.intent.value,
            agent_trace=state.agent_trace,
            archetype_label=plan.archetype_label,
            archetype_description=plan.archetype_description,
            sql_source=state.sql_source,
        )
        state.agent_trace.append("Synthesis: structured response ready")
        return response

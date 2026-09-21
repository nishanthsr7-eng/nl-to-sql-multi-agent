"""Multi-agent orchestrator for NL analytics.

Chains Planner -> SchemaRetriever -> SQLGenerator -> Validator (with a bounded
repair loop) -> execution -> Synthesis, appending a human-readable trace at each
step. Internally, each failure mode raises one of the typed exceptions in
:mod:`semantic_query_engine.core.errors`; :meth:`run` is the single place that
catches them and turns them into the matching variant of
:data:`~semantic_query_engine.core.results.QueryResult`.

Callers branch on ``result.kind`` -- ``"answer"``, ``"clarification"`` or
``"failure"`` -- rather than on ``isinstance(response, dict)``, so the two
non-answer paths are as typed as the successful one.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import duckdb

from semantic_query_engine.agents.planner import PlannerAgent
from semantic_query_engine.agents.schema_retriever import SchemaRetrieverAgent
from semantic_query_engine.agents.sql_generator import SQLGenerationResult, SQLGeneratorAgent
from semantic_query_engine.agents.synthesis import SynthesisAgent
from semantic_query_engine.agents.validator import ValidationResult, ValidatorAgent
from semantic_query_engine.core.config import FULL_PIPELINE, PIPELINE, AblationConfig
from semantic_query_engine.core.domains import active_domain
from semantic_query_engine.core.errors import (
    ClarificationNeededError,
    NoDataError,
    QueryExecutionError,
    QueryTimeoutError,
    SQLValidationError,
)
from semantic_query_engine.core.logging import get_logger
from semantic_query_engine.core.results import (
    Clarification,
    Failure,
    GovernanceRecord,
    QueryResult,
    StructuredResponse,
)
from semantic_query_engine.core.semantic_cache import SemanticCache, build_semantic_cache
from semantic_query_engine.governance.audit import (
    AuditRecord,
    audit_enabled,
    audit_log,
    utc_now,
)
from semantic_query_engine.governance.masking import mask_dataframe
from semantic_query_engine.governance.principals import STEWARD, Principal
from semantic_query_engine.governance.telemetry import run_context
from semantic_query_engine.pipeline.state import RunTrace
from semantic_query_engine.warehouse.duckdb_client import (
    execute_guarded,
    get_connection,
    open_cursor,
)
from semantic_query_engine.warehouse.schema_dump import raw_schema_context

logger = get_logger(__name__)


class AnalyticsPipeline:
    MAX_REPAIR_ATTEMPTS = PIPELINE.max_repair_attempts

    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection | None = None,
        ablation: AblationConfig = FULL_PIPELINE,
        cache: SemanticCache | None = None,
    ):
        """``conn`` lets a caller share one warehouse connection across pipeline
        instances instead of each one opening its own via ``get_connection()``.

        The shared connection is never used directly for queries: every
        :meth:`run` takes its own cursor from it (see
        :func:`~semantic_query_engine.warehouse.duckdb_client.open_cursor`),
        because a DuckDB connection is not safe to use from two threads at once
        and anything serving concurrent requests would otherwise share one.
        """
        self.planner = PlannerAgent()
        self.retriever = SchemaRetrieverAgent()
        self.generator = SQLGeneratorAgent()
        self.validator = ValidatorAgent()
        self.synthesis = SynthesisAgent()
        self.conn = conn or get_connection()
        # Which guardrails are live for this instance. Defaults to the full
        # pipeline, so every ordinary caller gets the product; the evaluation
        # ladder constructs one instance per rung with stages switched off. The
        # toggles are read inside ``_run`` rather than swapping agents at
        # construction, so the trace records the same stage names either way and
        # a rung is legible as "the pipeline, minus X".
        self.ablation = ablation
        self.max_repair_attempts = ablation.max_repair_attempts
        # Off unless a caller hands one in or the settings ask for it. A cache
        # that defaulted to on would silently change what every evaluation
        # measures, and a run that reported a previous question's SQL at zero
        # cost would look like a very good model.
        self.cache = cache
        if self.cache is None and PIPELINE.semantic_cache_enabled:
            self.cache = build_semantic_cache()

    def run(
        self,
        question: str,
        context: list[str] | None = None,
        principal: Principal | None = None,
    ) -> QueryResult:
        """Run one question through the pipeline.

        ``context`` is prior turns in the conversation (oldest first). It is only
        consulted when this question alone is ambiguous -- see :meth:`_run`.
        Passing it lets a clarification follow-up like "revenue, last month" be
        understood in light of the question that prompted it.

        ``principal`` is who the run executes as. It is an argument rather than
        pipeline state because the API serves many callers from one pipeline;
        holding the identity on the instance would let two concurrent requests
        answer under each other grants, which is the same hazard the
        per-request cursor exists to avoid. It defaults to the unrestricted
        steward -- see ``governance.principals``.
        """
        return self.run_traced(question, context, principal)[0]

    def run_traced(
        self,
        question: str,
        context: list[str] | None = None,
        principal: Principal | None = None,
    ) -> tuple[QueryResult, RunTrace]:
        """:meth:`run`, plus the :class:`RunTrace` the run accumulated.

        The evaluation harness needs what the result deliberately does not carry:
        a *successful* answer's ``Failure.issue_codes`` is empty by construction,
        so the validator rejections that a repair went on to fix -- the numerator
        of the whole funnel -- are invisible to a caller that only sees the
        result. Rather than widen ``StructuredResponse`` with fields no product
        surface renders, the trace is returned alongside it and ``run`` stays the
        narrow contract every other caller branches on.
        """
        acting = principal or STEWARD
        state = RunTrace(question=question, principal=acting.id, role=acting.role)
        started_at = time.perf_counter()

        def elapsed_ms() -> float:
            return round((time.perf_counter() - started_at) * 1000, 1)

        def governance() -> GovernanceRecord:
            """The governance block as it stands *now*.

            Built per outcome rather than once up front: a run rejected by the
            PII check never reached policy injection, and a record claiming
            predicates that were never built would be a comfortable lie in the
            one log that exists to be uncomfortable.
            """
            return GovernanceRecord(
                principal=acting.id,
                role=acting.role,
                applied_policies=state.applied_policies,
                masked_columns=state.masked_columns,
            )

        # Every log line emitted anywhere beneath this -- including from agents
        # three frames down that know nothing about runs -- carries this run id,
        # which is what ties a log line to its audit record and its spans. The
        # audit write and the summary line are inside it too: a line *about* a
        # run that does not carry the run id is the one line you most want to
        # grep for and cannot.
        with run_context(state.run_id):
            cursor = open_cursor(self.conn)
            try:
                result = self._dispatch(question, context or [], state, cursor, acting,
                                        governance, elapsed_ms)
            finally:
                cursor.close()
        # Logged after the cursor is released and outside every ``except``: the
        # audit record is written for *whatever* happened, including a guardrail
        # refusal or a timeout. A log that only recorded answers would be a usage
        # report -- the rows worth having are the ones where something was
        # stopped.
            result.stage_latency_ms = state.stage_latency_ms
            self._audit(state, result)
            logger.info(
                "run complete: outcome=%s stages=%s",
                result.kind,
                state.stage_latency_ms,
            )
        return result, state

    def _dispatch(
        self,
        question: str,
        context: list[str],
        state: RunTrace,
        cursor: duckdb.DuckDBPyConnection,
        acting: Principal,
        governance: Callable[[], GovernanceRecord],
        elapsed_ms: Callable[[], float],
    ) -> QueryResult:
        """Run the pipeline and turn each typed failure into its result variant."""
        try:
            response = self._run(question, context, state, cursor, acting)
            response.elapsed_ms = elapsed_ms()
            response.governance = governance()
            return response
        except ClarificationNeededError as exc:
            state.needs_clarification = True
            state.clarification_prompt = exc.prompt
            return Clarification(
                prompt=exc.prompt,
                missing_params=exc.missing_params,
                intent=state.intent,
                agent_trace=state.agent_trace,
                elapsed_ms=elapsed_ms(),
                usage=state.usage,
                governance=governance(),
            )
        except SQLValidationError as exc:
            state.validation_errors = exc.errors
            return Failure(
                reason="validation_failed",
                message="SQL validation failed",
                details=exc.errors,
                sql_query=exc.sql,
                agent_trace=state.agent_trace,
                elapsed_ms=elapsed_ms(),
                usage=state.usage,
                governance=governance(),
                issue_codes=exc.issue_codes,
            )
        except QueryTimeoutError as exc:
            return Failure(
                reason="query_timeout",
                message="Query exceeded the time limit",
                details=[str(exc)],
                sql_query=exc.sql,
                agent_trace=state.agent_trace,
                elapsed_ms=elapsed_ms(),
                usage=state.usage,
                governance=governance(),
            )
        except QueryExecutionError as exc:
            return Failure(
                reason="execution_failed",
                message="Query execution failed",
                details=[str(exc)],
                sql_query=exc.sql,
                agent_trace=state.agent_trace,
                elapsed_ms=elapsed_ms(),
                usage=state.usage,
                governance=governance(),
            )
        except NoDataError as exc:
            return Failure(
                reason="no_data",
                message="No data matched the request",
                details=["Try broadening the time period or product scope."],
                sql_query=exc.sql,
                agent_trace=state.agent_trace,
                elapsed_ms=elapsed_ms(),
                usage=state.usage,
                governance=governance(),
            )

    def _audit(self, state: RunTrace, result: QueryResult) -> None:
        """Append this run to the domain's append-only governance log.

        A logging failure is swallowed deliberately, and the choice is worth
        naming: this is an analytics engine, not a system of record, and
        refusing to return an answer the user already paid for because a log
        file was read-only would be the wrong trade. The failure is logged at
        warning level, which is the signal an operator acts on. A deployment
        where the audit trail is a compliance requirement rather than an
        engineering one should make this fatal -- that is a one-line change and
        a different product decision.
        """
        if not audit_enabled():
            return
        payload = result.to_dict()
        rows = payload.get("result_table") or []
        try:
            audit_log().append(
                AuditRecord(
                    run_id=state.run_id,
                    timestamp=utc_now(),
                    domain=active_domain().name,
                    principal=state.principal,
                    role=state.role,
                    question=state.question,
                    sql=state.executed_sql or state.sql,
                    sql_source=state.sql_source,
                    outcome=str(payload.get("reason") or payload.get("kind") or "unknown"),
                    row_count=len(rows),
                    elapsed_ms=float(payload.get("elapsed_ms") or 0.0),
                    issue_codes=list(payload.get("issue_codes") or []),
                    applied_policies=state.applied_policies,
                    masked_columns=state.masked_columns,
                    cost_usd=state.usage.cost_usd,
                )
            )
        except OSError as exc:  # pragma: no cover - depends on the filesystem
            logger.warning("Could not write the audit record for %s: %s", state.run_id, exc)

    def _validate(
        self,
        generated: SQLGenerationResult,
        conn: duckdb.DuckDBPyConnection,
        question: str,
        principal: Principal,
    ) -> ValidationResult:
        """Run the validator, or the mutation-only gate when it is ablated.

        The "no validator" rungs of the ladder still refuse to execute a
        mutation. That is not the guardrail under measurement -- no reader
        believes the interesting claim is "the semantic layer stops DROP TABLE"
        -- and running model-authored DDL against the warehouse to score a
        baseline would be an unsafe experiment rather than a rigorous one. Every
        rung therefore shares the safety floor and differs only in the schema,
        metric and bound checks, which is what the column headings claim.
        """
        if self.ablation.use_validator:
            return self.validator.run(
                generated.sql,
                conn,
                question=question,
                params=generated.params,
                principal=principal,
            )
        # An ablated rung still runs as the principal it was given, and row
        # policies are *not* part of what the ladder ablates: the ladder measures
        # the semantic guardrails, and an experiment that quietly dropped row
        # security to score a baseline would open the hole this phase exists to
        # close. A restricted principal is refused here rather than served
        # unfiltered -- see ``ValidatorAgent.safety_only``.
        return self.validator.safety_only(generated.sql, principal=principal)

    def _run(
        self,
        question: str,
        context: list[str],
        state: RunTrace,
        conn: duckdb.DuckDBPyConnection,
        principal: Principal,
    ) -> StructuredResponse:
        with state.spans.stage("planner"):
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

        with state.spans.stage(
            "schema_retrieval", semantic_layer=self.ablation.use_semantic_layer
        ):
            context_result = (
                self.retriever.run(effective_question)
                if self.ablation.use_semantic_layer
                else raw_schema_context(conn)
            )
        state.schema_context = context_result.formatted_context
        state.agent_trace.append(
            f"Schema Retriever: tables={[t['table_name'] for t in context_result.tables]}"
            + ("" if self.ablation.use_semantic_layer else " (raw schema dump -- semantic layer off)")
        )

        hit = (
            self.cache.lookup(effective_question, plan.entities) if self.cache else None
        )
        if hit is not None:
            # A hit replaces the *generation*, and nothing else. Validation,
            # LIMIT injection, EXPLAIN and execution all still happen below on
            # exactly the same path as fresh SQL -- the warehouse schema may have
            # changed since this SQL was stored, and only the validator can tell.
            generated = SQLGenerationResult(sql=hit.sql, source="cache")
            state.cache_hit = True
            state.cache_similarity = hit.similarity
            state.cache_backend = hit.backend
            state.agent_trace.append(
                f"Semantic cache: {hit.backend} hit at {hit.similarity:.3f} on "
                f"\"{hit.question}\" -- reusing its SQL, still validating it."
            )
        else:
            with state.spans.stage("sql_generation") as attributes:
                generated = self.generator.run(
                    effective_question,
                    context_result.formatted_context,
                    plan.intent.value,
                    metrics=context_result.metrics,
                )
                attributes["source"] = generated.source
            state.agent_trace.append(
                f"SQL Generator: source={generated.source} | "
                f"metrics_injected={[m.get('metric_name') for m in context_result.metrics]}"
            )
        state.sql = generated.sql
        state.sql_source = generated.source
        state.usage += generated.usage

        with state.spans.stage("validation", attempt=0) as attributes:
            validation = self._validate(generated, conn, effective_question, principal)
            attributes["valid"] = validation.is_valid
        repair_attempt = 0
        while not validation.is_valid and repair_attempt < self.max_repair_attempts:
            repair_attempt += 1
            state.rejections.append(validation.issue_codes)
            state.agent_trace.append(
                f"Validator: failed attempt {repair_attempt} -- {'; '.join(validation.errors)}"
            )
            previous_sql = generated.sql
            with state.spans.stage("sql_repair", attempt=repair_attempt) as attributes:
                generated = self.generator.repair(
                    question=effective_question,
                    schema_context=context_result.formatted_context,
                    intent=plan.intent.value,
                    failed_sql=generated.sql,
                    validation_errors=validation.errors,
                    metrics=context_result.metrics,
                )
                attributes["source"] = generated.source
            state.sql = generated.sql
            state.sql_source = generated.source
            state.usage += generated.usage
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
            with state.spans.stage("validation", attempt=repair_attempt) as attributes:
                validation = self._validate(generated, conn, effective_question, principal)
                attributes["valid"] = validation.is_valid

        if not validation.is_valid:
            state.rejections.append(validation.issue_codes)
            state.agent_trace.append(f"Validator: failed -- {'; '.join(validation.errors)}")
            raise SQLValidationError(validation.errors, generated.sql, validation.issue_codes)

        state.validated = True
        state.repair_attempts = repair_attempt
        state.applied_policies = [policy.to_dict() for policy in validation.applied_policies]
        state.masked_columns = [column.to_dict() for column in validation.masked_columns]
        if validation.applied_policies:
            state.agent_trace.append(
                f"Row security: {len(validation.applied_policies)} predicate(s) injected for "
                f"{principal.id} -- " + "; ".join(
                    f"{policy.table}: {policy.predicate}" for policy in validation.applied_policies
                )
            )
        if validation.applied_limit is not None:
            state.agent_trace.append(
                f"Validator: passed; no LIMIT was specified, capped at {validation.applied_limit} rows."
            )
        else:
            state.agent_trace.append("Validator: passed")

        state.executed_sql = validation.sanitized_sql
        try:
            with state.spans.stage("execution") as attributes:
                df = execute_guarded(conn, validation.sanitized_sql, generated.params)
                attributes["rows"] = len(df)
        except QueryTimeoutError as exc:
            state.agent_trace.append(f"Execution cancelled: {exc}")
            raise
        except Exception as exc:
            logger.warning("Query execution failed: %s", exc)
            state.agent_trace.append(f"Execution failed: {exc}")
            raise QueryExecutionError(str(exc), validation.sanitized_sql) from exc

        if df.empty:
            state.agent_trace.append("Execution: returned 0 rows")
            raise NoDataError(validation.sanitized_sql)

        # Masked before anything reads the rows -- synthesis included. See
        # ``governance.masking.mask_dataframe``.
        if validation.masked_columns:
            df = mask_dataframe(df, validation.masked_columns)
            state.agent_trace.append(
                "PII masking: "
                + ", ".join(
                    f"{column.output_name} ({column.strategy})"
                    for column in validation.masked_columns
                )
            )

        state.agent_trace.append(f"Execution: returned {len(df)} rows")

        # Stored only here: after validation *and* after the warehouse actually
        # returned rows. SQL that failed either is not a cheaper way to fail
        # next time, it is a way to make one bad generation permanent. A cache
        # hit is not re-stored -- its cost is already recorded, and overwriting
        # it with this run's zero would erase the saving it is being credited
        # with.
        if self.cache is not None and not state.cache_hit:
            self.cache.store(
                effective_question,
                validation.sanitized_sql,
                plan.entities,
                prompt_tokens=generated.usage.prompt_tokens,
                completion_tokens=generated.usage.completion_tokens,
                # None means "unpriced model"; the saving from reusing it is
                # genuinely zero dollars, not an unknown to propagate.
                cost_usd=generated.usage.cost_usd or 0.0,
            )

        with state.spans.stage("synthesis"):
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
        # Synthesis hands back only its own call. Fold it in, then overwrite with
        # the run total: what a caller wants on a result is the cost of the
        # question, not the cost of the last agent to touch it.
        state.usage += response.usage
        response.usage = state.usage
        # A full page of rows against the cap almost certainly means rows were
        # dropped, and a narrative that implies completeness would be wrong.
        response.truncated = (
            validation.applied_limit is not None and len(df) >= validation.applied_limit
        )
        state.agent_trace.append("Synthesis: structured response ready")
        return response

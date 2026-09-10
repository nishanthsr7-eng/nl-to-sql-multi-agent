"""HTTP surface over the same :class:`AnalyticsPipeline` the CLI drives.

Three things worth knowing before changing anything here.

**One pipeline, one connection, one cursor per request.** The app builds a
single ``AnalyticsPipeline`` at startup and shares it. That is only safe because
of the Phase 1 change in ``pipeline.orchestrator``: every ``run()`` takes its own
cursor from the shared DuckDB connection (``open_cursor``), and a cursor is an
independent connection to the same database. Rebuilding the pipeline per request
would instead re-open the warehouse -- and re-resolve LLM settings -- on every
call.

**The endpoints are ``def``, not ``async def``.** ``run()`` is blocking CPU/IO
work; declaring it ``async`` would run it on the event loop and serialise every
request behind the slowest query. A plain ``def`` endpoint is dispatched to
Starlette's threadpool, which is what the per-request cursor makes safe.

**The response body is the pipeline payload, unmodified.** HTTP status carries
the same distinction the CLI's exit code does -- see ``_STATUS_BY_OUTCOME``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from semantic_query_engine.api.models import HealthResponse, QueryRequest
from semantic_query_engine.core.config import load_llm_settings
from semantic_query_engine.core.logging import get_logger
from semantic_query_engine.governance.principals import PrincipalError, load_principals, resolve_principal
from semantic_query_engine.semantic.layer import load_semantic_layer

logger = get_logger(__name__)

try:
    __version__ = version("semantic-query-engine")
except PackageNotFoundError:  # pragma: no cover - source checkout without install
    __version__ = "0.0.0+local"


# An outcome -> HTTP status map, mirroring the CLI's exit codes. A clarification
# is a successful service response (the guardrail worked, the caller is asked for
# scope), so it is 200 with ``kind="clarification"`` -- not 4xx, which would tell
# a client its request was malformed when it was merely underspecified.
_STATUS_BY_OUTCOME: dict[str, int] = {
    "answer": 200,
    "clarification": 200,
    "validation_failed": 422,   # the generated SQL never passed the guardrails
    "no_data": 422,             # ran cleanly, matched nothing
    "execution_failed": 500,
    "query_timeout": 504,
}


def _status_for(payload: dict[str, Any]) -> int:
    kind = str(payload.get("kind", ""))
    key = str(payload.get("reason", "")) if kind == "failure" else kind
    return _STATUS_BY_OUTCOME.get(key, 500)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build the warehouse and the pipeline once, at startup.

    Doing it here rather than on first request means a container that cannot
    open its warehouse fails at boot -- where an orchestrator will notice --
    instead of returning 500s to the first user who asks a question.
    """
    from semantic_query_engine.pipeline.orchestrator import AnalyticsPipeline

    app.state.pipeline = AnalyticsPipeline()
    logger.info("Pipeline ready; API accepting requests.")
    try:
        yield
    finally:
        app.state.pipeline.conn.close()


app = FastAPI(
    title="Semantic Query Engine",
    version=__version__,
    summary="Governed natural-language analytics: NL question in, validated SQL and a structured answer out.",
    lifespan=lifespan,
)


@app.post("/query")
def query(request: QueryRequest) -> JSONResponse:
    """Answer one question. The body is the pipeline's result payload verbatim."""
    try:
        principal = resolve_principal(request.principal)
    except PrincipalError as exc:
        # 403 rather than 404: the caller asked to act as an identity that does
        # not exist here, which is a refusal to grant, not a missing resource.
        return JSONResponse(content={"kind": "error", "message": str(exc)}, status_code=403)

    result = app.state.pipeline.run(
        request.question, context=request.context, principal=principal
    )
    payload = result.to_dict()
    return JSONResponse(
        content=jsonable(payload),
        status_code=_status_for(payload),
        # Lets a client branch on the outcome without parsing the body, and
        # keeps the discriminant visible in access logs and traces.
        headers={"X-SQE-Result-Kind": str(payload.get("kind", ""))},
    )


@app.get("/principals")
def principals() -> dict[str, Any]:
    """The identities a caller may ask to run as, and what each may see."""
    return {
        "principals": [entry.to_dict() for entry in load_principals().all()],
    }


@app.get("/healthz", response_model=HealthResponse)
def healthz() -> HealthResponse:
    """Liveness plus a real readiness signal: the warehouse actually answers.

    A health check that only proves the process is up is worth very little; this
    one runs a query, so a corrupt or empty warehouse reports as degraded.
    """
    settings = load_llm_settings()
    try:
        rows = app.state.pipeline.conn.execute("SHOW TABLES").fetchall()
        tables = sorted(str(row[0]) for row in rows)
        status = "ok" if tables else "degraded"
        warehouse = "reachable" if tables else "empty"
    except Exception as exc:  # pragma: no cover - only on a broken warehouse
        logger.warning("Health check could not reach the warehouse: %s", exc)
        tables, status, warehouse = [], "degraded", f"unreachable: {exc}"

    return HealthResponse(
        status=status,
        warehouse=warehouse,
        tables=tables,
        llm_provider=settings.provider,
        # Reported because a keyless deployment silently answers from the
        # deterministic fallback, which is a very different system to evaluate.
        llm_enabled=settings.is_enabled,
        version=__version__,
    )


@app.get("/schema")
def schema() -> dict[str, Any]:
    """The semantic layer's tables and certified metrics -- what a client may ask about."""
    layer = load_semantic_layer()
    return {
        "tables": [
            {
                "table_name": table.get("table_name", ""),
                "description": table.get("description", ""),
                "columns": table.get("columns", []),
            }
            for table in layer.tables
        ],
        "metrics": layer.metrics,
        "dimension_values": layer.dimension_values,
    }


def jsonable(payload: dict[str, Any]) -> Any:
    """Coerce the numpy/pandas scalars DuckDB puts in result rows into JSON types.

    ``JSONResponse`` uses stdlib ``json``, which does not know what a
    ``numpy.int64`` is; FastAPI's own encoder does not either. Shares its intent
    with ``cli.main._json_default`` -- keep numbers as numbers rather than
    stringifying them, so consumers do not have to parse them back.
    """
    from fastapi.encoders import jsonable_encoder

    def coerce(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: coerce(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [coerce(v) for v in value]
        if isinstance(value, (str, bool, int, float)) or value is None:
            return value
        item = getattr(value, "item", None)
        if callable(item):
            try:
                return coerce(item())
            except (ValueError, TypeError):  # pragma: no cover - defensive
                pass
        return jsonable_encoder(value)

    return coerce(payload)

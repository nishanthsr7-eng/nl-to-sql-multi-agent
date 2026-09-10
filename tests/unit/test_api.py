"""API contract tests.

The HTTP layer is deliberately thin, so what is worth testing is the contract
rather than the plumbing: the body is the pipeline's own payload, and the status
code carries the same three-way distinction the CLI's exit code does. Both are
things a client builds against and neither is visible from a passing smoke test.

No test here opens DuckDB or reaches a provider: the pipeline the app builds at
startup is replaced by a fake before the lifespan handler runs.
"""

from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from semantic_query_engine.api import app as api_module
from semantic_query_engine.core.results import Clarification, Failure, StructuredResponse


class _FakeConn:
    def __init__(self, tables=("fmcg_sales", "weekly_modeling_data")):
        self._tables = tables

    def execute(self, sql, params=None):
        return self

    def fetchall(self):
        return [(name,) for name in self._tables]

    def close(self):
        pass


class _FakePipeline:
    def __init__(self, result=None):
        self.result = result
        self.conn = _FakeConn()
        self.calls: list[tuple[str, list[str]]] = []
        # Who each request ran as, so a test can assert the body's ``principal``
        # reached the pipeline rather than only being accepted by the schema.
        self.principals: list[str] = []

    def run(self, question, context=None, principal=None):
        self.calls.append((question, list(context or [])))
        self.principals.append(principal.id if principal is not None else None)
        return self.result


def _answer(**overrides):
    payload = dict(
        narrative_summary="PL-South leads on revenue.",
        key_metric="£6.67M Total Revenue",
        comparison_context=None,
        chart_recommendation="bar",
        sql_query="SELECT region FROM fmcg_sales LIMIT 10",
        result_table=[{"region": "PL-South", "total_revenue": 6_666_229.81}],
    )
    payload.update(overrides)
    return StructuredResponse(**payload)


@pytest.fixture
def client(monkeypatch):
    """A TestClient whose app holds a fake pipeline.

    Patching the orchestrator's class (rather than the app) is what keeps the
    lifespan handler -- which constructs the pipeline at startup -- offline.
    """
    from semantic_query_engine.pipeline import orchestrator

    fake = _FakePipeline()
    monkeypatch.setattr(orchestrator, "AnalyticsPipeline", lambda *a, **k: fake)
    with TestClient(api_module.app) as test_client:
        test_client.fake = fake  # type: ignore[attr-defined]
        yield test_client


@pytest.mark.parametrize(
    ("result", "expected_status"),
    [
        (_answer(), 200),
        # Underspecified is not malformed: a 4xx here would tell a client its
        # request was wrong when the guardrail simply wants more scope.
        (Clarification(prompt="Which metric?"), 200),
        (Failure(reason="validation_failed", message="SQL validation failed"), 422),
        (Failure(reason="no_data", message="No data matched"), 422),
        (Failure(reason="execution_failed", message="boom"), 500),
        (Failure(reason="query_timeout", message="too slow"), 504),
    ],
)
def test_query_status_reflects_the_outcome(client, result, expected_status):
    client.fake.result = result
    response = client.post("/query", json={"question": "anything"})
    assert response.status_code == expected_status
    assert response.headers["X-SQE-Result-Kind"] == result.kind


def test_query_body_is_the_pipeline_payload_verbatim(client):
    """Same rule as the CLI: one payload, no second schema to drift from it."""
    result = _answer()
    client.fake.result = result
    response = client.post("/query", json={"question": "total revenue by region"})
    assert response.json() == result.to_dict()


def test_query_keeps_numpy_scalars_as_json_numbers(client):
    """stdlib json (which JSONResponse uses) cannot encode numpy.int64 at all,
    and stringifying it would push the parsing problem onto every client."""
    client.fake.result = _answer(result_table=[{"units": np.int64(42), "revenue": np.float64(1.5)}])
    row = client.post("/query", json={"question": "q"}).json()["result_table"][0]
    assert row == {"units": 42, "revenue": 1.5}


def test_query_forwards_conversation_context(client):
    """The clarification follow-up path is the only reason ``context`` exists;
    dropping it server-side turns a multi-turn client into a single-turn one."""
    client.fake.result = _answer()
    client.post("/query", json={"question": "revenue, last month", "context": ["Show me the numbers"]})
    assert client.fake.calls[-1] == ("revenue, last month", ["Show me the numbers"])


def test_empty_question_is_rejected_before_the_pipeline_runs(client):
    client.fake.result = _answer()
    assert client.post("/query", json={"question": ""}).status_code == 422
    assert client.fake.calls == []


def test_healthz_reports_the_warehouse_and_whether_an_llm_is_configured(client):
    """A health check that only proves the process is up is worth nothing here:
    a keyless deployment silently answers from the deterministic fallback, which
    is a different system to the one being evaluated."""
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert "fmcg_sales" in body["tables"]
    assert body["llm_enabled"] is False  # the unit tier runs offline


def test_healthz_is_degraded_when_the_warehouse_has_no_tables(client):
    client.fake.conn = _FakeConn(tables=())
    body = client.get("/healthz").json()
    assert body["status"] == "degraded"


def test_schema_endpoint_exposes_tables_and_certified_metrics(client):
    body = client.get("/schema").json()
    assert {t["table_name"] for t in body["tables"]} >= {"fmcg_sales"}
    assert body["metrics"], "the semantic layer's certified metrics must be discoverable"


def test_the_request_principal_reaches_the_pipeline(client):
    """Accepting the field is not the feature; passing the identity on is."""
    client.fake.result = _answer()
    client.post("/query", json={"question": "revenue", "principal": "analyst_north"})
    assert client.fake.principals == ["analyst_north"]


def test_no_principal_runs_as_the_unrestricted_steward(client):
    client.fake.result = _answer()
    client.post("/query", json={"question": "revenue"})
    assert client.fake.principals == ["steward"]


def test_an_undeclared_principal_is_refused_without_running_anything(client):
    """403, not 404: the caller asked to act as an identity this deployment does
    not grant, which is a refusal rather than a missing resource. And the
    warehouse must not be touched on the way to finding that out."""
    response = client.post("/query", json={"question": "revenue", "principal": "nobody"})
    assert response.status_code == 403
    assert client.fake.principals == []


def test_principals_endpoint_lists_what_a_caller_may_run_as(client):
    body = client.get("/principals").json()
    assert {entry["id"] for entry in body["principals"]} >= {"steward", "analyst_north"}

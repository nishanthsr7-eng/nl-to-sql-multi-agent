"""Spans, the run id, and structured logs.

What is worth testing here is not that timing works -- it is that the run id
reaches code that never asked for it, that the spans survive a failing stage,
and that OpenTelemetry stays genuinely optional.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor

import pytest

from semantic_query_engine.governance.telemetry import (
    JsonFormatter,
    RunIdFilter,
    SpanRecorder,
    current_run_id,
    run_context,
)

# ---------------------------------------------------------------------------
# The run id
# ---------------------------------------------------------------------------


def test_the_run_id_is_empty_outside_a_run():
    assert current_run_id() == ""


def test_the_run_id_is_restored_afterwards():
    """A leaked context would stamp one run's id on the next one's log lines."""
    with run_context("outer"):
        with run_context("inner"):
            assert current_run_id() == "inner"
        assert current_run_id() == "outer"
    assert current_run_id() == ""


def test_the_run_id_is_restored_even_when_the_run_raises():
    with pytest.raises(ValueError):
        with run_context("boom"):
            raise ValueError("stage failed")
    assert current_run_id() == ""


def test_concurrent_runs_do_not_see_each_other_ids():
    """The API dispatches blocking endpoints to a threadpool, so two requests
    run at once. A module-level "current run" would cross-attribute their logs,
    the same hazard the per-request cursor exists to avoid."""

    def observe(run_id: str) -> str:
        with run_context(run_id):
            return current_run_id()

    with ThreadPoolExecutor(max_workers=4) as pool:
        observed = list(pool.map(observe, [f"run-{i}" for i in range(4)]))
    assert observed == [f"run-{i}" for i in range(4)]


# ---------------------------------------------------------------------------
# Spans
# ---------------------------------------------------------------------------


def test_a_stage_records_its_attributes_including_ones_set_late():
    """The useful attributes -- the row count, the SQL source -- are not known
    when a stage starts, which is why the context manager yields a dict."""
    recorder = SpanRecorder()
    with recorder.stage("execution") as attributes:
        attributes["rows"] = 3
    assert recorder.spans[0].attributes == {"rows": 3}
    assert recorder.spans[0].duration_ms >= 0


def test_a_failing_stage_is_still_recorded_and_names_the_error():
    """Knowing a run failed does not say *which* stage failed, and that is the
    only thing a latency breakdown of a failing run is good for."""
    recorder = SpanRecorder()
    with pytest.raises(RuntimeError):
        with recorder.stage("sql_generation"):
            raise RuntimeError("provider timed out")

    span = recorder.spans[0]
    assert span.name == "sql_generation"
    assert "provider timed out" in span.error


def test_a_repeated_stage_is_summed_in_the_breakdown():
    """The repair loop runs validation more than once, and the question worth
    answering is how long validation took this run, not per attempt."""
    recorder = SpanRecorder()
    for attempt in range(3):
        with recorder.stage("validation", attempt=attempt):
            pass
    assert set(recorder.breakdown()) == {"validation"}
    assert len(recorder.to_list()) == 3


def test_spans_are_per_recorder_not_global():
    """Held on the RunTrace for the same reason token usage is: two concurrent
    API requests must not accumulate into each other."""
    first, second = SpanRecorder(), SpanRecorder()
    with first.stage("planner"):
        pass
    assert second.spans == []


def test_opentelemetry_is_optional(monkeypatch):
    """The package is deliberately not a dependency. A tracing feature that made
    the CLI unusable without a collector would be a downgrade for the common
    case -- one person, one question, one terminal."""
    import semantic_query_engine.governance.telemetry as telemetry

    monkeypatch.setattr(telemetry, "_OTEL", None)
    monkeypatch.setenv("SQE_OTEL", "0")
    recorder = SpanRecorder()
    with recorder.stage("planner"):
        pass
    assert recorder.spans[0].name == "planner"


# ---------------------------------------------------------------------------
# Structured logs
# ---------------------------------------------------------------------------


def _record(message: str = "hello") -> logging.LogRecord:
    return logging.LogRecord(
        name="semantic_query_engine.demo",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def test_json_logs_carry_the_run_id():
    formatter = JsonFormatter()
    with run_context("abc123"):
        payload = json.loads(formatter.format(_record()))
    assert payload["run_id"] == "abc123"
    assert payload["message"] == "hello"
    assert payload["level"] == "INFO"


def test_json_logs_outside_a_run_omit_the_run_id_rather_than_faking_one():
    payload = json.loads(JsonFormatter().format(_record()))
    assert "run_id" not in payload


def test_the_human_format_gets_the_run_id_too():
    """Otherwise the run id -- the thing that ties a log line to an audit record
    -- would be the JSON format's private feature rather than the system's."""
    record = _record()
    with run_context("abc123"):
        assert RunIdFilter().filter(record)
    assert record.run_id == "abc123"


def test_the_human_format_shows_a_placeholder_outside_a_run():
    record = _record()
    RunIdFilter().filter(record)
    assert record.run_id == "-"


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


def test_a_run_records_a_stage_breakdown_and_puts_it_on_the_payload():
    from semantic_query_engine.pipeline.orchestrator import AnalyticsPipeline

    pipeline = AnalyticsPipeline()
    result, state = pipeline.run_traced("What is total revenue by region?")

    assert state.run_id
    assert "planner" in state.stage_latency_ms
    # The payload is the one surface; a breakdown the CLI could see but the JSON
    # could not would be a second code path, which is what to_dict() exists to
    # prevent.
    assert result.to_dict()["stage_latency_ms"] == state.stage_latency_ms


def test_a_failing_run_still_reports_where_the_time_went():
    """"Elapsed 9s" says a run was slow; "generation 7.9s" says where to look."""
    from semantic_query_engine.pipeline.orchestrator import AnalyticsPipeline

    result = AnalyticsPipeline().run("how are things")
    assert result.to_dict()["stage_latency_ms"].get("planner") is not None

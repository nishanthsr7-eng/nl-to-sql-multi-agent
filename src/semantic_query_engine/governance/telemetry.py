"""Per-stage spans, a run id that reaches every log line, and optional OpenTelemetry.

``RunTrace`` was already most of the way here -- it records what each agent did --
but it recorded it as *prose*, one human-readable sentence per stage. Prose
cannot answer "which stage is slow", because the answer to that is a
distribution over a thousand runs and the trace is a story about one.

This module adds the missing half without throwing the prose away, because the
prose is what ``sqe ask --explain`` renders and it is genuinely the better
artefact for one query. Spans sit alongside it: the same stage boundaries,
recorded as ``(name, start, duration)`` instead of as a sentence.

**OpenTelemetry is optional and stays optional.** The package is not a
dependency. If ``opentelemetry-api`` happens to be installed, every span is also
emitted through it and joins whatever the deployment already collects; if it is
not, spans are still recorded on the trace and still logged. A tracing feature
that made a CLI unusable without a collector would be a downgrade for the
overwhelmingly common case -- one person, one question, one terminal -- and the
project's own evaluation harness is exactly that case.

**The run id is a context variable, not an argument.** It has to reach code that
has no business taking a ``run_id`` parameter -- a logger call inside the
validator, three frames below anything that knows what a run is. A contextvar is
also the only version of this that survives the API's threadpool correctly:
each request gets its own context, so two concurrent runs cannot stamp each
other's id on their log lines. The same reasoning as the per-request cursor.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

# The run this thread/task is currently serving, or "" outside any run.
_CURRENT_RUN: ContextVar[str] = ContextVar("sqe_run_id", default="")


def current_run_id() -> str:
    return _CURRENT_RUN.get()


@contextmanager
def run_context(run_id: str) -> Iterator[None]:
    """Stamp ``run_id`` on everything logged inside this block."""
    token = _CURRENT_RUN.set(run_id)
    try:
        yield
    finally:
        _CURRENT_RUN.reset(token)


# ---------------------------------------------------------------------------
# Spans
# ---------------------------------------------------------------------------


@dataclass
class Span:
    """One stage of one run."""

    name: str
    duration_ms: float
    attributes: dict[str, Any] = field(default_factory=dict)
    # Set when the stage raised. Recorded rather than inferred from the run's
    # outcome: knowing the run failed does not say *which* stage failed, and
    # that is the only thing a latency breakdown of a failing run is good for.
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"stage": self.name, "duration_ms": self.duration_ms}
        if self.attributes:
            payload["attributes"] = self.attributes
        if self.error:
            payload["error"] = self.error
        return payload


class _NoopOtel:
    """What tracing costs when OpenTelemetry is not installed: nothing."""

    @contextmanager
    def span(self, name: str, attributes: dict[str, Any]) -> Iterator[None]:
        yield


class _Otel:
    """Mirrors each span into a real OpenTelemetry tracer."""

    def __init__(self, tracer: Any):
        self._tracer = tracer

    @contextmanager
    def span(self, name: str, attributes: dict[str, Any]) -> Iterator[None]:
        with self._tracer.start_as_current_span(name) as otel_span:
            try:
                yield
            finally:
                # Attributes are set on the way *out*, because most of them --
                # the row count, the SQL source, whether a repair happened --
                # are not known when the stage starts.
                for key, value in attributes.items():
                    otel_span.set_attribute(f"sqe.{key}", value)


def _build_otel() -> _NoopOtel | _Otel:
    """An OpenTelemetry bridge if the API is importable, otherwise a no-op.

    Import errors are the expected case, not an exceptional one: the package is
    deliberately not a dependency. Any *other* failure is swallowed too, because
    a misconfigured collector must not stop the engine answering questions --
    telemetry is a thing you observe the system with, never a thing the system
    depends on.
    """
    if (os.getenv("SQE_OTEL") or "").strip().lower() in ("0", "false", "off", "no"):
        return _NoopOtel()
    try:
        from opentelemetry import trace

        return _Otel(trace.get_tracer("semantic_query_engine"))
    except Exception:
        return _NoopOtel()


_OTEL: _NoopOtel | _Otel | None = None


def _otel() -> _NoopOtel | _Otel:
    global _OTEL
    if _OTEL is None:
        _OTEL = _build_otel()
    return _OTEL


class SpanRecorder:
    """Collects the spans of one run.

    Held by the ``RunTrace`` rather than being global, for the same reason token
    usage is: two concurrent API requests must not accumulate into each other.
    """

    def __init__(self) -> None:
        self.spans: list[Span] = []

    @contextmanager
    def stage(self, name: str, **attributes: Any) -> Iterator[dict[str, Any]]:
        """Time one stage. The yielded dict is for attributes known only at the end.

        ``with recorder.stage("execution") as attrs: ... attrs["rows"] = len(df)``
        -- the row count is the useful attribute and it does not exist until the
        stage is nearly over.
        """
        collected = dict(attributes)
        started = time.perf_counter()
        error = ""
        try:
            with _otel().span(name, collected):
                yield collected
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self.spans.append(
                Span(
                    name=name,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                    attributes=collected,
                    error=error,
                )
            )

    def to_list(self) -> list[dict[str, Any]]:
        return [span.to_dict() for span in self.spans]

    def breakdown(self) -> dict[str, float]:
        """Milliseconds per stage, summed -- the latency answer, per run.

        Summed rather than listed because a stage can occur more than once: the
        repair loop runs generation and validation again, and "how long did
        validation take this run" is the question worth answering.
        """
        totals: dict[str, float] = {}
        for span in self.spans:
            totals[span.name] = round(totals.get(span.name, 0.0) + span.duration_ms, 2)
        return totals


# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------


class JsonFormatter(logging.Formatter):
    """One JSON object per log line, carrying the run id.

    The default human formatter stays the default. A developer reading a
    terminal is the common case and JSON is worse for them; a log shipper is
    the case that needs this, and it is selected with ``SQE_LOG_FORMAT=json``.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        run_id = current_run_id()
        if run_id:
            payload["run_id"] = run_id
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class RunIdFilter(logging.Filter):
    """Makes ``%(run_id)s`` available to the human formatter too.

    Without it the two formats would carry different information, and the run id
    -- the thing that ties a log line to an audit record -- would be the JSON
    format's private feature rather than the system's.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = current_run_id() or "-"
        return True


__all__ = [
    "JsonFormatter",
    "RunIdFilter",
    "Span",
    "SpanRecorder",
    "current_run_id",
    "run_context",
]

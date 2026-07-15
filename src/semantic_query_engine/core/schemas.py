"""Typed schemas for LLM JSON output, plus the retry policy for LLM calls.

Every LLM call asks for ``response_format={"type": "json_object"}``, which
guarantees syntactically valid JSON -- not the *right* shape. Parsing through
these Pydantic models means a model that omits ``"sql"`` (or returns the wrong
type) raises a legible ``ValueError`` right where the bad payload was produced,
instead of degrading silently into an empty string three layers away from the
actual cause. Callers already wrap the whole LLM call in ``except Exception``
and fall back to the deterministic path -- this only changes *what* lands in
that except block: a named validation failure instead of a KeyError/TypeError
somewhere downstream.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, ValidationError, field_validator
from tenacity import retry, stop_after_attempt, wait_fixed

_VALID_CHART_TYPES = ("bar", "grouped_bar", "line", "scatter", "pie")

# One retry (two attempts total) with a short fixed backoff. This is enough to
# ride out a transient network blip or a one-off malformed JSON response
# without meaningfully slowing down the request/fallback path -- the caller's
# `except Exception` still catches a persistent failure and degrades to the
# deterministic template bank.
llm_retry = retry(stop=stop_after_attempt(2), wait=wait_fixed(0.5), reraise=True)


class SQLGenerationPayload(BaseModel):
    sql: str = Field(min_length=1)


class SynthesisPayload(BaseModel):
    narrative_summary: str = Field(min_length=1)
    key_metric: str | None = None
    comparison_context: str | None = None
    chart_recommendation: str = "bar"

    @field_validator("chart_recommendation", mode="before")
    @classmethod
    def _coerce_unknown_chart_type(cls, value: str | None) -> str:
        # The narrative and key metric are the load-bearing fields here; an
        # unrecognised chart hint from the model shouldn't fail the whole payload.
        lowered = (value or "bar").lower()
        return lowered if lowered in _VALID_CHART_TYPES else "bar"


def parse_sql_payload(raw_json: str) -> SQLGenerationPayload:
    try:
        return SQLGenerationPayload.model_validate_json(raw_json)
    except ValidationError as exc:
        raise ValueError(f"LLM returned an invalid SQL generation payload: {exc}") from exc


def parse_synthesis_payload(raw_json: str) -> SynthesisPayload:
    try:
        return SynthesisPayload.model_validate_json(raw_json)
    except ValidationError as exc:
        raise ValueError(f"LLM returned an invalid synthesis payload: {exc}") from exc

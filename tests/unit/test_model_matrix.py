"""The model-comparison matrix.

The arithmetic is the least interesting part. What is worth guarding is the
three refusals -- a degraded run is not scored, an unpriced model does not win
on price, and a recommendation is never made on accuracy alone -- because each
of those is a way this kind of table quietly misleads whoever reads it as a
ranking.
"""

from __future__ import annotations

import pytest
from evals.harness import CaseRecord, EvalRun
from evals.model_matrix import (
    EQUIVALENCE_MARGIN,
    MatrixEntry,
    ModelSpec,
    embedding_environment,
    recommend,
    summarise,
)


def _record(case_id: str, *, executed: bool, latency_ms: float, used_llm: bool = True) -> CaseRecord:
    return CaseRecord(
        case_id=case_id,
        archetype="A",
        difficulty="easy",
        sql_features=[],
        expects="rows",
        adversarial=False,
        kind="answer",
        executed=executed,
        values_correct=executed,
        latency_ms=latency_ms,
        # ``used_llm`` is derived from this, not stored: a record cannot claim
        # to have used a model while naming a different source.
        sql_source="llm" if used_llm else "fallback",
    )


def _adversarial(case_id: str) -> CaseRecord:
    """A guardrail case: contained, so it never executed, and never scored."""
    record = _record(case_id, executed=False, latency_ms=10.0)
    record.adversarial = True
    return record


def _run(records: list[CaseRecord], *, provider: str = "openai", model: str = "gpt-4o-mini") -> EvalRun:
    return EvalRun(
        suite="gold",
        baseline="full",
        started_at="2026-09-21T00:00:00+00:00",
        duration_seconds=1.0,
        provider=provider,
        generator_model=model,
        ablation={},
        records=records,
    )


def _entry(label: str, accuracy: float, cost: float | None, **overrides) -> MatrixEntry:
    return MatrixEntry(
        label=label,
        generator_model=label,
        provider="openai",
        execution_accuracy=accuracy,
        cost_per_query_usd=cost,
        **overrides,
    )


# ---------------------------------------------------------------------------
# Summarising a run
# ---------------------------------------------------------------------------


def test_a_row_reports_accuracy_and_latency_percentiles():
    records = [
        _record("a", executed=True, latency_ms=100),
        _record("b", executed=True, latency_ms=200),
        _record("c", executed=False, latency_ms=900),
        _record("d", executed=True, latency_ms=150),
    ]
    entry = summarise(_run(records), ModelSpec(label="mid", generator_model="gpt-4o-mini"))
    assert entry.execution_accuracy == 75.0
    assert entry.cases == 4
    # Nearest-rank, so both percentiles are latencies something actually took.
    assert entry.latency_p50_ms in {150.0, 200.0}
    assert entry.latency_p95_ms == 900.0


def test_a_degraded_run_is_disqualified_not_annotated():
    """A rate-limited provider makes the suite complete in under a second and
    report the template registry's coverage as the model's accuracy. A matrix is
    read as a ranking, and a footnote does not survive being read as one."""
    records = [_record(str(i), executed=True, latency_ms=5, used_llm=False) for i in range(10)]
    entry = summarise(_run(records), ModelSpec(label="mid", generator_model="gpt-4o-mini"))
    assert not entry.comparable
    assert "fell back" in entry.disqualified


def test_a_run_without_a_provider_is_not_called_degraded():
    """A keyless checkout is not a failed measurement, it is no measurement --
    and EvalRun.degraded already draws that line. Re-drawing it differently here
    would make an offline run look like a provider outage."""
    records = [_record(str(i), executed=True, latency_ms=5, used_llm=False) for i in range(10)]
    entry = summarise(
        _run(records, provider="none", model=""), ModelSpec(label="offline", generator_model="")
    )
    assert entry.comparable


def test_an_unpriced_model_reports_tokens_and_declines_to_report_cost():
    """None propagates rather than defaulting to zero: "$0.0000 per query" is
    both false and flattering, which is the worst a measurement can be."""
    unpriced = _record("a", executed=True, latency_ms=10)
    # cost_usd=None is what core/usage.py records for a model absent from
    # PRICING. An empty usage dict would test something else entirely: a run
    # that made no calls at all.
    unpriced.usage = {"prompt_tokens": 100, "completion_tokens": 20, "calls": 1, "cost_usd": None}
    entry = summarise(
        _run([unpriced], model="some-unlisted-model"),
        ModelSpec(label="unknown", generator_model="some-unlisted-model"),
    )
    assert entry.total_tokens == 120
    assert entry.cost_per_query_usd is None


# ---------------------------------------------------------------------------
# The recommendation
# ---------------------------------------------------------------------------


def test_the_cheapest_model_inside_the_margin_wins():
    """"The most accurate model" is one column, not a decision. A 1pp lead that
    costs 17x is a trade somebody should make explicitly."""
    label, reasoning = recommend(
        [
            _entry("frontier", 88.0, 0.017),
            _entry("mid", 87.0, 0.001),
        ]
    )
    assert label == "mid"
    assert "87.0" in reasoning and "frontier" in reasoning


def test_a_model_outside_the_margin_does_not_win_on_price_alone():
    label, _ = recommend(
        [
            _entry("frontier", 88.0, 0.017),
            _entry("cheap", 88.0 - EQUIVALENCE_MARGIN - 5, 0.0001),
        ]
    )
    assert label == "frontier"


def test_an_unpriced_model_cannot_win_on_cost():
    """It is not disqualified -- it may still be the most accurate -- but
    "cheapest" has to mean something, and unpriced is not cheap, it is unmeasured."""
    label, reasoning = recommend(
        [
            _entry("priced", 87.0, 0.001),
            _entry("unpriced", 88.0, None),
        ]
    )
    assert label == "priced"
    assert "0.001" in reasoning or "$0.00" in reasoning


def test_a_disqualified_model_is_never_recommended():
    label, _ = recommend(
        [
            _entry("degraded", 99.0, 0.0, disqualified="fell back to templates"),
            _entry("honest", 70.0, 0.002),
        ]
    )
    assert label == "honest"


def test_no_comparable_run_means_no_recommendation():
    """Not a default, not the best of a bad set: nothing."""
    label, reasoning = recommend([_entry("x", 99.0, 0.0, disqualified="not run")])
    assert label == ""
    assert "nothing to recommend" in reasoning


def test_every_recommendation_carries_its_reasoning():
    """A recommendation without it is an opinion, and the point of the exercise
    is that this one is not."""
    _, reasoning = recommend([_entry("only", 80.0, 0.002)])
    assert len(reasoning) > 40


# ---------------------------------------------------------------------------
# Reaching the models
# ---------------------------------------------------------------------------


def test_an_unreachable_model_is_detected_before_the_suite_runs(monkeypatch):
    """A missing key does not error -- it makes every generation fall back to the
    templates, which is the failure that looks like a result. Cheaper and
    clearer to notice before spending the time."""
    monkeypatch.delenv("SQE_NOT_SET_ANYWHERE", raising=False)
    spec = ModelSpec(label="x", generator_model="m", api_key_env="SQE_NOT_SET_ANYWHERE")
    assert not spec.is_reachable
    monkeypatch.setenv("SQE_NOT_SET_ANYWHERE", "key")
    assert spec.is_reachable


def test_the_synthesizer_defaults_to_the_generator(monkeypatch):
    """Synthesis is not what is being compared; holding it separate would add a
    second variable to an experiment that already has one."""
    monkeypatch.setenv("SQE_LLM_API_KEY", "k")
    env = ModelSpec(label="x", generator_model="gpt-4o").environment()
    assert env["SQE_SYNTHESIZER_MODEL"] == "gpt-4o"


def test_a_per_model_base_url_is_applied(monkeypatch):
    """A matrix worth running spans providers, and they are not all behind one key."""
    monkeypatch.setenv("SQE_ALT_KEY", "k")
    env = ModelSpec(
        label="open",
        generator_model="llama-3.1-8b-instant",
        api_key_env="SQE_ALT_KEY",
        base_url="https://api.groq.com/openai/v1",
    ).environment()
    assert env["SQE_LLM_BASE_URL"] == "https://api.groq.com/openai/v1"
    assert env["SQE_LLM_API_KEY"] == "k"


@pytest.mark.parametrize("spec", [pytest.param(s, id=s.label) for s in __import__(
    "evals.model_matrix", fromlist=["default_models"]
).default_models()])
def test_the_default_model_set_is_well_formed(spec):
    """The set a report was produced from is version-controlled next to the code
    that produced it, so a typo in it is a wrong artefact rather than an error."""
    assert spec.label and spec.generator_model and spec.note


def test_every_row_embeds_through_the_same_pinned_endpoint(monkeypatch):
    """The confound this guards: Groq serves no embeddings model, so a hosted row
    fell back to keyword retrieval while the local row retrieved by vector, and
    the whole resulting gap was attributed to the generator. Regression for the
    2026-09-21 matrix run, which was stopped for exactly this."""
    monkeypatch.setenv("SQE_MATRIX_EMBEDDING_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("SQE_MATRIX_EMBEDDING_MODEL", "nomic-embed-text")
    monkeypatch.setenv("SQE_GROQ_KEY", "k")
    monkeypatch.setenv("SQE_LLM_API_KEY", "k")

    hosted = ModelSpec(
        label="hosted",
        generator_model="openai/gpt-oss-120b",
        api_key_env="SQE_GROQ_KEY",
        base_url="https://api.groq.com/openai/v1",
    ).environment()
    local = ModelSpec(
        label="local", generator_model="sqe-coder", base_url="http://localhost:11434/v1"
    ).environment()

    for env in (hosted, local):
        assert env["SQE_EMBEDDING_BASE_URL"] == "http://localhost:11434/v1"
        assert env["SQE_EMBEDDING_MODEL"] == "nomic-embed-text"
    # The generator is what varies -- and the only thing that varies.
    assert hosted["SQE_LLM_BASE_URL"] != local["SQE_LLM_BASE_URL"]


def test_an_unpinned_run_reports_no_embedding_overrides(monkeypatch):
    """Absent the pin, rows inherit the ambient config rather than being handed
    an empty string, which would switch vector retrieval off mid-matrix."""
    for name in ("SQE_MATRIX_EMBEDDING_BASE_URL", "SQE_MATRIX_EMBEDDING_MODEL",
                 "SQE_MATRIX_EMBEDDING_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert embedding_environment() == {}


def test_a_cost_tie_is_broken_by_accuracy_not_list_order():
    """Every local endpoint is priced at exactly zero, so an all-local matrix has
    every row tied on cost. Before this, `min` broke that tie by list order and
    could recommend a model a full equivalence margin worse than the best, with
    nothing in the report explaining why. Regression for 2026-09-21."""
    entries = [
        MatrixEntry(
            label="worse-but-first",
            generator_model="a",
            provider="openai",
            execution_accuracy=60.0,
            cost_per_query_usd=0.0,
        ),
        MatrixEntry(
            label="best",
            generator_model="b",
            provider="openai",
            execution_accuracy=61.5,
            cost_per_query_usd=0.0,
        ),
    ]
    # Both are inside the equivalence margin of each other, so the tie is real.
    assert entries[1].execution_accuracy - entries[0].execution_accuracy < EQUIVALENCE_MARGIN

    label, reasoning = recommend(entries)
    assert label == "best"
    assert "61.5" in reasoning


def test_accuracy_excludes_adversarial_cases_from_its_denominator():
    """A matrix row labelled "execution accuracy" has to mean what the ladder's
    identically-labelled number means. Dividing by every record put the guardrail
    cases in the denominator and made the matrix quietly incomparable to every
    other accuracy in the repo -- the same denominator drift the README carried
    until 2026-09-21. Regression for that."""
    records = [
        _record("scored-1", executed=True, latency_ms=10.0),
        _record("scored-2", executed=True, latency_ms=10.0),
        _adversarial("attack-1"),
        _adversarial("attack-2"),
    ]

    entry = summarise(_run(records), ModelSpec(label="m", generator_model="m"))

    # 2 of 2 scored cases executed, not 2 of 4 records.
    assert entry.cases == 2
    assert entry.execution_accuracy == 100.0

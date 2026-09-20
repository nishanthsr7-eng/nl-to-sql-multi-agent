"""The variance scorer, offline.

Each test names the claim `sqe bench` would otherwise be able to make falsely.
The one that matters most is the cancellation test: a report that only knows how
to compute spread will call a wildly unstable system stable, because the wins and
losses net out in the aggregate.
"""

from __future__ import annotations

import pytest
from evals.harness import CaseRecord, EvalRun
from evals.variance import VarianceError, render_variance, variance


def _record(case_id: str, *, correct: bool, sql: str = "SELECT 1", **overrides: object) -> CaseRecord:
    defaults: dict[str, object] = {
        "case_id": case_id,
        "archetype": "descriptive_lookup",
        "difficulty": "easy",
        "sql_features": ["aggregate"],
        "expects": "answer",
        "adversarial": False,
        "kind": "answer",
        "executed": True,
        "values_correct": correct,
        "sql": sql,
        "sql_source": "llm",
        "latency_ms": 1000.0,
    }
    return CaseRecord(**{**defaults, **overrides})  # type: ignore[arg-type]


def _run(records: list[CaseRecord], **overrides: object) -> EvalRun:
    defaults: dict[str, object] = {
        "suite": "gold",
        "baseline": "full",
        "started_at": "2026-09-20T00:00:00+00:00",
        "duration_seconds": 1.0,
        "provider": "openai",
        "generator_model": "sqe-coder",
        "ablation": {},
    }
    return EvalRun(records=records, **{**defaults, **overrides})  # type: ignore[arg-type]


def test_flip_rate_catches_instability_that_spread_hides():
    """The reason this module exists.

    Two runs, identical accuracy (2/4 each), zero spread -- and every single case
    changed verdict between them. Reporting only the spread would publish "stable
    to 0.0pp" about a system where no case is reproducible.
    """
    first = _run([_record("a", correct=True), _record("b", correct=True),
                  _record("c", correct=False), _record("d", correct=False)])
    second = _run([_record("a", correct=False), _record("b", correct=False),
                   _record("c", correct=True), _record("d", correct=True)])

    report = variance([first, second])

    assert report.value_accuracy_spread == 0.0
    assert report.flip_rate == 1.0
    assert report.reproducible_floor == 0.0


def test_reproducible_floor_excludes_cases_that_were_only_sometimes_right():
    """A case right two runs in three is not something a user can rely on.

    Mean accuracy counts it at two thirds; the floor counts it at zero, which is
    the number to quote when someone asks what the system reliably does.
    """
    runs = [
        _run([_record("stable", correct=True), _record("flaky", correct=True)]),
        _run([_record("stable", correct=True), _record("flaky", correct=False)]),
        _run([_record("stable", correct=True), _record("flaky", correct=True)]),
    ]

    report = variance(runs)

    assert report.mean_value_accuracy == pytest.approx(5 / 6)
    assert report.reproducible_floor == 0.5
    assert report.stable_correct == 1
    assert report.stable_wrong == 0


def test_a_flip_with_identical_sql_is_reported_as_a_measurement_defect():
    """Same query, different verdict: the decode is not the cause.

    Separating this from a churned flip is what stops "the model is unstable"
    being written about a tolerance or a row-ordering bug in the scorer.
    """
    runs = [
        _run([_record("x", correct=True, sql="SELECT  region\n   FROM t")]),
        _run([_record("x", correct=False, sql="SELECT region FROM t")]),
    ]

    report = variance(runs)

    # Whitespace-only differences are not churn, so this counts as silent.
    assert report.sql_churn_rate == 0.0
    assert [case.case_id for case in report.silent_flips] == ["x"]


def test_changed_sql_with_a_stable_verdict_is_churn_but_not_a_flip():
    """The benign majority. Different query, same answer -- worth reporting as
    churn, and specifically not as instability in the result."""
    runs = [
        _run([_record("x", correct=True, sql="SELECT SUM(a) FROM t")]),
        _run([_record("x", correct=True, sql="SELECT SUM(t.a) FROM t")]),
    ]

    report = variance(runs)

    assert report.sql_churn_rate == 1.0
    assert report.flip_rate == 0.0
    assert report.silent_flips == []


def test_token_churn_catches_variation_that_sql_churn_misses():
    """The finding from the first real 3x run, as a regression test.

    ``sql_churned`` diffs one agent's output near the end of a five-agent
    pipeline. A run where the planner or synthesis emitted something different
    but the generator still converged on the same query looks perfectly
    deterministic to it. On the 2026-09-20 run that was 102 of 128 cases against
    a single churned query -- reporting only the SQL number would have supported
    a determinism claim the run does not support.
    """
    runs = [
        _run([_record("x", correct=True, sql="SELECT 1", usage={"total_tokens": 4000})]),
        _run([_record("x", correct=True, sql="SELECT 1", usage={"total_tokens": 4120})]),
    ]

    report = variance(runs)

    assert report.sql_churn_rate == 0.0
    assert report.token_churn_rate == 1.0
    assert "SQL is stable; the pipeline is not" in render_variance(report)


def test_adversarial_cases_are_excluded_from_the_denominator():
    """Same rule as the accuracy report: a refusal suite in the denominator
    would move the flip rate according to how many probes the suite contains."""
    runs = [
        _run([_record("a", correct=True), _record("probe", correct=False, adversarial=True)]),
        _run([_record("a", correct=True), _record("probe", correct=True, adversarial=True)]),
    ]

    report = variance(runs)

    assert report.scored_cases == 1
    assert report.flip_rate == 0.0


def test_only_cases_present_in_every_run_are_compared():
    """A suite that grew between repeats narrows the comparison rather than
    misaligning it -- comparing case 5 of one run against case 5 of another is
    how a variance report silently becomes fiction."""
    runs = [
        _run([_record("a", correct=True), _record("b", correct=True)]),
        _run([_record("a", correct=False)]),
    ]

    report = variance(runs)

    assert [case.case_id for case in report.cases] == ["a"]


def test_runs_from_different_rungs_are_refused():
    """Comparing naive against full would report the ladder's effect as noise,
    which is the one conclusion this module must never license."""
    runs = [_run([_record("a", correct=True)]), _run([_record("a", correct=False)], baseline="naive")]

    with pytest.raises(VarianceError, match="more than one configuration"):
        variance(runs)


def test_a_degraded_run_cannot_enter_a_variance_report():
    """The deterministic fallback answers identically every time. Including a
    degraded run would report perfect stability that the model did not earn."""
    healthy = _run([_record("a", correct=True)])
    degraded = _run([_record("a", correct=True, sql_source="template")])
    assert degraded.degraded  # guards the fixture, not the code under test

    with pytest.raises(VarianceError, match="did not measure a model"):
        variance([healthy, degraded])


def test_a_run_with_no_provider_cannot_report_stability():
    """``EvalRun.degraded`` exempts ``provider == "none"`` -- correctly, for an
    offline smoke test. Here that exemption would publish a 0% flip rate for a
    configuration in which no model ran, which is the most flattering possible
    variance report and an entirely false one."""
    runs = [
        _run([_record("a", correct=True, sql_source="template")], provider="none"),
        _run([_record("a", correct=True, sql_source="template")], provider="none"),
    ]
    assert not runs[0].degraded  # the exemption this test exists to close

    with pytest.raises(VarianceError, match="did not measure a model"):
        variance(runs)


def test_a_single_run_is_refused():
    with pytest.raises(VarianceError, match="at least two"):
        variance([_run([_record("a", correct=True)])])


def test_render_names_cancellation_when_the_flip_rate_beats_the_spread():
    """The interpretation is printed rather than left to the reader, because
    quoting the spread and ignoring the flip rate is the failure mode."""
    runs = [
        _run([_record("a", correct=True), _record("b", correct=False)]),
        _run([_record("a", correct=False), _record("b", correct=True)]),
    ]

    rendered = render_variance(variance(runs))

    assert "2 cases flipped" in rendered
    assert "Least stable cases" in rendered

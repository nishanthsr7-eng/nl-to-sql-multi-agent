"""The judge tier, offline.

No network: the judge's client is faked, which is the point of injecting a client
factory. What is tested here is everything around the model -- the rubric's
arithmetic, what happens when the judge replies with nonsense, and the calibration
that decides whether any of it counts as a measurement.
"""

from __future__ import annotations

from typing import Any

from evals.grounding import check_grounding
from evals.judge import (
    JudgeReport,
    NarrativeVerdict,
    Rubric,
    build_user_prompt,
    calibration,
    judge_narrative,
    render_judge,
)

from semantic_query_engine.core.config import LLMSettings

ROWS = [{"region": "PL-South", "total_revenue": 2861447.4}]


class _FakeClient:
    """A judge that replies with whatever content it was constructed with."""

    def __init__(self, content: str | Exception):
        self._content = content
        self.chat = self
        self.completions = self

    def create(self, **kwargs: Any) -> Any:
        if isinstance(self._content, Exception):
            raise self._content
        message = type("M", (), {"content": self._content})
        choice = type("C", (), {"message": message})
        return type("R", (), {"choices": [choice], "usage": None})


def _settings(provider: str = "openai", **overrides: Any) -> LLMSettings:
    fields: dict[str, Any] = {
        "provider": provider,
        "api_key": "sk-test",
        "base_url": "",
        "generator_model": "gen",
        "synthesizer_model": "synth",
        "embedding_model": "embed",
    }
    return LLMSettings(**{**fields, **overrides})  # type: ignore[arg-type]


def _verdict(case_id: str, scores: tuple[int, int, int], narrative: str = "Revenue varied.") -> NarrativeVerdict:
    return NarrativeVerdict(
        case_id=case_id,
        question="q",
        narrative=narrative,
        grounding=check_grounding(narrative, ROWS),
        rubric=Rubric(*scores),
    )


# ---------------------------------------------------------------------------
# The rubric
# ---------------------------------------------------------------------------

def test_a_narrative_passes_only_on_full_marks():
    """Partial credit in a pass rate would report a system as fine when a third
    of its prose overstates what the data shows."""
    assert Rubric(2, 2, 2).passes
    assert not Rubric(2, 2, 1).passes


def test_an_unreadable_score_counts_against_the_narrative():
    """A judge replying "2/2" or 7 must neither crash the suite nor contribute an
    out-of-range value to a mean. Unreadable resolves to 0 -- the conservative
    direction, since a lenient default is how a judge flatters its own output."""
    assert Rubric.from_dict({"relevance": "2/2"}).relevance == 2
    assert Rubric.from_dict({"relevance": 7}).relevance == 2
    assert Rubric.from_dict({"relevance": "nonsense"}).relevance == 0
    assert Rubric.from_dict({}).total == 0


def test_grounding_and_the_rubric_are_both_required_to_pass():
    """The reason the two halves are one verdict: a narrative that invents a
    number still reads as perfectly faithful to a judge that was told not to
    check arithmetic."""
    invented = NarrativeVerdict(
        case_id="x",
        question="q",
        narrative="Revenue was £9.9M.",
        grounding=check_grounding("Revenue was £9.9M.", ROWS),
        rubric=Rubric(2, 2, 2),
    )

    assert invented.rubric.passes
    assert not invented.grounding.grounded
    assert not invented.passes


# ---------------------------------------------------------------------------
# Calling the judge
# ---------------------------------------------------------------------------

def test_an_unreachable_judge_is_excluded_rather_than_scored_zero():
    """A provider outage is not a narrative defect. Scoring it 0 would let an
    outage look like a quality regression, which is the same distinction
    ``EvalRun.degraded`` draws for the accuracy suite."""
    verdict = judge_narrative(
        "x", "q", "Revenue varied.", ROWS,
        settings=_settings(),
        client_factory=lambda _: _FakeClient(RuntimeError("connection refused")),
    )

    assert not verdict.judged
    assert "connection refused" in verdict.error
    # The free half still ran.
    assert verdict.grounding.grounded


def test_grounding_runs_with_no_provider_at_all():
    """The arithmetic half is a complete measurement on its own, and recording it
    without a model is the reason it was split out in the first place."""
    verdict = judge_narrative("x", "q", "Revenue was £9.9M.", ROWS, settings=_settings("none"))

    assert not verdict.judged
    assert not verdict.grounding.grounded


def test_a_judge_reply_is_parsed_into_a_rubric():
    verdict = judge_narrative(
        "x", "q", "Revenue varied.", ROWS,
        settings=_settings(),
        client_factory=lambda _: _FakeClient('{"relevance": 2, "faithfulness": 1, "calibration": 0}'),
    )

    assert verdict.judged
    assert verdict.rubric.scores == {"relevance": 2, "faithfulness": 1, "calibration": 0}
    assert not verdict.passes


def test_the_prompt_discloses_that_rows_were_truncated():
    """A judge shown 15 of 400 rows and not told so marks a claim about the tail
    unfaithful -- scoring its own blindfold rather than the narrative."""
    prompt = build_user_prompt("q", "n", [{"a": i} for i in range(400)], sample=15)

    assert "first 15 of 400 rows" in prompt
    assert prompt.count('"a"') == 15


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

def test_no_labels_means_no_agreement_and_an_uncalibrated_report():
    """An unchecked judge produces scores, not a measurement, and the report has
    to say which one it is holding."""
    report = JudgeReport("m", "m", [_verdict("a", (2, 2, 2))])
    report.agreement = calibration(report.verdicts, {})

    assert report.agreement is None
    assert not report.calibrated
    assert "Uncalibrated" in render_judge(report)


def test_agreement_separates_the_lenient_direction_from_the_strict_one():
    """A judge passing what a human failed invalidates a quality claim; the
    reverse merely makes it conservative. Averaging them into one number would
    hide the difference."""
    verdicts = [_verdict("lenient", (2, 2, 2)), _verdict("strict", (1, 2, 2))]
    labels = {
        "lenient": Rubric(1, 2, 2),   # human failed it, judge passed it
        "strict": Rubric(2, 2, 2),    # human passed it, judge failed it
    }

    agreement = calibration(verdicts, labels)

    assert agreement.judge_lenient == ("lenient",)
    assert agreement.judge_strict == ("strict",)
    assert agreement.pass_agreement == 0.0


def test_within_one_agreement_is_reported_next_to_exact():
    """A three-level rubric leaves plenty of room for a defensible one-point
    disagreement, and an exact rate alone makes a usable judge look broken."""
    verdicts = [_verdict("a", (2, 2, 2))]
    agreement = calibration(verdicts, {"a": Rubric(1, 2, 2)})

    assert agreement.exact["relevance"] == 0.0
    assert agreement.within_one["relevance"] == 1.0


def test_a_self_judged_report_says_so():
    """A model grading its own output is the oldest failure in this corner of
    evaluation. On a single-model setup it may be unavoidable -- so it is
    disclosed rather than silently skipped."""
    report = JudgeReport("sqe-coder", "sqe-coder", [_verdict("a", (2, 2, 2))])

    assert report.self_judged
    assert "Self-judged" in render_judge(report)


def test_the_grounding_rate_counts_narratives_the_judge_could_not_reach():
    """Grounding needs no provider, so a narrative the judge missed still has an
    honest answer on that axis. Dropping it would throw away the free half."""
    reachable = _verdict("a", (2, 2, 2))
    unreachable = NarrativeVerdict(
        case_id="b",
        question="q",
        narrative="Revenue was £9.9M.",
        grounding=check_grounding("Revenue was £9.9M.", ROWS),
        error="provider down",
    )
    report = JudgeReport("m", "n", [reachable, unreachable])

    assert len(report.judged) == 1
    assert report.grounding_rate == 0.5

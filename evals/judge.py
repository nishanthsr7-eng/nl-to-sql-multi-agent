"""LLM-as-judge for the narrative, with the arithmetic taken away from it.

The synthesis narrative is the only part of an answer that reaches a user as
prose, and until now it was the only part this project never measured. Value
accuracy scores the *rows*; a narrative can describe a correct result set
incorrectly and nothing would notice.

**What the judge is and is not asked.** The one question everybody wants a judge
for -- did it invent a number -- is not asked here at all. It is decidable, so
:mod:`evals.grounding` decides it, offline and for free. Handing that to a model
would put a second hallucinator in exactly the seat where the first one's
hallucinations are being counted. The judge gets only what needs judgement:

* **relevance** -- does it answer the question that was asked, or a neighbouring one
* **faithfulness** -- do its qualitative claims follow from the rows (no invented
  entities, no trend that is not in the data, no comparison the rows cannot support)
* **calibration** -- does it state causes, forecasts or significance the result set
  cannot establish

Each is scored 0 (fails), 1 (partial) or 2 (passes), because a boolean cannot
tell "slightly overstated" from "fabricated a cause" and those warrant different
responses.

**A judge nobody has checked is not a measurement.** That is the same argument
this project makes about the pipeline, and it applies with more force to the
thing doing the scoring. So the judge ships with a calibration harness: a human
labels a sample against the same rubric, and :func:`calibration` reports how often
the judge agrees. Until labels exist, agreement is reported as *uncalibrated* and
the scores are explicitly not claimed as a measurement -- ``JudgeReport.calibrated``
is False and the renderer says so.

**The judge is never the same model as the one being judged**, when that can be
helped, and the artefact always records which model judged -- a model grading its
own output is the oldest failure in this corner of evaluation.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from evals.grounding import GroundingReport, check_grounding
from semantic_query_engine.core.config import EVALS_DIR, LLMSettings, load_llm_settings
from semantic_query_engine.core.llm_client import ChatClient, build_client
from semantic_query_engine.core.usage import NO_USAGE, TokenUsage, usage_from_response

LABELS_PATH = EVALS_DIR / "datasets" / "judge_labels.json"

# The rubric axes, in the order they are reported. Names are a stable key: they
# are the group-by for the judge report exactly as ``IssueCode`` is for the
# validator funnel, so treat them as append-only for the same reason.
AXES: tuple[str, ...] = ("relevance", "faithfulness", "calibration")

MAX_SCORE = 2

SYSTEM_PROMPT = """\
You are grading one analytics narrative against the result set it claims to \
describe. You are not grading the SQL, the question, or the analysis -- only \
whether this prose is an honest reading of these rows.

Do NOT verify arithmetic. Whether a figure appears in the rows is checked \
separately and is not your job. Assume every number is correct and grade what \
the narrative asserts around them.

Score each axis 0, 1 or 2:

relevance
  2 - answers the question that was asked
  1 - answers a related but different question, or answers only part of it
  0 - does not address the question

faithfulness
  2 - every qualitative claim follows from the rows
  1 - a claim overstates or blurs what the rows show
  0 - invents an entity, a trend, or a comparison the rows cannot support

calibration
  2 - claims nothing the result set cannot establish
  1 - hedged causal or predictive language ("may suggest", "likely driven by")
  0 - asserts a cause, a forecast, or statistical significance outright

Reply with JSON only:
{"relevance": <0-2>, "faithfulness": <0-2>, "calibration": <0-2>, "reason": "<one sentence>"}\
"""


def build_user_prompt(
    question: str, narrative: str, rows: Sequence[dict[str, Any]], *, sample: int = 15
) -> str:
    """The judge's prompt. Rows are truncated, and the truncation is disclosed.

    A judge shown 15 of 400 rows and not told so will mark a narrative's claim
    about the tail unfaithful because it cannot see the tail -- scoring its own
    blindfold rather than the narrative.
    """
    shown = list(rows[:sample])
    note = "" if len(rows) <= sample else (
        f"\n(Showing the first {sample} of {len(rows)} rows. A claim about rows you "
        f"cannot see is not evidence of unfaithfulness -- judge only what is visible.)"
    )
    return (
        f"QUESTION\n{question}\n\n"
        f"RESULT SET ({len(rows)} rows)\n"
        f"{json.dumps(shown, indent=2, default=str)}{note}\n\n"
        f"NARRATIVE\n{narrative}\n"
    )


@dataclass(frozen=True)
class Rubric:
    """One narrative's scores. Used for both judge verdicts and human labels."""

    relevance: int
    faithfulness: int
    calibration: int
    reason: str = ""

    @property
    def scores(self) -> dict[str, int]:
        return {axis: getattr(self, axis) for axis in AXES}

    @property
    def total(self) -> int:
        return sum(self.scores.values())

    @property
    def passes(self) -> bool:
        """A narrative passes only by scoring full marks on every axis.

        Deliberately strict. A 1 means the judge saw something wrong with it, and
        a pass rate that counts partial credit reports a system as fine when a
        third of its prose overstates what the data shows.
        """
        return all(score == MAX_SCORE for score in self.scores.values())

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Rubric:
        return cls(
            relevance=_clamp(raw.get("relevance")),
            faithfulness=_clamp(raw.get("faithfulness")),
            calibration=_clamp(raw.get("calibration")),
            reason=str(raw.get("reason", ""))[:300],
        )


def _clamp(value: Any) -> int:
    """Coerce a judge's score into the rubric's range.

    A judge that replies ``"2/2"``, ``2.0`` or ``5`` should not crash the suite or
    silently contribute an out-of-range score to a mean. Anything unreadable
    becomes 0, which is the conservative direction: an unparseable verdict counts
    against the narrative rather than for it.
    """
    try:
        number = int(float(str(value).split("/")[0].strip()))
    except (TypeError, ValueError):
        return 0
    return max(0, min(MAX_SCORE, number))


@dataclass
class NarrativeVerdict:
    """One narrative, judged on meaning and checked on arithmetic."""

    case_id: str
    question: str
    narrative: str
    grounding: GroundingReport
    rubric: Rubric | None = None
    # Set when the judge could not be reached or its reply was unusable. Such a
    # verdict is excluded from the judged denominator rather than scored 0 --
    # a provider outage is not a narrative defect, which is the same distinction
    # ``EvalRun.degraded`` draws for the accuracy suite.
    error: str = ""
    usage: dict[str, Any] = field(default_factory=dict)

    @property
    def judged(self) -> bool:
        return self.rubric is not None and not self.error

    @property
    def passes(self) -> bool:
        """Full marks from the judge *and* every figure found in the rows.

        Both halves are required, and this is the reason the two are built as one
        verdict: a narrative that invents a number can still read as a perfectly
        faithful summary to a judge that was told not to check arithmetic.
        """
        return bool(self.rubric and self.rubric.passes and self.grounding.grounded)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "question": self.question,
            "narrative": self.narrative,
            "grounding": self.grounding.to_dict(),
            "rubric": asdict(self.rubric) if self.rubric else None,
            "passes": self.passes,
            "error": self.error,
            "usage": self.usage,
        }


@dataclass
class JudgeReport:
    """Every judged narrative from one run, plus the judge's own credentials."""

    judge_model: str
    generator_model: str
    verdicts: list[NarrativeVerdict]
    # Agreement against the human-labelled set, or None when no labels exist.
    agreement: Agreement | None = None

    @property
    def judged(self) -> list[NarrativeVerdict]:
        return [verdict for verdict in self.verdicts if verdict.judged]

    @property
    def calibrated(self) -> bool:
        """Whether a human has checked the judge on a sample of these axes.

        Gates the language of the report, not the numbers. An uncalibrated judge
        still produces scores; what it does not produce is a measurement, and the
        renderer says so rather than letting a reader assume otherwise.
        """
        return self.agreement is not None and self.agreement.n > 0

    @property
    def self_judged(self) -> bool:
        """True when the judge and the generator are the same model.

        Recorded and surfaced rather than prevented: on a single-model local setup
        there may be nothing else to judge with, and the honest response is to run
        it and disclose it, not to quietly skip the tier.
        """
        return bool(self.judge_model) and self.judge_model == self.generator_model

    @property
    def pass_rate(self) -> float:
        judged = self.judged
        return sum(verdict.passes for verdict in judged) / len(judged) if judged else 0.0

    @property
    def grounding_rate(self) -> float:
        """Share of narratives whose every figure was found in the rows.

        Computed over *all* verdicts, not just judged ones: grounding needs no
        provider, so a narrative the judge could not reach still has an honest
        answer on this axis and excluding it would throw away the free half of
        the measurement.
        """
        if not self.verdicts:
            return 0.0
        return sum(verdict.grounding.grounded for verdict in self.verdicts) / len(self.verdicts)

    def mean(self, axis: str) -> float:
        judged = self.judged
        if not judged:
            return 0.0
        return sum(getattr(verdict.rubric, axis) for verdict in judged) / len(judged)

    def to_dict(self) -> dict[str, Any]:
        return {
            "judge_model": self.judge_model,
            "generator_model": self.generator_model,
            "self_judged": self.self_judged,
            "calibrated": self.calibrated,
            "pass_rate": round(self.pass_rate, 4),
            "grounding_rate": round(self.grounding_rate, 4),
            "means": {axis: round(self.mean(axis), 3) for axis in AXES},
            "agreement": self.agreement.to_dict() if self.agreement else None,
            "verdicts": [verdict.to_dict() for verdict in self.verdicts],
        }


# ---------------------------------------------------------------------------
# Judging
# ---------------------------------------------------------------------------

def judge_narrative(
    case_id: str,
    question: str,
    narrative: str,
    rows: Sequence[dict[str, Any]],
    *,
    settings: LLMSettings | None = None,
    client_factory: Callable[[LLMSettings], ChatClient] = build_client,
) -> NarrativeVerdict:
    """Score one narrative. Grounding always runs; the judge runs if it can."""
    resolved = settings or load_llm_settings()
    verdict = NarrativeVerdict(
        case_id=case_id,
        question=question,
        narrative=narrative,
        grounding=check_grounding(narrative, list(rows)),
    )

    if resolved.provider == "none":
        # Not an error: the grounding half is a complete, honest measurement on
        # its own and is worth recording without a provider. Only the rubric is
        # missing, and `judged` already says so.
        verdict.error = "no provider configured; grounding only"
        return verdict

    try:
        client = client_factory(resolved)
        response = client.chat.completions.create(
            model=judge_model_for(resolved),
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(question, narrative, rows)},
            ],
        )
        payload = json.loads(response.choices[0].message.content or "{}")
        verdict.rubric = Rubric.from_dict(payload)
        verdict.usage = usage_from_response(response, judge_model_for(resolved)).to_dict()
    except Exception as exc:
        # Recorded, not raised. One unreachable judge call must not cost a suite
        # that has already run, and an excluded verdict is more honest than a
        # zero that would be read as a narrative defect.
        verdict.error = f"{type(exc).__name__}: {exc}"
    return verdict


def judge_model_for(settings: LLMSettings) -> str:
    """Which model grades. Falls back to the synthesiser when nothing else exists."""
    return getattr(settings, "judge_model", "") or settings.synthesizer_model


def total_usage(report: JudgeReport) -> TokenUsage:
    total = NO_USAGE
    for verdict in report.verdicts:
        if verdict.usage:
            total += TokenUsage.from_dict(verdict.usage)
    return total


# ---------------------------------------------------------------------------
# Calibration against human labels
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Agreement:
    """How often the judge matched a human on the same narratives."""

    n: int
    # Exact-match rate per axis: judge score == human score.
    exact: dict[str, float]
    # Within-one rate per axis. Reported next to exact because a rubric with
    # three levels has a lot of room for a defensible one-point disagreement,
    # and an exact rate alone makes a usable judge look broken.
    within_one: dict[str, float]
    # Agreement on the thing the report actually claims: did both call it a pass.
    pass_agreement: float
    # Cases where the judge passed a narrative the human failed. The dangerous
    # direction, and the one to read first: a judge that is generous where a
    # human is not cannot be used to claim narrative quality.
    judge_lenient: tuple[str, ...] = ()
    judge_strict: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "exact": {axis: round(value, 3) for axis, value in self.exact.items()},
            "within_one": {axis: round(value, 3) for axis, value in self.within_one.items()},
            "pass_agreement": round(self.pass_agreement, 3),
            "judge_lenient": list(self.judge_lenient),
            "judge_strict": list(self.judge_strict),
        }


def load_labels(path: Path = LABELS_PATH) -> dict[str, Rubric]:
    """Human labels by case id. An absent or empty file is not an error."""
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    entries = raw.get("labels", raw) if isinstance(raw, dict) else raw
    return {entry["case_id"]: Rubric.from_dict(entry) for entry in entries}


def calibration(
    verdicts: Sequence[NarrativeVerdict], labels: dict[str, Rubric]
) -> Agreement | None:
    """Compare the judge against human labels on the cases both have scored."""
    paired = [
        (verdict, labels[verdict.case_id])
        for verdict in verdicts
        if verdict.judged and verdict.case_id in labels
    ]
    if not paired:
        return None

    exact: dict[str, float] = {}
    within_one: dict[str, float] = {}
    for axis in AXES:
        deltas = [
            abs(getattr(verdict.rubric, axis) - getattr(label, axis))
            for verdict, label in paired
        ]
        exact[axis] = sum(delta == 0 for delta in deltas) / len(deltas)
        within_one[axis] = sum(delta <= 1 for delta in deltas) / len(deltas)

    lenient = tuple(
        verdict.case_id
        for verdict, label in paired
        if verdict.rubric and verdict.rubric.passes and not label.passes
    )
    strict = tuple(
        verdict.case_id
        for verdict, label in paired
        if verdict.rubric and not verdict.rubric.passes and label.passes
    )
    agreed = sum(
        1 for verdict, label in paired if bool(verdict.rubric and verdict.rubric.passes) == label.passes
    )

    return Agreement(
        n=len(paired),
        exact=exact,
        within_one=within_one,
        pass_agreement=agreed / len(paired),
        judge_lenient=lenient,
        judge_strict=strict,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_judge(report: JudgeReport, *, show_cases: int = 10) -> str:
    """Markdown. Leads with the caveats, because they change how to read it."""
    from evals.report import markdown_table, pct

    lines = ["## Narrative quality", ""]

    if not report.calibrated:
        lines += [
            "> **Uncalibrated.** No human labels exist for these axes yet, so the judge's "
            "scores below are the judge's opinion and not a measurement of narrative "
            "quality. Label a sample with `sqe judge --label` and re-run to report "
            "agreement. The grounding rate is unaffected -- it is arithmetic, not "
            "judgement.",
            "",
        ]
    if report.self_judged:
        lines += [
            f"> **Self-judged.** `{report.judge_model}` graded its own narratives. Read "
            "the rubric scores as generous; the grounding rate is independent of this.",
            "",
        ]

    judged = len(report.judged)
    unreachable = len(report.verdicts) - judged
    lines += [
        markdown_table(
            ["Measure", "Value", "Basis"],
            [
                [
                    "Grounding rate",
                    pct(report.grounding_rate),
                    f"{len(report.verdicts)} narratives, deterministic -- no model involved",
                ],
                [
                    "Pass rate",
                    pct(report.pass_rate) if judged else "n/a",
                    f"{judged} judged, full marks on every axis AND grounded",
                ],
                *[
                    [
                        f"Mean {axis}",
                        f"{report.mean(axis):.2f} / {MAX_SCORE}" if judged else "n/a",
                        "judge",
                    ]
                    for axis in AXES
                ],
            ],
        ),
        "",
    ]

    if unreachable:
        lines += [
            f"{unreachable} narrative(s) could not be judged and are excluded from the "
            "judged denominator -- a provider that could not be reached is not a "
            "narrative defect. They still carry a grounding verdict.",
            "",
        ]

    if report.agreement:
        agreement = report.agreement
        lines += [
            f"### Judge vs human ({agreement.n} labelled)",
            "",
            markdown_table(
                ["Axis", "Exact", "Within 1"],
                [
                    [axis, pct(agreement.exact[axis]), pct(agreement.within_one[axis])]
                    for axis in AXES
                ],
            ),
            "",
            f"Agreement on pass/fail: **{pct(agreement.pass_agreement)}**.",
            "",
        ]
        if agreement.judge_lenient:
            # Read this before the pass rate. A judge that passes what a human
            # fails cannot be used to claim narrative quality, whatever its
            # per-axis agreement looks like.
            lines += [
                f"**{len(agreement.judge_lenient)} narrative(s) the judge passed and the "
                f"human failed**: {', '.join(agreement.judge_lenient)}. This is the "
                "direction that invalidates a quality claim.",
                "",
            ]
        if agreement.judge_strict:
            lines += [
                f"{len(agreement.judge_strict)} the judge failed and the human passed: "
                f"{', '.join(agreement.judge_strict)}.",
                "",
            ]

    ungrounded = [verdict for verdict in report.verdicts if not verdict.grounding.grounded]
    if ungrounded:
        lines += [
            f"### Narratives quoting figures not in their rows ({len(ungrounded)})",
            "",
            markdown_table(
                ["Case", "Invented figure(s)", "Narrative"],
                [
                    [
                        verdict.case_id,
                        ", ".join(figure.text for figure in verdict.grounding.ungrounded[:3]),
                        verdict.narrative[:70].replace("\n", " ") + "...",
                    ]
                    for verdict in ungrounded[:show_cases]
                ],
            ),
            "",
        ]

    return "\n".join(lines)


__all__ = [
    "AXES",
    "LABELS_PATH",
    "MAX_SCORE",
    "Agreement",
    "JudgeReport",
    "NarrativeVerdict",
    "Rubric",
    "build_user_prompt",
    "calibration",
    "judge_model_for",
    "judge_narrative",
    "load_labels",
    "render_judge",
    "total_usage",
]

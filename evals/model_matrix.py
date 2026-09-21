"""The same gold suite across several models: accuracy vs cost vs latency.

The ablation ladder answers "how much do the guardrails buy?" by holding the
model fixed and varying the pipeline. This answers the other question -- "which
model should this run on?" -- by holding the pipeline fixed and varying the
model. Both are the same experiment design, and the reason to write it as one is
that a model choice argued from a benchmark somebody else ran, on a suite that is
not this warehouse, is not an engineering decision.

Three things this module refuses to do, and each is a way a comparison like this
is usually misleading:

* **It will not report a degraded run.** A rate-limited provider makes the
  pipeline fall back to the deterministic templates, and a suite then completes
  in under a second reporting the template registry's coverage as the model's
  accuracy. ``EvalRun.degraded`` already detects that; here it disqualifies the
  entry rather than annotating it, because a matrix is read as a ranking and a
  footnote does not survive being read as one.
* **It will not price a model it has no price for.** ``cost_usd`` is ``None`` for
  a model absent from ``PRICING``, and ``None`` propagates: that entry reports
  tokens and declines to report cost, and is not eligible to *win* on cost.
* **It will not recommend on accuracy alone.** The recommendation is the
  cheapest model whose accuracy is within :data:`EQUIVALENCE_MARGIN` of the best
  -- because "the most accurate model" is not a decision, it is one column, and
  a 0.8pp lead that costs 17x is a trade somebody should make explicitly.

The suite is small enough that the differences here are not statistically
significant on their own. That is stated in the report rather than papered over:
:mod:`evals.variance` exists precisely because a single run of a 128-case suite
has a spread, and a matrix of single runs inherits it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evals.harness import EvalRun, run_suite
from evals.schema import GoldCase
from semantic_query_engine.core.config import EVALS_DIR
from semantic_query_engine.core.domains import active_domain
from semantic_query_engine.core.serialization import json_default
from semantic_query_engine.core.usage import TokenUsage

MATRIX_DIR = EVALS_DIR / "results" / "matrix"

# How much accuracy a cheaper model may give up and still be called equivalent,
# in percentage points. Deliberately a named constant rather than a magic 2.0:
# it is the judgement the recommendation rests on, so it should be arguable, and
# it is roughly the run-to-run spread evals/variance.py measured on this suite --
# a gap smaller than the noise is not a gap.
EQUIVALENCE_MARGIN = 2.0


# The one endpoint every row embeds through, whatever it generates through.
#
# This is the fourth thing the module refuses to do, and it is the least visible:
# Groq serves no embeddings model, so a hosted row would fail its embedding call,
# log a warning, and retrieve by keyword, while a local row retrieved by vector.
# The rows would then differ in retrieval *and* in generator, and the whole gap
# would be reported against the generator's name. Pinning it here makes the
# retrieval path a constant of the experiment rather than a consequence of which
# provider each row happens to use.
EMBEDDING_ENV = {
    "SQE_EMBEDDING_BASE_URL": "SQE_MATRIX_EMBEDDING_BASE_URL",
    "SQE_EMBEDDING_API_KEY": "SQE_MATRIX_EMBEDDING_API_KEY",
    "SQE_EMBEDDING_MODEL": "SQE_MATRIX_EMBEDDING_MODEL",
}


def embedding_environment() -> dict[str, str]:
    """The pinned embedding settings, read from the ``SQE_MATRIX_EMBEDDING_*`` vars.

    Empty when none are set, in which case every row inherits the ambient
    embedding configuration -- still identical across rows, which is the property
    that matters, but only because nothing in the loop changes it.
    """
    return {
        target: os.environ[source]
        for target, source in EMBEDDING_ENV.items()
        if os.environ.get(source)
    }


@dataclass(frozen=True)
class ModelSpec:
    """One model to measure, and how to reach it.

    ``api_key_env`` and ``base_url`` are here because a matrix worth running
    spans providers -- a frontier model, a mid-tier one and a small open model
    served locally are the three points of interest, and they are not all behind
    one key. A spec that could only vary the model *name* would be a matrix over
    one vendor, which is the comparison nobody needs.
    """

    label: str
    generator_model: str
    # Defaults to the generator model: synthesis is not what is being compared,
    # and holding it separate would add a variable to an experiment that already
    # has one.
    synthesizer_model: str = ""
    api_key_env: str = "SQE_LLM_API_KEY"
    base_url: str = ""
    # Free text for the report: why this model is in the comparison at all.
    note: str = ""

    def environment(self) -> dict[str, str]:
        """The process environment this model is measured under."""
        key = os.getenv(self.api_key_env) or ""
        env = {
            "SQE_LLM_API_KEY": key,
            "SQE_GENERATOR_MODEL": self.generator_model,
            "SQE_SYNTHESIZER_MODEL": self.synthesizer_model or self.generator_model,
        }
        if self.base_url:
            env["SQE_LLM_BASE_URL"] = self.base_url
        env.update(embedding_environment())
        return env

    @property
    def is_reachable(self) -> bool:
        """Whether this checkout has a key for it.

        Checked before the suite runs rather than discovered case by case: a
        missing key does not error, it makes every generation fall back to the
        templates, which is the failure that looks like a result.
        """
        return bool(os.getenv(self.api_key_env))


@dataclass
class MatrixEntry:
    """One model's row: what it scored, what it cost, how long it took."""

    label: str
    generator_model: str
    provider: str
    note: str = ""
    cases: int = 0
    execution_accuracy: float = 0.0
    value_accuracy: float = 0.0
    clarification_rate: float = 0.0
    first_attempt_rejection_rate: float = 0.0
    repair_success_rate: float = 0.0
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0
    total_tokens: int = 0
    cost_usd: float | None = None
    cost_per_query_usd: float | None = None
    fallback_share: float = 0.0
    # Non-empty when this entry may not be compared: a degraded run, or a model
    # that could not be reached at all. Carried rather than dropped, because
    # "we tried this model and could not measure it" is information and a silently
    # missing row is not.
    disqualified: str = ""

    @property
    def comparable(self) -> bool:
        return not self.disqualified

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MatrixReport:
    """Every model's row, plus the recommendation and its reasoning."""

    domain: str
    suite: str
    baseline: str
    started_at: str
    entries: list[MatrixEntry] = field(default_factory=list)
    recommendation: str = ""
    reasoning: str = ""
    # Caveats that belong *on* the artefact rather than in a README somebody
    # reads separately from the numbers.
    caveats: list[str] = field(default_factory=list)

    @property
    def comparable_entries(self) -> list[MatrixEntry]:
        return [entry for entry in self.entries if entry.comparable]

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "suite": self.suite,
            "baseline": self.baseline,
            "started_at": self.started_at,
            "entries": [entry.to_dict() for entry in self.entries],
            "recommendation": self.recommendation,
            "reasoning": self.reasoning,
            "caveats": self.caveats,
        }


def _percentile(values: Sequence[float], fraction: float) -> float:
    """A nearest-rank percentile.

    Not ``statistics.quantiles``: it interpolates, and an interpolated p95 over
    30 cases is a number between two latencies that nothing actually took.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * len(ordered)) - 1))
    return round(ordered[index], 1)


def summarise(run: EvalRun, spec: ModelSpec) -> MatrixEntry:
    """Turn one completed suite into one comparable row.

    Two denominators, deliberately. **Accuracy is scored over the non-adversarial
    cases only**, via the same :func:`evals.report.accuracy_cases` the ladder
    uses -- a matrix row labelled "execution accuracy" has to mean what the
    identically-labelled number in `README.md` means, or a reader will compare
    the two and be wrong. This row previously divided by every record, which put
    the ten guardrail cases in the denominator and made the matrix's accuracy
    quietly incomparable to every other accuracy in the repository.

    **Cost and latency are over every case**, because every case was actually
    run and actually paid for. Scoring cost per query over the accuracy
    denominator would understate what the suite cost by the share of adversarial
    cases in it.
    """
    from evals.report import accuracy_cases

    records = run.records
    total = len(records) or 1
    scored = accuracy_cases(records)
    scored_total = len(scored) or 1
    latencies = [record.latency_ms for record in records if record.latency_ms]
    usage: TokenUsage = run.total_usage

    rejected = [record for record in records if record.first_attempt_rejected]
    repaired = [record for record in rejected if record.repaired]

    entry = MatrixEntry(
        label=spec.label,
        generator_model=run.generator_model,
        provider=run.provider,
        note=spec.note,
        cases=len(scored),
        execution_accuracy=round(100 * sum(r.executed for r in scored) / scored_total, 1),
        value_accuracy=round(100 * sum(r.values_correct for r in scored) / scored_total, 1),
        clarification_rate=round(
            100 * sum(r.kind == "clarification" for r in scored) / scored_total, 1
        ),
        # The funnel keeps the full denominator: rejections are what the
        # guardrail cases exist to provoke, so excluding them here would hide
        # the validator working.
        first_attempt_rejection_rate=round(100 * len(rejected) / total, 1),
        repair_success_rate=round(100 * len(repaired) / len(rejected), 1) if rejected else 0.0,
        latency_p50_ms=_percentile(latencies, 0.50),
        latency_p95_ms=_percentile(latencies, 0.95),
        total_tokens=usage.total_tokens,
        cost_usd=usage.cost_usd,
        cost_per_query_usd=run.cost_per_query,
        fallback_share=round(run.fallback_share, 4),
    )
    if run.degraded:
        entry.disqualified = (
            f"{entry.fallback_share:.0%} of generations fell back to the deterministic "
            "templates -- this measures the template registry, not the model"
        )
    return entry


def recommend(entries: Sequence[MatrixEntry]) -> tuple[str, str]:
    """The cheapest model within :data:`EQUIVALENCE_MARGIN` of the best accuracy.

    Returns the label and the sentence justifying it, because a recommendation
    without its reasoning is an opinion and the whole point of the exercise is
    that this one is not.
    """
    comparable = [entry for entry in entries if entry.comparable]
    if not comparable:
        return "", "No model produced a comparable run; there is nothing to recommend."

    best = max(comparable, key=lambda entry: entry.execution_accuracy)
    within = [
        entry
        for entry in comparable
        if best.execution_accuracy - entry.execution_accuracy <= EQUIVALENCE_MARGIN
    ]
    # A model with no published price cannot win on price. It is not disqualified
    # -- it may still be the most accurate -- but "cheapest" has to mean
    # something, and an unpriced model is not cheap, it is unmeasured.
    priced = [entry for entry in within if entry.cost_per_query_usd is not None]
    if not priced:
        return best.label, (
            f"{best.label} is the most accurate at {best.execution_accuracy:.1f}% execution "
            "accuracy. No model in the equivalence band has a published price, so this "
            "recommendation rests on accuracy alone."
        )

    # Accuracy breaks a cost tie, because otherwise ``min`` breaks it by list
    # order -- and a matrix of models that all cost the same is not a corner
    # case, it is what an all-local comparison *is*: every loopback endpoint is
    # priced at exactly zero, so every row ties. Without this, such a matrix
    # could recommend a model a whole equivalence margin worse than the best for
    # no reason a reader could see, which is worse than having no recommendation.
    cheapest = min(
        priced, key=lambda entry: (entry.cost_per_query_usd or 0.0, -entry.execution_accuracy)
    )
    if cheapest.label == best.label:
        return best.label, (
            f"{best.label} is both the most accurate ({best.execution_accuracy:.1f}%) and the "
            f"cheapest of the models within {EQUIVALENCE_MARGIN:.0f}pp of it, at "
            f"${cheapest.cost_per_query_usd:.6f} per query."
        )

    gap = best.execution_accuracy - cheapest.execution_accuracy
    # The most accurate model may itself be unpriced, in which case there is no
    # cost *ratio* to quote and saying "against $0.000000" would invent one.
    if best.cost_per_query_usd is None:
        comparison = f"against an unpriced {best.label}, whose cost this run cannot state"
    else:
        multiple = (
            best.cost_per_query_usd / cheapest.cost_per_query_usd
            if cheapest.cost_per_query_usd
            else 0.0
        )
        comparison = f"against ${best.cost_per_query_usd:.6f}" + (
            f", a {multiple:.1f}x difference" if multiple else ""
        )
    return cheapest.label, (
        f"{cheapest.label} at {cheapest.execution_accuracy:.1f}% is {gap:.1f}pp behind "
        f"{best.label} ({best.execution_accuracy:.1f}%) -- inside the {EQUIVALENCE_MARGIN:.0f}pp "
        f"equivalence margin, which is about this suite's run-to-run spread -- and costs "
        f"${cheapest.cost_per_query_usd:.6f} per query {comparison}."
    )


def _retrieval_caveat() -> str:
    """State on the artefact which endpoint every row retrieved through.

    On the artefact rather than in a README: a reader comparing these accuracies
    against the ladder's needs to know whether retrieval was the same path, and
    that question arrives with the numbers, not separately from them.
    """
    pinned = embedding_environment()
    url = pinned.get("SQE_EMBEDDING_BASE_URL")
    model = pinned.get("SQE_EMBEDDING_MODEL")
    if not url and not model:
        return (
            "Retrieval was not pinned for this run: every row inherited the ambient "
            "embedding configuration. Identical across rows, but not recorded here."
        )
    return (
        f"Retrieval was pinned across every row to {model or 'the ambient model'} at "
        f"{url or 'the generation endpoint'}, so the generator is the only thing that "
        "varies between rows. A provider with no embeddings endpoint would otherwise "
        "have retrieved by keyword while a local row retrieved by vector."
    )


def run_matrix(
    cases: Sequence[GoldCase],
    models: Iterable[ModelSpec],
    *,
    baseline: str = "full",
    suite: str = "gold",
    on_model: Any = None,
) -> MatrixReport:
    """Run ``cases`` once per model and assemble the comparison.

    The environment is set per model and restored afterwards, because agents
    resolve their ``LLMSettings`` when they are *constructed* and ``run_suite``
    builds a fresh pipeline per call. That is the property this function
    depends on; if agents ever started re-reading settings per request, every
    row after the first would silently measure the last model set.
    """
    report = MatrixReport(
        domain=active_domain().name,
        suite=suite,
        baseline=baseline,
        started_at=datetime.now(timezone.utc).isoformat(),
        caveats=[
            f"One run per model over {len(cases)} cases. evals/variance.py measures a "
            "run-to-run spread on this suite, so a gap smaller than that spread is not "
            "a result -- it is noise with a decimal point.",
            "Prices are the recorded constants in core/usage.py at the time of the run, "
            "not a live feed.",
            "Every model ran the same pipeline, prompts, semantic layer and gold set, "
            "which is what makes the rows comparable to each other and not to a "
            "published benchmark.",
            _retrieval_caveat(),
        ],
    )

    preserved = {
        key: os.environ.get(key)
        for key in (
            "SQE_LLM_API_KEY",
            "SQE_GENERATOR_MODEL",
            "SQE_SYNTHESIZER_MODEL",
            "SQE_LLM_BASE_URL",
            *EMBEDDING_ENV,
        )
    }
    try:
        for spec in models:
            if not spec.is_reachable:
                report.entries.append(
                    MatrixEntry(
                        label=spec.label,
                        generator_model=spec.generator_model,
                        provider="none",
                        note=spec.note,
                        disqualified=f"no API key in {spec.api_key_env}; not run",
                    )
                )
                continue

            for key, value in spec.environment().items():
                os.environ[key] = value
            run = run_suite(cases, baseline=baseline, suite=suite)
            entry = summarise(run, spec)
            report.entries.append(entry)
            if on_model is not None:
                on_model(entry, run)
    finally:
        for key, preserved_value in preserved.items():
            if preserved_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = preserved_value

    report.recommendation, report.reasoning = recommend(report.entries)
    return report


def save_matrix(report: MatrixReport, directory: Path = MATRIX_DIR) -> Path:
    """Write the comparison where the other measured artefacts live."""
    directory.mkdir(parents=True, exist_ok=True)
    stamp = report.started_at.replace(":", "").replace("-", "")[:15]
    path = directory / f"{stamp}_{report.domain}_matrix.json"
    path.write_text(
        json.dumps(report.to_dict(), indent=2, default=json_default) + "\n", encoding="utf-8"
    )
    return path


def default_models() -> list[ModelSpec]:
    """The three points of interest: frontier, mid-tier, small open.

    Named here rather than in the CLI so the set a report was produced from is
    version-controlled next to the code that produced it. They are a starting
    point, not a claim -- ``sqe matrix --model`` overrides them.
    """
    # Three open models served locally, rather than the hosted tiers this set
    # named first. The hosted comparison is not unaffordable, it is unrunnable on
    # a free tier: the pipeline spends ~5k tokens per case and Groq's free tier
    # caps at 200k tokens *per day*, so one model's 137 cases is three days of
    # budget and a three-model matrix is ten. A partial hosted run is not a
    # cheaper version of that experiment -- it is a degraded one, which this
    # module disqualifies rather than reports. See ROADMAP.md Phase 5 task 6.
    #
    # What this set can answer is the question an operator of *this* checkout
    # actually faces: given that it runs against a local endpoint, which local
    # model should it run against? Every row is priced at exactly zero because
    # every row is loopback-served and that is true, so the cost column is a
    # constant here and the decision falls to accuracy and latency.
    return [
        ModelSpec(
            label="sqe-coder",
            generator_model="sqe-coder",
            base_url="http://localhost:11434/v1",
            note="qwen2.5-coder:7b. The default, and the model every published number in this repo was produced with.",
        ),
        ModelSpec(
            label="sqe-deepseek",
            generator_model="sqe-deepseek",
            base_url="http://localhost:11434/v1",
            note="deepseek-coder-v2:lite, a mixture-of-experts code model: more parameters, fewer active per token.",
        ),
        ModelSpec(
            label="sqe-mistral",
            generator_model="sqe-mistral",
            base_url="http://localhost:11434/v1",
            note="mistral:latest, a general instruct model rather than a code-specialised one.",
        ),
    ]


__all__ = [
    "EQUIVALENCE_MARGIN",
    "MATRIX_DIR",
    "MatrixEntry",
    "MatrixReport",
    "ModelSpec",
    "default_models",
    "recommend",
    "run_matrix",
    "save_matrix",
    "summarise",
]

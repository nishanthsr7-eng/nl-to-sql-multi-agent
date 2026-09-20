"""Run-to-run variance across repeats of the same suite at temperature 0.

The reason this module exists is that temperature 0 is not determinism. It is a
greedy decode, and greedy decoding is only deterministic if the logits are, which
they are not: batching, kernel selection and floating-point reduction order all
move the tail of the distribution, and a near-tie between two tokens resolves
differently between runs. Every number in ``evals/report.py`` is a single sample
from a distribution that nobody has measured. This measures it.

Two numbers come out, and the second one is the point.

**Spread** is what people expect: value accuracy over N runs, as a mean and a
min-max band. It is the number that belongs next to a headline accuracy, because
"58.5%" and "58.5% +/- 0.4pp over 3 runs" are different claims.

**Flip rate** is the number that spread hides. Aggregate accuracy can be stable
to a tenth of a point while a substantial share of individual cases swap between
correct and incorrect from run to run -- the wins and the losses cancel. A system
with 0pp spread and a 12% flip rate is not a stable system; it is an unstable
system being averaged over. Anyone who has shipped an LLM feature has been bitten
by exactly that, because it is the shape that survives a demo and fails a user
who asks the same question twice.

SQL churn separates the two mechanisms behind a flip. If the generated SQL is
byte-identical across runs and the verdict still moved, the instability is
downstream -- in the warehouse, the scorer, or a tie in row ordering. If the SQL
changed, it is the decode. They call for different fixes, so they are reported
apart.

Scoring is deliberately split from running, exactly as :mod:`evals.harness` is
split from :mod:`evals.report`: :func:`variance` takes recorded runs, so an
overnight 3x suite can be re-reported from ``evals/results/`` without paying for
it again.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from statistics import median, pstdev
from typing import Any

from evals.harness import CaseRecord, EvalRun
from evals.report import Latency, accuracy_cases, markdown_table, overall, pct


class VarianceError(ValueError):
    """The runs handed in cannot be compared against each other."""


def _normalise_sql(sql: str) -> str:
    """Collapse whitespace so cosmetic formatting is not counted as churn.

    Intentionally not a parse: two queries that differ only in indentation are
    the same generation for this purpose, but two that differ in a literal, an
    alias or a clause order are *not* -- that is a different decode, and hiding
    it behind a normaliser would understate exactly what this module measures.
    """
    return " ".join(sql.split())


@dataclass(frozen=True)
class CaseVariance:
    """One case's behaviour across the repeats."""

    case_id: str
    # ``values_correct`` per run, in run order. Length equals the repeat count.
    verdicts: tuple[bool, ...]
    # Distinct whitespace-normalised SQL strings the case produced.
    distinct_sql: int
    # Distinct outcome kinds (answer / clarification / failure / error).
    distinct_kinds: tuple[str, ...]
    latencies_ms: tuple[float, ...]
    # Total tokens per run. The cheapest available proxy for "did any model in
    # the pipeline emit something different this time" -- see ``token_churned``.
    total_tokens: tuple[int, ...] = ()

    @property
    def flipped(self) -> bool:
        """True when the case was not scored the same way in every run."""
        return len(set(self.verdicts)) > 1

    @property
    def correct_runs(self) -> int:
        return sum(self.verdicts)

    @property
    def sql_churned(self) -> bool:
        return self.distinct_sql > 1

    @property
    def token_churned(self) -> bool:
        """The pipeline's model output differed in length between runs.

        ``sql_churned`` only diffs the *final SQL*, which is one agent's output
        near the end of a five-agent pipeline. A run where the planner reasoned
        differently, the retriever returned a different context, or synthesis
        wrote a different narrative -- but the generator still converged on the
        same query -- is invisible to it. Token count catches that: it is a weak
        signal (equal totals do not prove equal text) but it only ever errs
        towards under-reporting, which is the safe direction for a metric whose
        job is to stop a determinism claim being made too easily.
        """
        return len(set(self.total_tokens)) > 1

    @property
    def silent_flip(self) -> bool:
        """Flipped verdict despite identical SQL every time.

        The more alarming of the two flip modes, because the model is not the
        cause: the same query returned rows that scored differently. That points
        at the warehouse, the comparison tolerance, or an unordered result being
        compared as though order mattered -- all of which are defects in the
        measurement rather than noise in the model.
        """
        return self.flipped and not self.sql_churned

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "verdicts": list(self.verdicts),
            "correct_runs": self.correct_runs,
            "distinct_sql": self.distinct_sql,
            "distinct_kinds": list(self.distinct_kinds),
            "flipped": self.flipped,
            "silent_flip": self.silent_flip,
            "sql_churned": self.sql_churned,
            "token_churned": self.token_churned,
            "latencies_ms": list(self.latencies_ms),
            "total_tokens": list(self.total_tokens),
        }


@dataclass(frozen=True)
class VarianceReport:
    """Spread and stability across N runs of one suite under one configuration."""

    suite: str
    baseline: str
    runs: int
    # Per-run value accuracy, in run order, as fractions.
    value_accuracies: tuple[float, ...]
    execution_accuracies: tuple[float, ...]
    confidently_wrong: tuple[float, ...]
    p50_latencies_ms: tuple[float, ...]
    total_tokens: tuple[int, ...]
    cases: tuple[CaseVariance, ...]

    # --- spread -----------------------------------------------------------

    @property
    def mean_value_accuracy(self) -> float:
        return sum(self.value_accuracies) / self.runs

    @property
    def value_accuracy_spread(self) -> float:
        """max - min, as a fraction. Multiply by 100 for percentage points."""
        return max(self.value_accuracies) - min(self.value_accuracies)

    @property
    def value_accuracy_stdev(self) -> float:
        """Population stdev: these N runs are the whole sample, not a draw from it."""
        return pstdev(self.value_accuracies) if self.runs > 1 else 0.0

    @property
    def latency_spread_ms(self) -> float:
        return max(self.p50_latencies_ms) - min(self.p50_latencies_ms)

    # --- stability --------------------------------------------------------

    @property
    def scored_cases(self) -> int:
        return len(self.cases)

    @property
    def flipped(self) -> list[CaseVariance]:
        return [case for case in self.cases if case.flipped]

    @property
    def flip_rate(self) -> float:
        """Share of cases that did not score identically in every run."""
        return len(self.flipped) / self.scored_cases if self.scored_cases else 0.0

    @property
    def stable_correct(self) -> int:
        return sum(1 for case in self.cases if all(case.verdicts))

    @property
    def stable_wrong(self) -> int:
        return sum(1 for case in self.cases if not any(case.verdicts))

    @property
    def sql_churn_rate(self) -> float:
        """Share of cases whose generated SQL was not identical across runs.

        Reported over every case, not just the flipped ones: SQL that changes
        while the answer stays right is the benign majority, and its size is what
        makes the flipped-and-churned subset interpretable.
        """
        churned = sum(1 for case in self.cases if case.sql_churned)
        return churned / self.scored_cases if self.scored_cases else 0.0

    @property
    def token_churn_rate(self) -> float:
        """Share of cases whose total token spend moved between runs.

        Reported next to ``sql_churn_rate`` because the gap between the two is
        the finding. A low SQL churn with a high token churn does not mean the
        model is deterministic; it means the *generator converges* while the
        agents around it do not. Publishing the SQL number alone would invite
        exactly the determinism claim this module exists to prevent.
        """
        churned = sum(1 for case in self.cases if case.token_churned)
        return churned / self.scored_cases if self.scored_cases else 0.0

    @property
    def silent_flips(self) -> list[CaseVariance]:
        return [case for case in self.cases if case.silent_flip]

    @property
    def reproducible_floor(self) -> float:
        """Accuracy if only the always-correct cases counted.

        The honest lower bound on what the system does: a case that is right two
        runs in three is not something a user can rely on, and an accuracy that
        counts it is quoting the good run.
        """
        return self.stable_correct / self.scored_cases if self.scored_cases else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite": self.suite,
            "baseline": self.baseline,
            "runs": self.runs,
            "value_accuracies": list(self.value_accuracies),
            "execution_accuracies": list(self.execution_accuracies),
            "confidently_wrong": list(self.confidently_wrong),
            "p50_latencies_ms": list(self.p50_latencies_ms),
            "total_tokens": list(self.total_tokens),
            "mean_value_accuracy": self.mean_value_accuracy,
            "value_accuracy_spread": self.value_accuracy_spread,
            "value_accuracy_stdev": self.value_accuracy_stdev,
            "flip_rate": self.flip_rate,
            "sql_churn_rate": self.sql_churn_rate,
            "token_churn_rate": self.token_churn_rate,
            "reproducible_floor": self.reproducible_floor,
            "stable_correct": self.stable_correct,
            "stable_wrong": self.stable_wrong,
            "cases": [case.to_dict() for case in self.cases],
        }


def variance(runs: Sequence[EvalRun]) -> VarianceReport:
    """Score N recorded runs of the same suite and configuration against each other.

    Refuses mismatched runs rather than reporting across them. Comparing a
    ``naive`` run to a ``full`` one would report the ladder's effect as though it
    were noise, which is the one conclusion this module must never license.
    """
    if len(runs) < 2:
        raise VarianceError("variance needs at least two runs to compare")

    baselines = {run.baseline for run in runs}
    if len(baselines) > 1:
        raise VarianceError(
            f"runs span more than one configuration ({sorted(baselines)}); "
            "variance is only meaningful within one rung"
        )
    suites = {run.suite for run in runs}
    if len(suites) > 1:
        raise VarianceError(f"runs span more than one suite ({sorted(suites)})")
    # Same reason as the rung check above, one axis over: two warehouses share
    # the rung names, so a mixed set would report the difference between
    # domains as run-to-run noise.
    domains = {run.domain for run in runs}
    if len(domains) > 1:
        raise VarianceError(
            f"runs span more than one domain ({sorted(domains)}); "
            "variance is only meaningful within one warehouse"
        )

    # ``EvalRun.degraded`` deliberately exempts ``provider == "none"``: a run with
    # no provider configured is a legitimate offline smoke test of the templates,
    # not a degradation. That exemption is wrong *here*, and dangerously so. The
    # templates are deterministic, so a no-provider repeat reports 0pp spread and
    # a 0% flip rate -- a perfect stability score for a configuration in which no
    # model ran at all. Both are refused.
    unusable = [
        run
        for run in runs
        if run.degraded or run.provider == "none" or run.fallback_share > 0.05
    ]
    if unusable:
        raise VarianceError(
            f"{len(unusable)} of {len(runs)} runs did not measure a model (degraded, "
            "or no provider configured). The deterministic fallback answers identically "
            "every time, so including one would report a stability the model did not earn."
        )

    # Only cases present in every run can be compared. The intersection is taken
    # rather than assumed equal so a suite that grew between repeats degrades to
    # a narrower comparison instead of an exception or, worse, a silently
    # misaligned one.
    per_run: list[dict[str, CaseRecord]] = [
        {record.case_id: record for record in accuracy_cases(run.records)} for run in runs
    ]
    shared = sorted(set.intersection(*(set(mapping) for mapping in per_run)))
    if not shared:
        raise VarianceError("the runs have no scored cases in common")

    cases = tuple(
        CaseVariance(
            case_id=case_id,
            verdicts=tuple(mapping[case_id].values_correct for mapping in per_run),
            distinct_sql=len({_normalise_sql(mapping[case_id].sql) for mapping in per_run}),
            distinct_kinds=tuple(sorted({mapping[case_id].kind for mapping in per_run})),
            latencies_ms=tuple(mapping[case_id].latency_ms for mapping in per_run),
            total_tokens=tuple(
                int(mapping[case_id].usage.get("total_tokens", 0)) for mapping in per_run
            ),
        )
        for case_id in shared
    )

    return VarianceReport(
        suite=runs[0].suite,
        baseline=runs[0].baseline,
        runs=len(runs),
        value_accuracies=tuple(overall(run).value_accuracy for run in runs),
        execution_accuracies=tuple(overall(run).execution_accuracy for run in runs),
        confidently_wrong=tuple(overall(run).confidently_wrong for run in runs),
        p50_latencies_ms=tuple(
            Latency.of(accuracy_cases(run.records)).p50_ms for run in runs
        ),
        total_tokens=tuple(run.total_usage.total_tokens for run in runs),
        cases=cases,
    )


def render_variance(report: VarianceReport, *, show_cases: int = 12) -> str:
    """Markdown, so the output can be pasted into the README beside the ladder."""
    lines = [
        f"## Variance -- {report.runs}x {report.suite} on the `{report.baseline}` rung",
        "",
        f"{report.scored_cases} cases scored in every run, temperature 0.",
        "",
    ]

    lines.append(
        markdown_table(
            ["Run", "Exec. acc.", "Value acc.", "Confidently wrong", "p50", "Tokens"],
            [
                [
                    f"{index + 1}",
                    pct(report.execution_accuracies[index]),
                    pct(report.value_accuracies[index]),
                    pct(report.confidently_wrong[index]),
                    f"{report.p50_latencies_ms[index] / 1000:.1f} s",
                    f"{report.total_tokens[index]:,}",
                ]
                for index in range(report.runs)
            ],
        )
    )

    lines += [
        "",
        markdown_table(
            ["Measure", "Value", "What it says"],
            [
                [
                    "Mean value accuracy",
                    pct(report.mean_value_accuracy),
                    "the number worth quoting, not any single run's",
                ],
                [
                    "Spread (max-min)",
                    f"{report.value_accuracy_spread * 100:.1f} pp",
                    "how far a single-run headline can be from the mean",
                ],
                [
                    "Stdev",
                    f"{report.value_accuracy_stdev * 100:.1f} pp",
                    "population stdev over these runs",
                ],
                [
                    "Flip rate",
                    pct(report.flip_rate),
                    "cases that did not score the same way every run",
                ],
                [
                    "SQL churn",
                    pct(report.sql_churn_rate),
                    "cases whose generated SQL was not byte-identical",
                ],
                [
                    "Token churn",
                    pct(report.token_churn_rate),
                    "cases where some model in the pipeline emitted a different length",
                ],
                [
                    "Reproducible floor",
                    pct(report.reproducible_floor),
                    "accuracy counting only always-correct cases",
                ],
                [
                    "p50 latency spread",
                    f"{report.latency_spread_ms / 1000:.1f} s",
                    "run-to-run movement in the median case",
                ],
            ],
        ),
        "",
    ]

    # The interpretation is printed rather than left to the reader, because the
    # failure mode this report guards against is quoting the spread and ignoring
    # the flip rate.
    if report.flip_rate > report.value_accuracy_spread:
        lines += [
            f"**{len(report.flipped)} cases flipped** while the headline moved only "
            f"{report.value_accuracy_spread * 100:.1f} pp -- the wins and losses cancel in the "
            "aggregate. A single-run accuracy is stable here; a single-run *answer* is not.",
            "",
        ]

    if report.token_churn_rate > report.sql_churn_rate + 0.1:
        lines += [
            f"**The SQL is stable; the pipeline is not.** SQL churned on "
            f"{pct(report.sql_churn_rate)} of cases while token spend moved on "
            f"{pct(report.token_churn_rate)} -- the generator converges on the same query "
            "while the planner, retriever and synthesis around it do not. Read the low SQL "
            "churn as convergence under these serving conditions, never as determinism.",
            "",
        ]

    if report.silent_flips:
        lines += [
            f"**{len(report.silent_flips)} flipped with identical SQL.** The model is not the "
            "cause: the same query scored differently, which points at the comparison "
            "(tolerance, row order) or the warehouse rather than the decode.",
            "",
        ]

    unstable = sorted(
        report.flipped, key=lambda case: (abs(case.correct_runs * 2 - report.runs), case.case_id)
    )[:show_cases]
    if unstable:
        lines += [
            f"### Least stable cases ({len(report.flipped)} flipped)",
            "",
            markdown_table(
                ["Case", "Correct", "Distinct SQL", "Outcomes", "Median latency"],
                [
                    [
                        case.case_id,
                        f"{case.correct_runs}/{report.runs}",
                        str(case.distinct_sql),
                        ", ".join(case.distinct_kinds),
                        f"{median(case.latencies_ms) / 1000:.1f} s",
                    ]
                    for case in unstable
                ],
            ),
        ]

    return "\n".join(lines)


__all__ = [
    "CaseVariance",
    "VarianceError",
    "VarianceReport",
    "render_variance",
    "variance",
]

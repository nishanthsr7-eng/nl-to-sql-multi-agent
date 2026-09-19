"""The gold case contract.

A case used to assert three things: the intent, the presence of some required
columns, and -- for a handful of cases -- a hand-typed expected number for the
top row. That is enough for a smoke test and not enough for a measurement, for
two reasons.

First, column presence is not correctness: ``SELECT region, 0 AS total_revenue``
passes a required-columns check. Second, hand-typed expected values do not scale
past a dozen cases, and they rot silently the moment the warehouse is rebuilt --
which is exactly when an eval suite most needs to be trusted.

So every case now carries a **reference SQL**: a hand-written, trusted query that
answers the question. Ground truth is whatever that query returns *at eval time*,
against the same warehouse the pipeline just queried. That buys three things:

* **Value accuracy becomes a real metric.** The pipeline's full result set is
  compared against the reference's, not just its first row's column names.
* **The set survives a data rebuild.** Regenerate the warehouse and the expected
  answers move with it, because they were never written down.
* **The set is auditable.** A reviewer disputing a number can read the SQL that
  produced it, which is not true of a literal in a JSON file.

The labels (``archetype``, ``sql_features``, ``difficulty``) exist so results can
be *stratified*. A single headline accuracy number invites the suspicion that the
easy cases carry it; "82% overall, 61% on window functions" is the more credible
claim precisely because it names where the system is weak.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, get_args

from semantic_query_engine.core.domains import active_domain

# The planner's four archetypes, as they appear on a case. Kept as the strings
# ``IntentArchetype`` already uses so that a case label and a pipeline intent are
# directly comparable without a mapping table between them.
Archetype = Literal[
    "descriptive_lookup", "comparative_analysis", "diagnostic_pivot", "ambiguous"
]

Difficulty = Literal["easy", "medium", "hard"]

# SQL constructs a correct answer requires. These describe the *reference* query,
# not whatever the model happens to emit -- they measure how hard the question
# is, so they stay stable while model output does not. This is the second
# stratification axis and the more actionable one: an aggregate-only accuracy of
# 90% next to a window-function accuracy of 55% says something a single number
# cannot.
SqlFeature = Literal[
    "aggregate",        # a GROUP BY / SUM / AVG is required
    "multi_filter",     # two or more independent WHERE predicates
    "date_arithmetic",  # date truncation, extraction, or interval maths
    "join",             # more than one table in the FROM
    "cte",              # a WITH clause is needed to express it
    "window_fn",        # OVER (...) -- ranking, lag, running totals
    "ratio",            # a quotient metric, so divide-by-zero handling matters
    "ordering",         # the answer is a ranking; row order is part of correctness
]

# What a case expects the pipeline to do. Separating these is what makes a
# refusal rate measurable: a suite in which "the system declined" and "the system
# got it wrong" both count as failures cannot report a guardrail working.
ExpectedOutcome = Literal["answer", "clarification", "refusal"]


class GoldCaseError(ValueError):
    """A case in the dataset is malformed. Raised at load, never at compare time."""


@dataclass(frozen=True)
class GoldCase:
    id: str
    question: str
    archetype: Archetype
    difficulty: Difficulty
    sql_features: tuple[SqlFeature, ...]
    expects: ExpectedOutcome
    # The trusted query that answers ``question``. Empty only for cases that
    # expect a clarification or a refusal, where there is no correct answer to
    # compare against and demanding one would be meaningless.
    reference_sql: str = ""
    # Columns the answer must contain. Still checked, but now as the weaker of
    # two signals: it is what "execution accuracy" means, while agreement with
    # the reference result set is what "value accuracy" means.
    required_columns: tuple[str, ...] = ()
    # Relative tolerance for float comparison against the reference result.
    tolerance: float = 0.01
    # True when row order is part of the answer (a "top 5", a ranking). When
    # False, result sets are compared as multisets: ``GROUP BY region`` has no
    # inherent order, and penalising a correct answer for emitting it in a
    # different one would be measuring nothing.
    ordered: bool = False
    notes: str = ""
    # Marks a case that probes the guardrails with hostile or impossible input
    # rather than measuring analytical accuracy. Reported separately, because
    # mixing prompt-injection cases into an accuracy denominator moves the
    # number according to how many of them you chose to write.
    adversarial: bool = False
    # Who the question is asked as. Empty means the unrestricted steward, which
    # is what every previously published number was measured under -- so an
    # existing case's meaning does not change by this field being added.
    #
    # A named principal makes two things measurable that were not. First, value
    # accuracy becomes a statement about *scoped* correctness: the same question
    # asked by two analysts has two different right answers, and ``reference_sql``
    # here is the hand-written scoped query, so a pipeline that ignored the row
    # policy scores wrong rather than merely unfiltered. Second, it is what lets
    # a row-policy breach be counted at all -- with every case running as the
    # steward no policy applies and the check can never fire.
    principal: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.expects == "answer" and not self.reference_sql:
            raise GoldCaseError(f"{self.id}: a case expecting an answer needs reference_sql")
        if self.expects != "answer" and self.reference_sql:
            raise GoldCaseError(
                f"{self.id}: expects={self.expects} has no correct result set, so "
                "reference_sql would never be compared against -- remove it"
            )
        for feature in self.sql_features:
            if feature not in get_args(SqlFeature):
                raise GoldCaseError(f"{self.id}: unknown sql_feature {feature!r}")
        if self.archetype not in get_args(Archetype):
            raise GoldCaseError(f"{self.id}: unknown archetype {self.archetype!r}")
        if self.difficulty not in get_args(Difficulty):
            raise GoldCaseError(f"{self.id}: unknown difficulty {self.difficulty!r}")
        if self.expects not in get_args(ExpectedOutcome):
            raise GoldCaseError(f"{self.id}: unknown expects {self.expects!r}")

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> GoldCase:
        try:
            return cls(
                id=raw["id"],
                question=raw["question"],
                archetype=raw["archetype"],
                difficulty=raw["difficulty"],
                sql_features=tuple(raw.get("sql_features", ())),
                expects=raw["expects"],
                reference_sql=raw.get("reference_sql", ""),
                required_columns=tuple(raw.get("required_columns", ())),
                tolerance=raw.get("tolerance", 0.01),
                ordered=raw.get("ordered", False),
                notes=raw.get("notes", ""),
                adversarial=raw.get("adversarial", False),
                principal=raw.get("principal", ""),
                tags=tuple(raw.get("tags", ())),
            )
        except KeyError as exc:
            raise GoldCaseError(f"case {raw.get('id', '<no id>')} is missing {exc}") from exc

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "question": self.question,
            "archetype": self.archetype,
            "difficulty": self.difficulty,
            "sql_features": list(self.sql_features),
            "expects": self.expects,
        }
        if self.reference_sql:
            payload["reference_sql"] = self.reference_sql
        if self.required_columns:
            payload["required_columns"] = list(self.required_columns)
        if self.tolerance != 0.01:
            payload["tolerance"] = self.tolerance
        if self.ordered:
            payload["ordered"] = True
        if self.adversarial:
            payload["adversarial"] = True
        if self.principal:
            payload["principal"] = self.principal
        if self.tags:
            payload["tags"] = list(self.tags)
        if self.notes:
            payload["notes"] = self.notes
        return payload


def load_gold_cases(path: Path | None = None) -> list[GoldCase]:
    """Load and validate every case, failing loudly on the first malformed one.

    Validation happens at load rather than during the run so that a typo in the
    dataset is a load error, not a mid-suite crash that has already burned half
    an evaluation's worth of API calls.
    """
    path = path or active_domain().gold_queries_path
    raw = json.loads(path.read_text(encoding="utf-8"))
    cases = [GoldCase.from_dict(entry) for entry in raw]

    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise GoldCaseError(f"duplicate case id {case.id!r}")
        seen.add(case.id)
    return cases


def write_gold_cases(cases: list[GoldCase], path: Path | None = None) -> None:
    path = path or active_domain().gold_queries_path
    path.write_text(
        json.dumps([case.to_dict() for case in cases], indent=2) + "\n", encoding="utf-8"
    )


__all__ = [
    "Archetype",
    "Difficulty",
    "ExpectedOutcome",
    "GoldCase",
    "GoldCaseError",
    "SqlFeature",
    "load_gold_cases",
    "write_gold_cases",
]

"""Deterministic grounding: is every number in the narrative actually in the rows?

The synthesis agent's narrative is the only part of a pipeline answer that reaches
a user as prose, and it is the only part this project has never measured. The
obvious way to measure it is an LLM judge. This module exists because the single
most important thing a judge would be asked -- *did it invent a number* -- should
never be delegated to a model in the first place.

The reasons are practical, not purist:

* **It is decidable.** A number either appears in the result set or it does not.
  Asking a model to arithmetically verify a figure against forty rows introduces
  a second thing that can hallucinate, in the exact position where the first
  one's hallucinations are being counted.
* **It is free and offline.** Every recorded run can be re-scored on this axis
  with no provider, which means it can run in the unit tier and in CI, where a
  judge cannot.
* **A judge that is wrong here is worse than no judge.** A false "grounded"
  verdict on an invented figure is precisely the confidently-wrong outcome the
  rest of the measurement layer exists to surface.

So the split is: **arithmetic is checked here, meaning is left to the judge**
(:mod:`evals.judge`). Whether "revenue grew strongly" is a fair reading of the
rows is a judgement call. Whether the narrative's "£2.86M" appears among them is
not.

The checker is deliberately **conservative in one direction**: when it cannot
resolve a token it says so rather than guessing, and an unresolvable token is not
counted as ungrounded. Over-reporting invented numbers would make the metric
useless faster than under-reporting them, because the first false positive is the
reason someone stops reading the report.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

# Magnitude suffixes the synthesis prompt uses when it compacts a figure
# ("£2.86M"). Matching them matters more than it looks: a narrative that says
# 2.86M against a row holding 2861447.4 is grounded, and a checker that cannot
# see that would report the most common formatting the agent produces as an
# invented number.
_SUFFIXES: dict[str, Decimal] = {
    "k": Decimal(1_000),
    "m": Decimal(1_000_000),
    "bn": Decimal(1_000_000_000),
    "b": Decimal(1_000_000_000),
}

# A number as it appears in prose: optional currency symbol, digits with optional
# thousands separators, optional decimals, optional magnitude suffix, optional
# percent sign. Deliberately does not match bare years -- see ``_YEARISH``.
_NUMBER = re.compile(
    r"""
    (?<![\w.])                     # not mid-identifier (v1.2, 3.5x)
    (?<!\w-)                       # not the tail of a hyphenated code (MI-006, 2022-2024)
    (?P<currency>[£$€])?
    (?P<sign>-)?
    (?P<digits>\d{1,3}(?:,\d{3})+|\d+)
    (?P<decimals>\.\d+)?
    \s*
    (?P<suffix>bn|[kmb])?
    (?P<percent>\s*%)?
    (?![\w])
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Four-digit integers in the 1900-2100 band with no currency, suffix or percent
# are almost always calendar years ("in 2024, revenue grew"), and years are not
# claims about the result set. Counting them as ungrounded figures would put a
# false positive in nearly every narrative that mentions a period.
_YEARISH = re.compile(r"^(19|20|21)\d{2}$")


@dataclass(frozen=True)
class Figure:
    """One number lifted out of a narrative, with what it was matched against."""

    text: str                    # exactly as it appeared in the prose
    value: Decimal | None        # parsed magnitude, None when unparseable
    # Half an ulp of the last digit the prose actually quoted, in the figure's
    # own units. "£2.9M" was rounded to one decimal at millions scale, so it
    # asserts nothing finer than +/-50,000 and cannot be held to more. Zero for a
    # figure quoted without decimals or a magnitude suffix, which is exact as
    # written and stays on the relative tolerance alone. See ``_quantum``.
    band: Decimal = Decimal(0)
    is_percent: bool = False
    grounded: bool = False
    # The result-set value it matched, for the report. None when unmatched.
    matched: Any = None
    # Set when the token could not be resolved to a number at all. Such a figure
    # is reported but never counted against the narrative -- see the module
    # docstring on conservatism.
    unresolved: bool = False


@dataclass
class GroundingReport:
    """Every figure in one narrative, and whether the rows support it."""

    figures: list[Figure] = field(default_factory=list)

    @property
    def checkable(self) -> list[Figure]:
        """Figures that could be parsed, and so can be held against the rows."""
        return [figure for figure in self.figures if not figure.unresolved]

    @property
    def ungrounded(self) -> list[Figure]:
        return [figure for figure in self.checkable if not figure.grounded]

    @property
    def grounded(self) -> bool:
        """True when every checkable figure was found in the result set.

        A narrative with no figures at all is vacuously grounded. That is the
        right answer -- it has invented nothing -- and it is why grounding is
        reported next to the judge's relevance score rather than alone: a
        narrative that says nothing numeric passes here and should not pass
        overall.
        """
        return not self.ungrounded

    @property
    def score(self) -> float:
        """Share of checkable figures the rows support, 1.0 when there are none."""
        checkable = self.checkable
        if not checkable:
            return 1.0
        return sum(figure.grounded for figure in checkable) / len(checkable)

    def to_dict(self) -> dict[str, Any]:
        return {
            "grounded": self.grounded,
            "score": round(self.score, 4),
            "figures": len(self.figures),
            "checkable": len(self.checkable),
            "ungrounded": [
                {"text": figure.text, "value": str(figure.value)} for figure in self.ungrounded
            ],
        }


def extract_figures(text: str) -> list[Figure]:
    """Pull every number-like token out of prose, in order of appearance."""
    figures: list[Figure] = []
    for match in _NUMBER.finditer(text or ""):
        raw = match.group(0).strip()
        digits = match.group("digits").replace(",", "")
        decimals = match.group("decimals") or ""
        suffix = (match.group("suffix") or "").lower()
        is_percent = bool(match.group("percent"))
        has_currency = bool(match.group("currency"))

        if (
            _YEARISH.match(digits)
            and not decimals
            and not suffix
            and not is_percent
            and not has_currency
        ):
            continue

        try:
            value = Decimal(f"{'-' if match.group('sign') else ''}{digits}{decimals}")
        except InvalidOperation:  # pragma: no cover - the regex shape prevents this
            figures.append(Figure(text=raw, value=None, unresolved=True))
            continue

        band = _quantum(decimals, suffix)
        if suffix:
            value *= _SUFFIXES[suffix]
        figures.append(Figure(text=raw, value=value, band=band, is_percent=is_percent))
    return figures


def _quantum(decimals: str, suffix: str) -> Decimal:
    """How much precision a figure gave up when it was written down.

    The 1% relative tolerance was tighter than the synthesis prompt's own
    rounding: "£2.9M" against a row holding 2,860,430.84 is 1.38% off and was
    scored as an invented number, which made the grounding rate mostly a
    measurement of rounding convention -- the exact thing ``check_grounding``'s
    docstring says it must not measure. 37 of 52 ungrounded figures in the first
    full judge run were this artefact rather than invention.

    A figure can only be held to the precision it claims. "£2.9M" claims one
    decimal at millions scale, so it admits +/-50,000; "£2.86M" claims two and
    admits +/-5,000. A figure with neither decimals nor a suffix was written
    exactly and gets no band at all, so a narrative saying "3" cannot be grounded
    in a 3.4 -- loosening small bare integers would buy nothing and would ground
    claims nobody rounded.
    """
    if not decimals and not suffix:
        return Decimal(0)
    places = len(decimals) - 1 if decimals else 0
    step = Decimal(10) ** -places
    if suffix:
        step *= _SUFFIXES[suffix]
    return step / 2


def _row_values(rows: list[dict[str, Any]]) -> list[Decimal]:
    """Every numeric cell in the result set, as Decimals."""
    values: list[Decimal] = []
    for row in rows:
        for cell in row.values():
            if isinstance(cell, bool):
                continue
            if isinstance(cell, int | float | Decimal):
                try:
                    value = Decimal(str(cell))
                except InvalidOperation:  # pragma: no cover - defensive
                    continue
                # A NULL numeric arrives from pandas as NaN, and ``Decimal("nan")``
                # *constructs* without complaint -- the except above never fires for
                # it. The poison only surfaces later, in the sum/max/min of
                # ``_derived_values``, which is what took the judge down mid-run on
                # the first result set containing a null. Drop non-finite values at
                # the door: a figure cannot be grounded in a value that is not one.
                if value.is_finite():
                    values.append(value)
    return values


def _derived_values(values: list[Decimal]) -> list[Decimal]:
    """Figures a faithful narrative may state that are not cells themselves.

    A summary that says "total revenue was £19.9M across four regions" is
    grounded even though neither the total nor the row count is a cell in a
    per-region result set: both follow from the rows by arithmetic the synthesis
    agent is explicitly asked to do. Counting them as invented would make the
    metric punish the agent for doing its job.

    Kept to the three the prompt actually asks for -- sum, count, and pairwise
    differences are *not* included, because admitting every derivable quantity
    would eventually ground any number at all and the check would stop meaning
    anything.
    """
    if not values:
        return []
    derived = [sum(values, Decimal(0)), Decimal(len(values)), max(values), min(values)]
    return derived


def check_grounding(
    narrative: str, rows: list[dict[str, Any]], *, tolerance: float = 0.01
) -> GroundingReport:
    """Check every figure in ``narrative`` against the values in ``rows``.

    ``tolerance`` is relative, matching :mod:`evals.compare`: a narrative rounding
    2,861,447.4 to "£2.86M" is grounded, and demanding exactness would measure the
    synthesis prompt's rounding convention rather than its honesty.
    """
    report = GroundingReport()
    candidates = _row_values(rows)
    # Percentages get their own candidate pool: a narrative saying "28%" about a
    # row holding 0.28 is correct, and the row holding 28 is a different claim
    # that happens to share digits. Both are admitted rather than guessing which
    # convention the query used, because the query's convention is not knowable
    # from the rows alone.
    percent_candidates = [value * 100 for value in candidates] + candidates
    all_candidates = candidates + _derived_values(candidates)

    for figure in extract_figures(narrative):
        if figure.value is None:
            report.figures.append(figure)
            continue
        pool = percent_candidates if figure.is_percent else all_candidates
        matched = _nearest_within(figure.value, pool, tolerance, figure.band)
        report.figures.append(
            Figure(
                text=figure.text,
                value=figure.value,
                band=figure.band,
                is_percent=figure.is_percent,
                grounded=matched is not None,
                matched=matched,
            )
        )
    return report


def _nearest_within(
    target: Decimal, candidates: list[Decimal], tolerance: float, band: Decimal = Decimal(0)
) -> Decimal | None:
    """The first candidate the figure could honestly be describing, or None.

    Two admissions, and a figure needs only one: within ``tolerance`` relative --
    which carries the large figures -- or within ``band``, the precision the
    prose gave up by rounding, which carries the compact ones. The wider of the
    two wins rather than the narrower, because each covers a case the other
    reports as invention.
    """
    limit = Decimal(str(tolerance))
    for candidate in candidates:
        if candidate == target:
            return candidate
        distance = abs(candidate - target)
        if band and distance <= band:
            return candidate
        scale = abs(candidate) if candidate else abs(target)
        if not scale:
            continue
        if distance / scale <= limit:
            return candidate
    return None


__all__ = [
    "Figure",
    "GroundingReport",
    "check_grounding",
    "extract_figures",
]

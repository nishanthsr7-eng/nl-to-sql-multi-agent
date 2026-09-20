"""The deterministic half of the narrative judge, offline.

Every test here names a narrative the check must not mis-score. Two directions
matter and they are not symmetric: a missed invented number is a gap, but a false
accusation is worse, because the first false positive is the reason someone stops
reading the report.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from evals.grounding import check_grounding, extract_figures

ROWS = [
    {"region": "PL-South", "total_revenue": 2861447.4},
    {"region": "PL-North", "total_revenue": 1500000.0},
    {"region": "PL-East", "total_revenue": 900000.0},
]


def test_an_invented_number_is_caught():
    """The failure mode the whole judge tier exists to count: a well-formed
    narrative quoting a figure that is nowhere in the rows it describes."""
    report = check_grounding("PL-South led with £4.2M in revenue.", ROWS)

    assert not report.grounded
    assert [figure.text for figure in report.ungrounded] == ["£4.2M"]


def test_a_compacted_figure_matches_the_full_precision_row():
    """"£2.86M" against a row holding 2861447.4 is the synthesis prompt's own
    formatting convention. A checker that flagged it would report the agent's
    most common output as an invented number."""
    report = check_grounding("PL-South led with £2.86M in revenue.", ROWS)

    assert report.grounded
    assert report.score == 1.0


def test_a_total_the_narrative_computed_is_grounded():
    """The prompt asks synthesis to surface a headline figure. A sum over the
    rows is arithmetic it was told to do, not a number it invented."""
    report = check_grounding("Total revenue across the three regions was £5.26M.", ROWS)

    assert report.grounded


def test_a_row_count_is_grounded():
    """"across 3 regions" is a claim about the result set's shape, and the
    result set is right there."""
    report = check_grounding("Revenue is spread across 3 regions.", ROWS)

    assert report.grounded


def test_a_year_is_not_treated_as_a_figure():
    """Calendar years are not claims about the rows. Counting them would put a
    false positive in nearly every narrative that mentions a period."""
    report = check_grounding("In 2024, PL-South led the table.", ROWS)

    assert report.checkable == []
    assert report.grounded


def test_a_year_with_a_currency_symbol_is_still_a_figure():
    """The year exemption is narrow on purpose -- "£2024" is money, and an
    exemption wide enough to swallow it would be a hole in the check."""
    report = check_grounding("The smallest line was £2024.", ROWS)

    assert [figure.text for figure in report.ungrounded] == ["£2024"]


def test_a_percentage_matches_a_fractional_row_value():
    """A narrative saying "28%" about a row holding 0.28 is correct. The query's
    convention is not knowable from the rows, so both readings are admitted."""
    rows = [{"region": "PL-South", "share": 0.28}]

    assert check_grounding("PL-South holds 28% of revenue.", rows).grounded


def test_an_identifier_is_not_read_as_a_number():
    """SKU codes and version strings are not figures. Splitting "MI-006" into a
    6 would ground or accuse nearly at random."""
    figures = extract_figures("SKU MI-006 and v1.2 were included.")

    assert figures == []


def test_a_narrative_with_no_figures_is_vacuously_grounded():
    """It has invented nothing, which is the honest answer -- and is exactly why
    grounding is never reported on its own. A narrative that says nothing
    numeric passes here and must not pass overall."""
    report = check_grounding("Revenue varied across regions.", ROWS)

    assert report.grounded
    assert report.checkable == []


def test_the_score_is_the_share_of_figures_the_rows_support():
    """Partial credit, because "one invented number in six" and "six in six" are
    different failures and a boolean would report them identically."""
    report = check_grounding("PL-South made £2.86M, PL-North £9.9M.", ROWS)

    assert report.score == 0.5
    assert not report.grounded


def test_thousands_separators_parse():
    report = check_grounding("PL-North made 1,500,000 in revenue.", ROWS)

    assert report.grounded


def test_figures_carry_their_parsed_magnitude():
    """Guards the suffix table: a 'k' read as a bare number would silently
    ground three orders of magnitude of nonsense."""
    figures = extract_figures("It rose to 2.5k from 900.")

    assert [figure.value for figure in figures] == [Decimal("2500.0"), Decimal("900")]


def test_the_real_fabricated_total_from_the_first_judge_run():
    """The case that justifies taking arithmetic away from the judge.

    On the first live judge run (2026-09-20, case ``a_revenue_by_brand``) the
    synthesis agent wrote "the top three brands collectively account for over
    £7.78M". The top three sum to £7.45M. The judge scored the narrative 2/2/2
    and volunteered that it "correctly calculates the total revenue for the top
    three brands" -- a confident endorsement of a fabricated figure, from the
    model that would otherwise have been the only thing checking it.

    The two individually-quoted brand figures in the same sentence are real,
    which is what makes it the dangerous shape: the narrative reads as carefully
    sourced right up to the number nobody can check by eye.
    """
    rows = [
        {"brand": "SnBrand2", "total_revenue": 2860430.84},
        {"brand": "YoBrand4", "total_revenue": 2473953.74},
        {"brand": "YoBrand3", "total_revenue": 2116950.30},
        {"brand": "MiBrand3", "total_revenue": 1664244.74},
    ]
    narrative = (
        "SnBrand2 leads with **£2.86M** in revenue, followed closely by YoBrand4 "
        "with **£2.47M**. The top three brands collectively account for over "
        "**£7.78M** of the total revenue."
    )

    report = check_grounding(narrative, rows)

    assert [figure.text for figure in report.ungrounded] == ["£7.78M"]
    assert report.score == 2 / 3


def test_a_null_numeric_does_not_take_the_whole_check_down():
    """A NULL cell arrives from pandas as NaN and must not poison the pool.

    ``_row_values`` guarded its ``Decimal(str(cell))`` with ``except
    InvalidOperation``, but ``Decimal("nan")`` constructs perfectly happily --
    the guard never fired. NaN then entered the candidate list and raised
    ``InvalidOperation`` from the ``sum()`` in ``_derived_values``, killing
    ``sqe judge`` partway through a 107-narrative run on the first result set
    that contained a null. The remaining figures must still be scored.
    """
    rows = [
        {"region": "North", "revenue": 1000.0},
        {"region": "South", "revenue": float("nan")},
    ]

    report = check_grounding("North led with **1,000**.", rows)

    assert report.ungrounded == []
    assert report.score == 1.0


def test_a_figure_is_held_only_to_the_precision_it_quotes():
    """"£2.9M" against 2,860,430.84 is rounding, not invention.

    The relative tolerance alone was tighter than the synthesis prompt's own
    rounding -- 1.38% off against a 1% band -- so the most common shape the agent
    produces was counted as a hallucinated number, and the grounding rate became
    a measurement of rounding convention. 37 of the 52 ungrounded figures in the
    first full judge run were this artefact.
    """
    rows = [{"brand": "SnBrand2", "total_revenue": 2860430.84}]

    report = check_grounding("SnBrand2 leads with **£2.9M**.", rows)

    assert report.ungrounded == []


def test_the_band_does_not_ground_a_figure_nobody_rounded_to():
    """The band is half an ulp, not a free pass.

    This is the guard that keeps the fix above from hollowing the metric out:
    a wider band eventually grounds any number at all, and the invented total is
    the failure this whole module exists to catch.
    """
    rows = [
        {"brand": "SnBrand2", "total_revenue": 2860430.84},
        {"brand": "YoBrand4", "total_revenue": 2473953.74},
        {"brand": "YoBrand3", "total_revenue": 2116950.30},
    ]

    report = check_grounding(
        "SnBrand2 leads with **£2.9M**. The top three total **£11.7M**.", rows
    )

    assert [figure.text for figure in report.ungrounded] == ["£11.7M"]


def test_a_bare_integer_gets_no_band():
    """"3" was written exactly; only a rounded figure gave precision up."""
    rows = [{"region": "North", "stores": 3.4}]

    report = check_grounding("North has **3** stores.", rows)

    assert [figure.text for figure in report.ungrounded] == ["3"]


def test_grounding_survives_every_recorded_narrative():
    """Re-score every committed artefact's narratives, offline.

    The grounding check is pure arithmetic over data already in the repo, so the
    whole corpus can be re-scored in the unit tier with no provider. That matters
    because the judge tier shipped without ever having been run end to end, and
    the bug that surfaced -- a NULL numeric arriving as NaN and detonating in
    ``_derived_values`` -- was invisible to every hand-written fixture while
    sitting on the path of any run whose rows contained a null. It only appeared
    40 narratives into a 107-narrative run.

    This test is the cheap standing guard: a real result set, from a real run,
    scored on every commit rather than whenever someone next spends 20 minutes
    on ``sqe judge``.
    """
    from semantic_query_engine.core.config import EVALS_DIR

    artefacts = sorted((EVALS_DIR / "results").glob("*.json"))
    if not artefacts:  # a fresh clone with no recorded runs yet
        pytest.skip("no recorded runs to re-score")

    scored = 0
    for artefact in artefacts:
        run = json.loads(artefact.read_text(encoding="utf-8"))
        for record in run.get("records", []):
            if not record.get("narrative"):
                continue
            report = check_grounding(record["narrative"], record.get("result_sample", []))
            assert 0.0 <= report.score <= 1.0
            scored += 1

    assert scored, "artefacts exist but carry no narratives to score"

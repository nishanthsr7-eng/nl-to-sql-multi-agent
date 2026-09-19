"""Result-set comparison: what "correct" means, precisely.

Two accuracies are reported, and the distinction between them is the whole point
of having both.

**Execution accuracy** asks whether the pipeline produced the right *kind* of
outcome and, for an answer, SQL that ran and came back with the columns the
question needs. It is the shape-level question: did we get a plausible answer?

**Value accuracy** asks whether that answer is *right* -- whether the rows agree
with the reference query's rows, within tolerance. It is strictly stronger, so
value accuracy can never exceed execution accuracy, and the gap between the two
columns in the report is precisely the rate at which the system produces
confident, well-formed, wrong answers. That gap is the number a reader of an
LLM-to-SQL project should care about most, and reporting only one of the two
hides it.

Three comparison rules are worth stating, because each is a choice that could
reasonably have gone the other way:

* **Rows are compared as multisets unless the case says otherwise.** A
  ``GROUP BY region`` has no inherent row order, and marking a correct answer
  wrong for emitting the regions in a different sequence measures nothing.
  Cases whose answer *is* a ranking set ``ordered``, and those are compared in
  order.
* **Only the reference query's columns are compared.** A pipeline answer that
  carries extra columns is not penalised: the question was answered. A missing
  column fails, which is what ``required_columns`` already asserted.
* **Numbers compare within a relative tolerance; everything else exactly.**
  Floating-point aggregation order differs between two correct queries, so exact
  float equality would measure DuckDB's summation order rather than the model.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

# A value the comparison treats as numeric. bool is deliberately excluded --
# comparing True against 1.0 within a tolerance would silently accept a flag
# column matching an aggregate.
_NUMERIC = (int, float, Decimal)


@dataclass(frozen=True)
class Comparison:
    """The outcome of comparing one answer against its reference result set."""

    values_match: bool
    # Why it did not match, in one line, for the failure report. Empty on a match.
    reason: str = ""
    reference_rows: int = 0
    actual_rows: int = 0


def _is_number(value: Any) -> bool:
    return isinstance(value, _NUMERIC) and not isinstance(value, bool)


def canonical(value: Any) -> Any:
    """Normalise a cell so two correct queries compare equal.

    DuckDB hands back ``date``/``datetime``/``Decimal`` depending on the
    expression that produced a column, so two queries that are analytically
    identical can return the same value in different Python types. Normalising
    here rather than at each comparison site means the rule is written once.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        return value.strip()
    return value


def values_equal(expected: Any, actual: Any, tolerance: float) -> bool:
    """Compare one cell, with a relative tolerance for numbers."""
    expected, actual = canonical(expected), canonical(actual)
    if expected is None or actual is None:
        return expected is actual
    if _is_number(expected) and _is_number(actual):
        # Relative, with an absolute floor so that an expected value of exactly
        # zero is comparable at all -- a purely relative tolerance makes 0 an
        # infinitely strict target.
        allowed = abs(float(expected)) * tolerance + 1e-9
        return abs(float(expected) - float(actual)) <= allowed
    if _is_number(expected) != _is_number(actual):
        return False
    return expected == actual


def _sort_key(row: tuple[Any, ...]) -> tuple[str, ...]:
    """A total order over rows for multiset comparison.

    Rows are sorted by their stringified cells rather than natively because a row
    can mix types across columns, and Python will not order ``str`` against
    ``int``. The ordering itself is arbitrary and only needs to be *consistent*
    between the two sides -- it exists to pair rows up, not to rank them.
    """
    return tuple(f"{type(cell).__name__}:{cell!r}" for cell in row)


def _project(rows: list[dict[str, Any]], columns: list[str]) -> list[tuple[Any, ...]]:
    return [tuple(canonical(row.get(column)) for column in columns) for row in rows]


def compare_result_sets(
    reference: list[dict[str, Any]],
    actual: list[dict[str, Any]],
    *,
    tolerance: float = 0.01,
    ordered: bool = False,
) -> Comparison:
    """Compare an answer's rows against the reference query's rows.

    ``reference`` defines the columns under comparison; extra columns in
    ``actual`` are ignored.
    """
    if not reference:
        # A reference that returns nothing cannot discriminate a right answer
        # from a wrong one, so this is a broken case rather than a failed run.
        # Saying so explicitly beats silently scoring every pipeline output as
        # correct against an empty expectation.
        return Comparison(False, "reference query returned no rows -- the case is broken", 0, len(actual))

    columns = list(reference[0].keys())
    missing = [column for column in columns if column not in (actual[0] if actual else {})]
    if missing:
        return Comparison(
            False,
            f"missing reference column(s): {sorted(missing)}",
            len(reference),
            len(actual),
        )

    expected_rows = _project(reference, columns)
    actual_rows = _project(actual, columns)

    if len(expected_rows) != len(actual_rows):
        return Comparison(
            False,
            f"row count differs: reference {len(expected_rows)}, answer {len(actual_rows)}",
            len(expected_rows),
            len(actual_rows),
        )

    if not ordered:
        expected_rows = sorted(expected_rows, key=_sort_key)
        actual_rows = sorted(actual_rows, key=_sort_key)

    for index, (expected_row, actual_row) in enumerate(zip(expected_rows, actual_rows, strict=True)):
        for column, expected_cell, actual_cell in zip(columns, expected_row, actual_row, strict=True):
            if not values_equal(expected_cell, actual_cell, tolerance):
                position = f"row {index}" + (" (ordered)" if ordered else " (after sorting)")
                return Comparison(
                    False,
                    f"{position}, column {column!r}: expected {expected_cell!r}, got {actual_cell!r}",
                    len(expected_rows),
                    len(actual_rows),
                )

    return Comparison(True, "", len(expected_rows), len(actual_rows))


__all__ = ["Comparison", "canonical", "compare_result_sets", "values_equal"]

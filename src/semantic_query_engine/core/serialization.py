"""One JSON encoder hook for the types DuckDB and pandas put in a result row.

This lives in ``core`` rather than next to either of its callers because there
are two writers of the same payload -- ``sqe ask --json`` and
``evals.harness.save_run`` -- and they diverged: the CLI passed this hook,
``save_run`` did not, so the first eval run that carried narrative rows died on
``TypeError: Object of type Timestamp is not JSON serializable`` *after* the
suite had finished, discarding 20 minutes of work at the write step.

``json.dumps(default=str)`` would "work" here, but it would silently turn a
numpy float into the string ``"2861234.56"``, which any downstream consumer --
the evaluation scorer included -- would then have to parse back. Casting through
``.item()`` keeps numbers as JSON numbers.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any


def json_default(value: Any) -> Any:
    """Serialise one value ``json.dumps`` could not handle on its own."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    item = getattr(value, "item", None)  # numpy scalars, pandas Timestamps
    if callable(item):
        try:
            unwrapped = item()
        except (ValueError, TypeError):  # pragma: no cover - defensive
            return str(value)
        if isinstance(unwrapped, (datetime, date)):
            return unwrapped.isoformat()
        if isinstance(unwrapped, (str, int, float, bool)) or unwrapped is None:
            return unwrapped
        return str(unwrapped)
    return str(value)

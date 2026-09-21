"""PII masking: which output columns carry personal data, and what a caller sees instead.

The semantic layer tags a column as personal data (see
:mod:`semantic_query_engine.governance.policy`); this module decides what that
means for the rows a question actually returns.

Deciding *which* output columns to mask is the hard half, and it is a question
about the query, not about the result. A result column is called whatever the
model aliased it, so matching on the name of a tagged column alone would miss
``SELECT crew_email AS contact`` entirely. Three rules are applied, in this
order, and the second and third exist because the first is not sufficient:

1. **Lineage.** A projection that is a bare column -- aliased or not -- is
   resolved against the base tables of its ``SELECT``, and masked when it
   resolves to a tagged column. ``SELECT c.crew_email AS contact`` masks
   ``contact``. A ``*`` expands to the tagged columns of the tables in scope.
2. **Name carry-over.** An output column whose *name* matches a tagged column is
   masked even when its lineage cannot be resolved -- which is what happens when
   the value arrives through a CTE or a subquery that this module does not trace
   into. It costs a false positive on a non-PII column that happens to share a
   tagged name; that is the direction to be wrong in.
3. **Derived expressions are rejected, not masked.** ``UPPER(crew_email)`` or
   ``crew_email || '!'`` produces a value that is still the email, in a form
   nothing downstream can mask. There is no correct masking of an arbitrary
   function of personal data, so the query is refused with
   ``IssueCode.PII_DERIVED`` instead. ``COUNT`` over a tagged column is exempt:
   counting people does not reveal them, and refusing it would make the tag mean
   "this column is unusable" rather than "this column is personal".

Personal data in ``WHERE`` is allowed. Filtering by a value the caller already
has is a lookup, not a disclosure, and the row it returns is masked like any
other.

``hash`` masking is **pseudonymisation, not anonymisation**. The salt is stable
so that the same person hashes alike within a report -- which is the entire point
of the strategy, and also exactly what makes the output re-identifiable by anyone
who can guess the values and the salt. It is the right tool for "group by
employee without seeing who", and the wrong tool for publishing.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable
from dataclasses import dataclass

import pandas as pd
from sqlglot import exp

from semantic_query_engine.governance.policy import GovernancePolicy, PIIColumn
from semantic_query_engine.governance.principals import Principal

# Aggregates that may read a tagged column without disclosing it. COUNT reduces
# any number of values to a cardinality; everything else (MIN, MAX, STRING_AGG,
# and every scalar function) either returns a value or can be made to.
_NON_DISCLOSING_AGGREGATES = ("count",)

REDACTED = "***"


@dataclass(frozen=True)
class MaskedColumn:
    """One output column that will be masked, and why."""

    output_name: str
    source: str
    classification: str
    strategy: str

    def to_dict(self) -> dict[str, str]:
        return {
            "column": self.output_name,
            "source": self.source,
            "classification": self.classification,
            "strategy": self.strategy,
        }


def _salt() -> str:
    """The pseudonymisation salt.

    Deployment configuration, not a domain fact, so it comes from the
    environment. The default is a constant rather than a random value per
    process: a salt that changed on restart would make yesterday's grouped
    report incomparable with today's, which is a subtler failure than a weak
    salt and one nobody would notice.
    """
    return os.getenv("SQE_MASK_SALT") or "sqe-default-mask-salt"


def mask_value(value: object, strategy: str) -> object:
    """Apply one masking strategy to one cell.

    ``None`` is returned unchanged by every strategy: a missing value carries no
    personal data, and replacing it with ``***`` would invent one.
    """
    if value is None:
        return None
    text = str(value)
    if strategy == "null":
        return None
    if strategy == "hash":
        digest = hashlib.sha256((_salt() + text).encode("utf-8")).hexdigest()
        return f"px_{digest[:12]}"
    if strategy == "email":
        _, _, domain = text.partition("@")
        return f"{REDACTED}@{domain}" if domain else REDACTED
    if strategy == "last4":
        return f"{REDACTED}{text[-4:]}" if len(text) > 4 else REDACTED
    # "redact" and anything unrecognised. An unknown strategy redacts rather
    # than passing the value through -- a typo in the semantic layer must not
    # silently disable masking for a column somebody deliberately tagged.
    return REDACTED


# ---------------------------------------------------------------------------
# Which output columns are personal data
# ---------------------------------------------------------------------------


def _base_tables(select: exp.Select) -> dict[str, str]:
    tables: dict[str, str] = {}

    def add(node: exp.Expression | None) -> None:
        if isinstance(node, exp.Table) and node.name:
            tables[(node.alias or node.name).lower()] = node.name.lower()

    from_clause = select.args.get("from_") or select.args.get("from")
    if isinstance(from_clause, exp.From):
        add(from_clause.this)
    for join in select.args.get("joins") or []:
        add(join.this)
    return tables


def _tagged_for(
    policy: GovernancePolicy, tables: Iterable[str]
) -> dict[str, PIIColumn]:
    """Column name -> tag, across several tables.

    Two tables tagging the same column name is a collision the semantic layer is
    free to have; the first wins, and both mask, which is the outcome either tag
    would have produced anyway.
    """
    tagged: dict[str, PIIColumn] = {}
    for table in tables:
        for column, entry in policy.pii_for(table).items():
            tagged.setdefault(column, entry)
    return tagged


def _resolve(column: exp.Column, sources: dict[str, str], policy: GovernancePolicy) -> PIIColumn | None:
    """The tag on a bare column reference, resolved against its SELECT's sources."""
    name = column.name.lower()
    qualifier = (column.table or "").lower()
    if qualifier:
        table = sources.get(qualifier, qualifier)
        return policy.pii_for(table).get(name)
    return _tagged_for(policy, sources.values()).get(name)


def derived_pii_expressions(tree: exp.Expression, policy: GovernancePolicy) -> list[str]:
    """Projections that compute over personal data in a way no mask can undo.

    Checked across every ``SELECT`` in the tree, not only the outermost: a CTE
    that projects ``UPPER(crew_email)`` has already produced the value by the
    time the outer query selects it back out under a harmless name.
    """
    if not policy.has_pii:
        return []

    offending: list[str] = []
    for select in tree.find_all(exp.Select):
        sources = _base_tables(select)
        # A select reading only CTEs has no resolvable sources; fall back to the
        # domain's tagged names, which is the same fail-closed carry-over rule
        # the output naming uses.
        tagged_names = set(_tagged_for(policy, sources.values()) or {e.column for e in policy.pii})
        for projection in select.expressions:
            inner = projection.this if isinstance(projection, exp.Alias) else projection
            if isinstance(inner, (exp.Column, exp.Star)):
                continue
            references = {c.name.lower() for c in inner.find_all(exp.Column)} & tagged_names
            if not references:
                continue
            if isinstance(inner, exp.AggFunc) and inner.sql_name().lower() in _NON_DISCLOSING_AGGREGATES:
                continue
            offending.append(
                f"{inner.sql(dialect='duckdb')} computes over the personal-data "
                f"column {', '.join(sorted(references))}"
            )
    return offending


def masked_columns(
    tree: exp.Expression, principal: Principal, policy: GovernancePolicy
) -> list[MaskedColumn]:
    """The output columns of ``tree`` that must be masked for ``principal``."""
    if principal.sees_unmasked_pii or not policy.has_pii:
        return []
    select = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    if select is None:
        return []

    sources = _base_tables(select)
    # Rule 2: anything tagged anywhere in the domain, for values arriving
    # through a CTE or subquery whose lineage is not traced.
    carry_over = {entry.column: entry for entry in policy.pii}

    found: dict[str, MaskedColumn] = {}

    def record(output_name: str, entry: PIIColumn) -> None:
        found.setdefault(
            output_name,
            MaskedColumn(
                output_name=output_name,
                source=f"{entry.table}.{entry.column}",
                classification=entry.classification,
                strategy=entry.mask,
            ),
        )

    for projection in select.expressions:
        inner = projection.this if isinstance(projection, exp.Alias) else projection
        output_name = projection.alias_or_name

        if isinstance(inner, exp.Star):
            # ``*`` and ``t.*`` project the source columns under their own
            # names, so the tag's column name *is* the output name.
            qualifier = (inner.table or "").lower() if isinstance(inner, exp.Column) else ""
            tables = [sources[qualifier]] if qualifier in sources else list(sources.values())
            for entry in _tagged_for(policy, tables).values():
                record(entry.column, entry)
            continue

        if isinstance(inner, exp.Column):
            resolved = _resolve(inner, sources, policy)
            if resolved is not None:
                record(output_name, resolved)
                continue

        if output_name.lower() in carry_over:
            record(output_name, carry_over[output_name.lower()])

    return sorted(found.values(), key=lambda column: column.output_name)


def mask_rows(
    rows: list[dict[str, object]], columns: list[MaskedColumn]
) -> list[dict[str, object]]:
    """Apply ``columns`` to result rows, returning new dicts.

    Matching is case-insensitive on the output name because DuckDB preserves the
    case the query wrote and the semantic layer stores names lower-cased.
    """
    if not columns:
        return rows
    strategies = {column.output_name.lower(): column.strategy for column in columns}
    return [
        {
            key: mask_value(value, strategies[key.lower()])
            if key.lower() in strategies
            else value
            for key, value in row.items()
        }
        for row in rows
    ]


def mask_dataframe(frame: pd.DataFrame, columns: list[MaskedColumn]) -> pd.DataFrame:
    """Mask a result frame in place of the caller, before anything else reads it.

    Applied to the frame rather than to the serialised rows because synthesis
    reads the frame: a narrative that quoted an address it was about to have
    masked out of the table underneath it would disclose exactly what the tag
    exists to prevent, and would do it in the part of the answer a user actually
    reads.
    """
    if not columns:
        return frame
    masked = frame.copy()
    by_lower = {str(name).lower(): name for name in masked.columns}
    for column in columns:
        actual = by_lower.get(column.output_name.lower())
        if actual is not None:
            strategy = column.strategy
            masked[actual] = pd.Series(
                [mask_value(value, strategy) for value in masked[actual]],
                index=masked.index,
            )
    return masked


__all__ = [
    "REDACTED",
    "MaskedColumn",
    "derived_pii_expressions",
    "mask_dataframe",
    "mask_rows",
    "mask_value",
    "masked_columns",
]

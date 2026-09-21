"""What the domain declares about who may see which rows, and which columns are PII.

Both are facts about the warehouse, not about the deployment, so both live in the
domain's ``semantic_layer.json`` next to the grain and the metric formulas -- for
the same reason every other business fact does. A policy written in Python would
be a ninth copy of data the semantic layer already owns, and a second domain
would need a code change to be governed at all, which is precisely the claim
Phase 4 exists to disprove.

The *principals* -- who holds which grants -- are deliberately **not** here. Those
are a property of the deployment and live in a separate file per domain; see
:mod:`semantic_query_engine.governance.principals`.

Two shapes of row policy, because two shapes occur in every star schema:

``direct``
    The table carries the scoping column itself (``dim_store.region``). The
    predicate is an ``IN`` list on that column.
``semijoin``
    The table carries only a key into the table that does
    (``fmcg_sales.store_id``). The predicate is an ``IN (SELECT ...)`` against
    the scoping table.

The second is the one that matters. The obvious implementation -- "filter on
``region`` when the query joined ``dim_store``" -- leaks the entire fact table to
any query that simply does not join it, which is most of them. Scoping a fact
through its own key means the restriction holds whether or not the model chose to
write the join, and cannot be removed by rewriting the FROM clause.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from semantic_query_engine.domain.registry import GrainRegistry
from semantic_query_engine.semantic.layer import SemanticLayer, load_semantic_layer

DIRECT = "direct"
SEMIJOIN = "semijoin"


class GovernancePolicyError(ValueError):
    """The governance block is malformed. Raised at load, never at query time."""


@dataclass(frozen=True)
class ScopedTable:
    """How one table is restricted under one row policy."""

    table: str
    mode: str
    # The column on ``table`` the predicate is built from: the scoping column
    # itself under ``direct``, the key into the scoping table under ``semijoin``.
    key: str
    # ``semijoin`` only: the table the key points at, the column it joins on, and
    # the column on *that* table the grant values are compared against.
    through_table: str = ""
    through_column: str = ""
    through_filter_column: str = ""
    # ``semijoin`` through a *slowly-changing* dimension. Without these, the
    # semi-join asks "did this key ever belong to a granted scope?", which is a
    # different and strictly wider question than "did it belong to one when this
    # row happened". An airframe that transfers between carriers makes the
    # difference concrete: both the old and the new operator can read the whole
    # of its fuel history, including the years they did not own it.
    #
    # ``as_of`` is the date column on the *scoped* table -- the fact's own event
    # date -- and the two validity columns are the window on the dimension. All
    # three are required together; a partial declaration is refused at load
    # rather than silently degrading to the wider predicate, because the wider
    # predicate is the bug this exists to close.
    through_valid_from: str = ""
    through_valid_to: str = ""
    as_of: str = ""

    @property
    def is_semijoin(self) -> bool:
        return self.mode == SEMIJOIN

    @property
    def is_temporal(self) -> bool:
        """True when the scoping dimension's validity window must be honoured."""
        return bool(self.through_valid_from and self.through_valid_to and self.as_of)


@dataclass(frozen=True)
class RowPolicy:
    """One dimension of row-level restriction across the whole warehouse.

    ``grant_key`` is what a principal's grants are keyed by, so a principal reads
    as ``{"region": ["PL-North"]}`` rather than naming a policy. The key is the
    dimension, not the policy, because that is the vocabulary the rest of the
    semantic layer already uses for the same values.
    """

    name: str
    grant_key: str
    anchor_table: str
    anchor_column: str
    description: str
    tables: dict[str, ScopedTable]

    def scope_for(self, table: str) -> ScopedTable | None:
        return self.tables.get(table.lower())


@dataclass(frozen=True)
class PIIColumn:
    """One column carrying personal data, and how it is rendered to a caller without access."""

    table: str
    column: str
    classification: str
    # Which masking strategy applies; see :mod:`semantic_query_engine.governance.masking`.
    mask: str
    description: str = ""


class GovernancePolicy:
    """The ``governance`` block of one semantic layer, parsed.

    An absent block is a valid, fully-permissive policy rather than an error: a
    domain that declares no policies is ungoverned, which is what every domain
    was before this module existed, and a hard failure would make governance a
    breaking change to the semantic-layer schema rather than an addition to it.
    """

    def __init__(self, governance: dict[str, Any] | None):
        raw = governance or {}
        self.row_policies: list[RowPolicy] = [
            self._build_row_policy(entry) for entry in raw.get("row_policies", [])
        ]
        self.pii: list[PIIColumn] = [
            PIIColumn(
                table=str(entry.get("table", "")).lower(),
                column=str(entry.get("column", "")).lower(),
                classification=str(entry.get("classification", "unclassified")),
                mask=str(entry.get("mask", "redact")),
                description=str(entry.get("description", "")),
            )
            for entry in raw.get("pii", [])
        ]

    @staticmethod
    def _build_row_policy(entry: dict[str, Any]) -> RowPolicy:
        tables: dict[str, ScopedTable] = {}
        for scope in entry.get("tables", []):
            through = scope.get("through") or {}
            name = str(scope.get("table", "")).lower()
            temporal = {
                "through_valid_from": str(through.get("valid_from", "")).lower(),
                "through_valid_to": str(through.get("valid_to", "")).lower(),
                "as_of": str(through.get("as_of", "")).lower(),
            }
            # All three or none. A half-written temporal scope is the shape of
            # somebody starting the change and not finishing it, and the failure
            # mode of accepting it is a predicate that quietly reverts to the
            # wider, ever-belonged-to form.
            if any(temporal.values()) and not all(temporal.values()):
                missing = sorted(key for key, value in temporal.items() if not value)
                raise GovernancePolicyError(
                    f"{name}: a temporal semi-join needs valid_from, valid_to and "
                    f"as_of together; missing {missing}"
                )
            tables[name] = ScopedTable(
                table=name,
                mode=str(scope.get("mode", DIRECT)),
                key=str(scope.get("key", "")).lower(),
                through_table=str(through.get("table", "")).lower(),
                through_column=str(through.get("column", "")).lower(),
                through_filter_column=str(through.get("filter_column", "")).lower(),
                **temporal,
            )
        return RowPolicy(
            name=str(entry.get("name", "")),
            grant_key=str(entry.get("grant_key", "")),
            anchor_table=str(entry.get("anchor_table", "")).lower(),
            anchor_column=str(entry.get("anchor_column", "")).lower(),
            description=str(entry.get("description", "")),
            tables=tables,
        )

    # -----------------------------------------------------------------------
    # Queries over the policy
    # -----------------------------------------------------------------------

    @property
    def grant_keys(self) -> list[str]:
        return [policy.grant_key for policy in self.row_policies]

    def policies_for(self, table: str) -> list[tuple[RowPolicy, ScopedTable]]:
        """Every (policy, scope) pair that restricts ``table``."""
        pairs = []
        for policy in self.row_policies:
            scope = policy.scope_for(table)
            if scope is not None:
                pairs.append((policy, scope))
        return pairs

    def pii_for(self, table: str) -> dict[str, PIIColumn]:
        """Column name -> tag, for the PII columns of one table."""
        return {entry.column: entry for entry in self.pii if entry.table == table.lower()}

    @property
    def has_pii(self) -> bool:
        return bool(self.pii)

    def unguarded_tables(
        self, grains: GrainRegistry, allowed_tables: frozenset[str]
    ) -> dict[str, list[str]]:
        """Tables that hold restricted data but declare no scope. Should always be empty.

        This is the check that keeps row-level security from decaying silently.
        A table absent from a policy's ``tables`` list is queried *unfiltered*,
        which is right for ``dim_product`` and catastrophic for a new fact table
        somebody added last week. Nothing in the SQL layer can tell those two
        apart -- so the semantic layer is asked instead, and the rule is
        mechanical: a table is restricted data when it carries either the
        policy's scoping column or the scoping table's own grain key. Those are
        exactly the two ways a row can be attributed to a scope.

        Declaring a scope for a table the rule would not have required is fine
        and is not reported; the check is a floor, not an exact match.

        Returned as a mapping rather than raised, so a caller can decide whether
        an omission is a startup failure or a report. A test asserts it is empty
        for every shipped domain.
        """
        gaps: dict[str, list[str]] = {}
        for policy in self.row_policies:
            anchor_grain = grains.grain(policy.anchor_table)
            anchor_keys = set(anchor_grain.key_columns) if anchor_grain else set()
            identifying = anchor_keys | {policy.anchor_column}
            for table in sorted(allowed_tables):
                if table == policy.anchor_table or policy.scope_for(table) is not None:
                    continue
                carried = sorted(c for c in identifying if grains.owns_column(table, c))
                if carried:
                    gaps.setdefault(table, []).extend(
                        f"{policy.name}:{column}" for column in carried
                    )
        return gaps


def load_governance_policy(layer: SemanticLayer | None = None) -> GovernancePolicy:
    """Build the policy for a domain's semantic layer (loading it if not given)."""
    layer = layer or load_semantic_layer()
    return GovernancePolicy(layer.governance)


__all__ = [
    "DIRECT",
    "SEMIJOIN",
    "GovernancePolicy",
    "GovernancePolicyError",
    "PIIColumn",
    "RowPolicy",
    "ScopedTable",
    "load_governance_policy",
]

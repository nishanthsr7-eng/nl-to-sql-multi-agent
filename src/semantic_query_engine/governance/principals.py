"""Who is asking, and what they are allowed to see.

Deliberately not in the semantic layer. The semantic layer is the contract for
what the *warehouse* means -- grain, metrics, vocabulary, which columns are
personal data -- and it is checked into the repo as a description of the data.
Who holds which grant is a property of a deployment: it changes when somebody
joins a team, it differs between staging and production, and it is the part a
real system would read from an identity provider rather than from a file.
Keeping the two apart is what makes the file below replaceable by an OIDC token
without touching a line of the policy.

Three rules here are load-bearing, and all three are the fail-closed direction:

* **An unknown principal id is an error, not an anonymous fallback.** Resolving
  it to "no grants" would be safe; resolving it to a default would not, and a
  typo in a caller's identity must not quietly become someone else's access.
* **Zero grants means zero rows, not no filter.** ``grants={"region": []}`` is a
  principal who has been given nothing. The empty-``IN``-list bug -- where an
  empty grant set compiles to no predicate at all -- is the single most common
  way row-level security is wrong in practice, so it is spelled out in
  :meth:`Principal.grant_values` and tested directly.
* **Unrestricted is opt-in and explicit.** ``unrestricted: true`` in the file;
  never inferred from a missing ``grants`` key, which is what a half-written
  principal looks like.

The process default is :data:`STEWARD`, an unrestricted principal. That is a
compatibility decision and worth stating plainly: every number this project has
published -- retail's 19,951,300.58, every gold-set score -- was measured without
row restriction, and a default that filtered would silently change all of them.
Restriction is therefore something a caller asks for (``sqe ask --as ...``,
``{"principal": ...}`` on the API), and the audit log records which principal
each run actually ran as.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from semantic_query_engine.core.domains import Domain, active_domain

# What a principal may do with a column tagged as personal data.
MASKED = "masked"
UNMASKED = "unmasked"


class PrincipalError(RuntimeError):
    """No such principal, or the principal file could not be read."""


@dataclass(frozen=True)
class Principal:
    """One identity, its grants, and its PII access level."""

    id: str
    role: str = "analyst"
    description: str = ""
    # Grant key (a policy's ``grant_key``, e.g. "region") -> the values held.
    # A key that is absent entirely means this principal is not scoped by that
    # policy; a key present with an empty list means it is scoped to nothing.
    # The difference is the whole point -- see :meth:`grant_values`.
    grants: dict[str, tuple[str, ...]] = field(default_factory=dict)
    unrestricted: bool = False
    pii_access: str = MASKED

    @property
    def sees_unmasked_pii(self) -> bool:
        return self.pii_access == UNMASKED

    def is_scoped_by(self, grant_key: str) -> bool:
        """True when this principal is subject to the policy keyed by ``grant_key``."""
        return not self.unrestricted and grant_key in self.grants

    def grant_values(self, grant_key: str) -> tuple[str, ...]:
        """The values this principal holds for ``grant_key``.

        Only meaningful when :meth:`is_scoped_by` is true. An empty tuple is a
        real answer -- "granted nothing" -- and the predicate builder turns it
        into ``FALSE`` rather than into no predicate at all.
        """
        return self.grants.get(grant_key, ())

    def to_dict(self) -> dict[str, Any]:
        """The identity as recorded in the audit log and echoed on a result."""
        return {
            "id": self.id,
            "role": self.role,
            "unrestricted": self.unrestricted,
            "grants": {key: list(values) for key, values in sorted(self.grants.items())},
            "pii_access": self.pii_access,
        }


# The default identity for any caller that does not name one. See the module
# docstring: unrestricted, because every published number was measured that way.
STEWARD = Principal(
    id="steward",
    role="data_steward",
    description="The default process identity: no row restriction, unmasked PII.",
    unrestricted=True,
    pii_access=UNMASKED,
)


class PrincipalRegistry:
    """The principals one domain declares, plus the default."""

    def __init__(self, principals: list[Principal]):
        self._by_id = {principal.id: principal for principal in principals}
        # The default is always resolvable, even from an empty or missing file,
        # so the ordinary un-governed path cannot be broken by a deployment that
        # never wrote one.
        self._by_id.setdefault(STEWARD.id, STEWARD)

    def get(self, principal_id: str) -> Principal:
        try:
            return self._by_id[principal_id]
        except KeyError:
            known = ", ".join(sorted(self._by_id)) or "none"
            raise PrincipalError(
                f"No such principal: {principal_id!r}. Declared principals: {known}."
            ) from None

    def all(self) -> list[Principal]:
        return [self._by_id[key] for key in sorted(self._by_id)]

    def __contains__(self, principal_id: object) -> bool:
        return principal_id in self._by_id


def _parse(entry: dict[str, Any]) -> Principal:
    raw_grants = entry.get("grants") or {}
    return Principal(
        id=str(entry["id"]),
        role=str(entry.get("role", "analyst")),
        description=str(entry.get("description", "")),
        grants={
            str(key): tuple(str(value) for value in values)
            for key, values in raw_grants.items()
        },
        unrestricted=bool(entry.get("unrestricted", False)),
        pii_access=str(entry.get("pii_access", MASKED)),
    )


def load_principals(path: Path | None = None, domain: Domain | None = None) -> PrincipalRegistry:
    """Read a domain's principal file.

    A missing file yields a registry holding only :data:`STEWARD`: a checkout
    that has declared no identities is one where nobody has been restricted yet,
    which is a coherent state and not a startup failure. A file that exists and
    is malformed *is* an error -- silently ignoring it would drop grants that
    somebody wrote down, and the failure mode of dropping a grant is granting
    more access than intended.
    """
    path = path or (domain or active_domain()).principals_path
    if not path.exists():
        return PrincipalRegistry([])
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        return PrincipalRegistry([_parse(entry) for entry in payload.get("principals", [])])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PrincipalError(f"Could not read the principal file at {path}: {exc}") from exc


def resolve_principal(principal_id: str | None, domain: Domain | None = None) -> Principal:
    """The principal a run should execute as.

    ``None`` and the empty string both mean "the caller named nobody", which
    resolves via ``SQE_PRINCIPAL`` and then to :data:`STEWARD`. An id that *was*
    named and is not declared raises -- see the module docstring.
    """
    named = principal_id or os.getenv("SQE_PRINCIPAL") or ""
    if not named:
        return STEWARD
    return load_principals(domain=domain).get(named)


__all__ = [
    "MASKED",
    "STEWARD",
    "UNMASKED",
    "Principal",
    "PrincipalError",
    "PrincipalRegistry",
    "load_principals",
    "resolve_principal",
]

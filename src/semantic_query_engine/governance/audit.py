"""The append-only record of every query this system ran, and who ran it.

An audit log is only worth having if two things are true of it: nothing that
touched the warehouse is missing from it, and nobody can quietly edit it
afterwards. Both are design problems rather than features, and both are solved
here in the least clever way that actually works.

**Nothing is missing.** The log is written from the orchestrator's ``finally``
block, so a run that raised, timed out, returned no rows, or was refused by a
guardrail is recorded exactly like one that answered. A log of successes is a
usage report; the rows a reader of an audit trail is looking for are the refusals
and the empty results, because those are what a restricted principal's day looks
like when the policy is working.

**Nothing can be quietly edited.** Each record carries the SHA-256 of the record
before it, so the file is a hash chain: changing or deleting an entry invalidates
every hash after it, and :func:`verify_chain` says where. This does not make the
log tamper-*proof* -- anyone who can write the file can rewrite the whole chain
from the edit onwards -- and claiming otherwise would be the sort of security
theatre this project exists to argue against. It makes it tamper-*evident*
against the realistic case: an edit in the middle, or a deletion, by somebody who
did not think about the chain. Shipping the anchor off-box is what would close
the rest, and that is a deployment concern, not a code one.

**JSONL, one object per line, opened in append mode.** A single ``write`` of a
line under the platform's append semantics is the cheapest thing that survives
two processes logging at once without a lock; a JSON *array* would require
rewriting the closing bracket on every append, which is the opposite of
append-only.

The SQL recorded is the SQL that **executed** -- bounded, and carrying its
injected row predicates -- not the SQL the model generated. Those differ on every
governed run, and the one worth keeping is the one the warehouse saw.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from semantic_query_engine.core.domains import active_domain

# Bumped when the meaning of a field changes, so a reader can tell an old record
# from a new one without guessing from which keys are present.
SCHEMA_VERSION = 1

# The hash a first record chains from. A constant rather than an empty string so
# that a truncated-to-nothing log is distinguishable from a fresh one only by
# the anchor being where it belongs -- and so ``verify_chain`` has something to
# check the first record against rather than trusting it by default.
GENESIS = "0" * 64


@dataclass(frozen=True)
class AuditRecord:
    """One executed (or refused) query, as it is written to the log."""

    run_id: str
    timestamp: str
    domain: str
    principal: str
    role: str
    question: str
    # The SQL that reached the warehouse, bounded and with row predicates
    # injected. Empty when the run never got that far.
    sql: str
    outcome: str
    row_count: int
    elapsed_ms: float
    sql_source: str = ""
    issue_codes: list[str] = field(default_factory=list)
    applied_policies: list[dict[str, str]] = field(default_factory=list)
    masked_columns: list[dict[str, str]] = field(default_factory=list)
    cost_usd: float | None = None
    schema_version: int = SCHEMA_VERSION

    def content(self) -> dict[str, Any]:
        """Everything the hash covers -- i.e. the record minus the chain fields."""
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "timestamp": self.timestamp,
            "domain": self.domain,
            "principal": self.principal,
            "role": self.role,
            "question": self.question,
            "sql": self.sql,
            "sql_source": self.sql_source,
            "outcome": self.outcome,
            "row_count": self.row_count,
            "elapsed_ms": self.elapsed_ms,
            "issue_codes": self.issue_codes,
            "applied_policies": self.applied_policies,
            "masked_columns": self.masked_columns,
            "cost_usd": self.cost_usd,
        }


def _digest(content: dict[str, Any], previous_hash: str) -> str:
    """The chain hash for one record.

    ``sort_keys`` is what makes this reproducible: the hash must depend on the
    record's *content*, not on the order a dict happened to serialise in, or
    re-verifying a log written by a different Python would fail for no reason.
    """
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256((previous_hash + canonical).encode("utf-8")).hexdigest()


class AuditLog:
    """Append-only JSONL log for one domain."""

    def __init__(self, path: Path):
        self.path = path

    # -- writing ----------------------------------------------------------

    def _last_hash(self) -> str:
        """The hash of the final record, or :data:`GENESIS` for an empty log.

        Read from the end of the file on each append. That is one file read per
        query, which is cheap next to the query itself, and it is what keeps the
        chain correct when a second process has appended since this one started
        -- an in-memory "last hash" would fork the chain silently.
        """
        if not self.path.exists():
            return GENESIS
        last = ""
        with open(self.path, encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    last = line
        if not last:
            return GENESIS
        try:
            return str(json.loads(last).get("hash", GENESIS))
        except ValueError:
            # A truncated final line (a process killed mid-write) must not be
            # silently chained onto as though it were valid; chaining from
            # GENESIS makes verify_chain report the break at exactly that row.
            return GENESIS

    def append(self, record: AuditRecord) -> dict[str, Any]:
        """Write one record and return it as stored, including its chain fields."""
        content = record.content()
        previous = self._last_hash()
        entry = {**content, "prev_hash": previous, "hash": _digest(content, previous)}

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, default=str) + "\n")
        return entry

    # -- reading ----------------------------------------------------------

    def read(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Records oldest-first, or the most recent ``limit`` of them.

        A line that will not parse is returned as a marked placeholder rather
        than raising. One corrupt line -- a process killed mid-write, or an
        edit made with a text editor -- must not make the whole log unreadable,
        because the moment somebody needs to read an audit log is exactly the
        moment it may have been damaged. The placeholder carries no ``hash``,
        so :func:`verify_chain` reports it as a break where it is instead of
        letting it pass unseen.
        """
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        with open(self.path, encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    records.append(json.loads(line))
                except ValueError:
                    records.append({"run_id": "?", "unparseable": line.strip()[:200]})
        return records[-limit:] if limit else records

    def verify(self) -> list[str]:
        """Empty when the chain is intact; otherwise what is wrong and where."""
        return verify_chain(self.read())


def verify_chain(records: list[dict[str, Any]]) -> list[str]:
    """Check a hash chain, reporting every break rather than only the first.

    Reporting all of them matters: a single edited record breaks its own hash
    *and* the link of the one after it, and seeing both is how a reader tells an
    edit apart from a deletion, which breaks only the link.
    """
    problems: list[str] = []
    previous = GENESIS
    for index, record in enumerate(records):
        stored_prev = str(record.get("prev_hash", ""))
        stored_hash = str(record.get("hash", ""))
        content = {key: value for key, value in record.items() if key not in ("prev_hash", "hash")}

        if stored_prev != previous:
            problems.append(
                f"record {index} ({record.get('run_id', '?')}): chain break -- "
                f"prev_hash does not match the previous record's hash"
            )
        if _digest(content, stored_prev) != stored_hash:
            problems.append(
                f"record {index} ({record.get('run_id', '?')}): content does not "
                f"match its own hash -- this record was edited"
            )
        previous = stored_hash
    return problems


def audit_log(path: Path | None = None) -> AuditLog:
    """The active domain's log, unless a path is given."""
    return AuditLog(path or active_domain().audit_log_path)


def audit_enabled() -> bool:
    """Whether runs are logged.

    On by default: an audit trail that had to be switched on is one that is off
    on the machine where it mattered. ``SQE_AUDIT=0`` exists for the test suite
    and for a throwaway container, and the fact that a run was *not* logged is
    then visible in the absence of the file rather than in a config nobody reads.
    """
    return (os.getenv("SQE_AUDIT") or "1").strip().lower() not in ("0", "false", "off", "no")


__all__ = [
    "GENESIS",
    "SCHEMA_VERSION",
    "AuditLog",
    "AuditRecord",
    "audit_enabled",
    "audit_log",
    "verify_chain",
]


def utc_now() -> str:
    """An ISO-8601 timestamp in UTC.

    UTC rather than local time because an audit trail is read by somebody
    correlating it with other systems, and a local timestamp with no offset is
    the single most common way that correlation goes wrong.
    """
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

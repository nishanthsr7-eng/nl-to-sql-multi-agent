"""Domains: which warehouse, which semantic layer, which gold set.

Phase 4's claim is that the semantic layer is an *abstraction*, not a config file
for one CSV. The only way to make that claim checkable is to run a second,
unrelated vertical through the identical pipeline -- so everything that used to
be a module-level constant pointing at the FMCG files (the DuckDB path, the
semantic layer, the embedding cache, the gold set, the CSV-to-table mapping) is
now a property of a :class:`Domain`, declared in data, and selected at runtime by
``sqe --domain <name>``.

A domain is a directory under ``data/domains/`` holding a ``domain.json``
manifest. The manifest owns *paths only*; everything about the business -- the
tables, the metrics, the dimension vocabulary, the planner's keywords, the
few-shot examples -- lives in that domain's ``semantic_layer.json``. That split
is what keeps "add a domain" a data change: a manifest that could carry business
facts would be a second semantic layer with a different name, which is exactly
the duplication the registry work removed.

Path values in a manifest are relative to :data:`PROJECT_ROOT`, not to the
manifest, so they read the same as every other path in the repo.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from types import MappingProxyType

from semantic_query_engine.core.config import (
    DATA_DIR,
    DUCKDB_PATH_OVERRIDE,
    PROJECT_ROOT,
)

DOMAINS_DIR = DATA_DIR / "domains"
DEFAULT_DOMAIN = "retail"

MANIFEST_NAME = "domain.json"


class DomainError(RuntimeError):
    """A domain was requested that does not exist, or whose manifest is unusable."""


@dataclass(frozen=True)
class Domain:
    """One warehouse, one semantic layer, one gold set.

    Constructed only by :func:`get_domain`. Frozen because half the pipeline
    reads these paths during construction: a domain that could be mutated after
    an agent captured it would give that agent a stale warehouse and a live
    semantic layer.
    """

    name: str
    title: str
    description: str
    root: Path
    semantic_layer_path: Path
    warehouse_path: Path
    vector_cache_path: Path
    gold_queries_path: Path
    # One semantic-cache file per domain. A shared one would let a retail
    # question match an airline one on the filler words they share and return
    # SQL over tables that do not exist -- caught by the validator, but only
    # after the cache had already skipped the generation that would have been
    # right.
    cache_path: Path
    # Who may see which rows, for this warehouse. A path rather than the
    # grants themselves for the same reason the warehouse is a path: the
    # manifest describes where a domain's things are, and a deployment that
    # reads its principals from an identity provider replaces this one file
    # without touching the rest of the domain.
    principals_path: Path
    # Append-only governance log. Per domain, because an audit trail that mixed
    # two warehouses would make "every query against the retail data" an
    # exercise in filtering rather than a file.
    audit_log_path: Path
    tables: Mapping[str, Path]
    # Name of the deterministic fallback template set this domain ships, or "".
    # A domain with no templates fails a generation outright rather than
    # answering it with another domain's SQL -- see ``sql_generator``.
    fallback_templates: str
    # How the generator that produced this domain's CSVs is invoked, quoted back
    # in the error when a source file is missing. Every domain in this repo is
    # generated under a fixed seed; none of the CSVs are hand-edited.
    build_command: str

    @property
    def allowed_tables(self) -> frozenset[str]:
        """The queryable table names, read from this domain's semantic layer.

        Derived rather than declared in the manifest: the manifest already says
        which CSV backs which table, and a second list would be free to disagree
        with the semantic layer about what exists.
        """
        return _allowed_tables_for(self.semantic_layer_path)


@cache
def _allowed_tables_for(semantic_layer_path: Path) -> frozenset[str]:
    """Table names declared by a semantic layer document.

    A missing or malformed file yields an empty set rather than raising, so a
    bad path surfaces downstream as an ``unknown_table`` rejection from the
    validator instead of an exception at import time.
    """
    try:
        with open(semantic_layer_path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return frozenset()
    return frozenset(
        table["table_name"] for table in payload.get("tables", []) if table.get("table_name")
    )


def _resolve(value: str) -> Path:
    return (PROJECT_ROOT / value).resolve()


@cache
def get_domain(name: str) -> Domain:
    """Load one domain's manifest.

    Raises
    ------
    DomainError
        No such domain, or its manifest is missing required keys.
    """
    manifest_path = DOMAINS_DIR / name / MANIFEST_NAME
    if not manifest_path.exists():
        known = ", ".join(sorted(available_domain_names())) or "none"
        raise DomainError(
            f"No such domain: {name!r}. Domains are directories under {DOMAINS_DIR} "
            f"holding a {MANIFEST_NAME}. Available: {known}."
        )

    try:
        with open(manifest_path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        raise DomainError(f"Could not read the domain manifest at {manifest_path}: {exc}") from exc

    for key in ("semantic_layer", "warehouse", "tables"):
        if key not in payload:
            raise DomainError(f"{manifest_path} is missing the required key {key!r}.")

    # SQE_DUCKDB_PATH predates domains and is how the test suite redirects the
    # warehouse to a temp file. It applies to the default domain only: a single
    # override honoured by every domain would point two different warehouses at
    # one file, and the second one loaded would silently serve the first one's
    # tables.
    warehouse = _resolve(payload["warehouse"])
    if name == DEFAULT_DOMAIN and DUCKDB_PATH_OVERRIDE:
        warehouse = Path(DUCKDB_PATH_OVERRIDE)

    return Domain(
        name=name,
        title=payload.get("title", name),
        description=payload.get("description", ""),
        root=manifest_path.parent,
        semantic_layer_path=_resolve(payload["semantic_layer"]),
        warehouse_path=warehouse,
        vector_cache_path=_resolve(
            payload.get("vector_cache", f"data/warehouse/{name}_embedding_cache.json")
        ),
        gold_queries_path=_resolve(
            payload.get("gold_queries", f"evals/datasets/{name}_gold_queries.json")
        ),
        cache_path=_resolve(
            payload.get("semantic_cache", f"data/warehouse/{name}_semantic_cache.json")
        ),
        principals_path=_resolve(
            payload.get("principals", f"data/domains/{name}/principals.json")
        ),
        audit_log_path=_resolve(
            payload.get("audit_log", f"data/audit/{name}_audit.jsonl")
        ),
        tables=MappingProxyType(
            {table: _resolve(csv) for table, csv in payload["tables"].items()}
        ),
        fallback_templates=str(payload.get("fallback_templates", "")),
        build_command=str(payload.get("build_command", "")),
    )


def available_domain_names() -> list[str]:
    """Every domain directory that holds a manifest, sorted."""
    if not DOMAINS_DIR.exists():
        return []
    return sorted(
        entry.name for entry in DOMAINS_DIR.iterdir() if (entry / MANIFEST_NAME).exists()
    )


def available_domains() -> list[Domain]:
    return [get_domain(name) for name in available_domain_names()]


# The active domain is process-global state, which is deliberate: it is selected
# once, by the ``--domain`` flag or ``SQE_DOMAIN``, before any agent is built.
# Threading it through every constructor instead would put a domain argument on
# code that has no business knowing domains exist, and the two surfaces (CLI and
# API) each serve one domain per process.
_active: Domain | None = None


def active_domain() -> Domain:
    """The domain this process is serving, defaulting to ``SQE_DOMAIN`` or retail."""
    global _active
    if _active is None:
        _active = get_domain(os.getenv("SQE_DOMAIN") or DEFAULT_DOMAIN)
    return _active


def set_active_domain(name: str) -> Domain:
    """Select the domain for this process. Call before constructing the pipeline.

    Returns the resolved domain so a caller can report what it switched to.
    """
    global _active
    _active = get_domain(name)
    return _active

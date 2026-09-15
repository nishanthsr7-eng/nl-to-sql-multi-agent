"""Retrieve the tables and business metrics relevant to a question.

Two retrieval strategies share one interface:

- Vector mode: embed the semantic-layer documents once (cached to disk) and
  the incoming question each call, rank by cosine similarity.
- Keyword mode: score documents by naive word overlap with the question.

Vector mode requires an embeddings-capable provider -- OpenAI, or a local
OpenAI-compatible server with an embeddings model (Ollama serving
nomic-embed-text). Groq serves no embeddings endpoint. Which applies is
decided by LLMSettings.supports_embeddings, i.e. by whether an embedding
model is configured at all. Keyword mode is the always-available fallback and
is what a Groq-only deployment runs.

The disk cache is keyed by embedding model name, so switching providers does
not silently rank a 768-dimension question against 1536-dimension documents.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import numpy as np

from semantic_query_engine.core.config import PIPELINE, load_llm_settings
from semantic_query_engine.core.domains import active_domain
from semantic_query_engine.core.llm_client import build_embedding_client
from semantic_query_engine.core.logging import get_logger
from semantic_query_engine.domain.registry import GrainRegistry
from semantic_query_engine.semantic.layer import (
    SemanticLayer,
    build_metric_document,
    build_table_document,
    load_semantic_layer,
)

logger = get_logger(__name__)


@dataclass
class RetrievedContext:
    tables: list[dict[str, Any]]
    metrics: list[dict[str, Any]]
    formatted_context: str


def _owns_all(grains: GrainRegistry, table: str, columns: set[str]) -> bool:
    """True when ``table`` provides every column the metric formula names.

    The formula is tokenised loosely, so it also yields SQL keywords like SUM and
    CASE. Those are simply columns no table has, and a metric whose real columns
    are split across two tables matches neither -- which is the correct answer:
    no single table can compute it.

    A token counts as a real column when *any* table declares it, not when this
    one does. Testing against this table made the function a tautology: the
    filter and the predicate were the same call, so every token the table did
    not own was excluded from the check that was supposed to catch it, and
    `_owns_all` silently degraded to "owns at least one token". That let
    ``fact_inventory`` claim ``SUM(units_sold * price_unit)`` -- it has
    ``units_sold``, and ``price_unit`` was skipped precisely because it does
    not have it -- so retrieval picked the wrong fact whenever ranking put
    inventory above sales.
    """
    real = {c for c in columns if grains.owns_column(table, c)}
    return bool(real) and all(
        grains.owns_column(table, c) for c in columns if _looks_like_column(c, grains)
    )


def _looks_like_column(token: str, grains: GrainRegistry) -> bool:
    return grains.any_table_owns_column(token)


def _uses_level_measure(grains: GrainRegistry, table: str, columns: set[str]) -> bool:
    """True when the formula reads a column this table only holds as a level.

    A level measure is a snapshot or an average, not a quantity that survives
    being summed -- multiplying an average price by units does not produce the
    revenue that actually occurred.
    """
    grain = grains.grain(table)
    if grain is None:
        return False
    return bool(columns & set(grain.level_measures))


class SemanticRetriever:
    def __init__(self, layer: SemanticLayer | None = None):
        self.layer = layer or load_semantic_layer()
        self.tables = self.layer.tables
        self.metrics = self.layer.metrics
        # Built here rather than imported from domain.registry to avoid a cycle:
        # the registry module imports this package's layer loader.
        self._grains = GrainRegistry(self.layer.tables, self.layer.relationships)

        self.llm_settings = load_llm_settings()
        self.client = None
        if self.llm_settings.is_enabled and self.llm_settings.supports_embeddings:
            self.client = build_embedding_client(self.llm_settings)

        self.table_embeddings: list[np.ndarray] = []
        self.metric_embeddings: list[np.ndarray] = []
        self._vector_mode = False

        if self.client:
            try:
                self._initialize_embeddings()
                self._vector_mode = True
            except Exception as e:
                logger.warning(
                    "Embedding init failed (%s): %s. Falling back to keyword retrieval.",
                    type(e).__name__,
                    e,
                )

    def _initialize_embeddings(self) -> None:
        """Load from cache or compute missing embeddings, then persist the cache."""
        import json
        import os

        # Per domain: the cache key is the model plus the table name, and two
        # domains are free to have a table with the same name meaning different
        # things. A shared file would hand one domain the other's vectors.
        cache_path = active_domain().vector_cache_path
        cache: dict[str, list[float]] = {}
        if os.path.exists(cache_path):
            try:
                with open(cache_path, encoding="utf-8") as f:
                    cache = json.load(f)
            except Exception:
                pass

        cache_updated = False

        model_prefix = self.llm_settings.embedding_model

        for table in self.tables:
            doc = build_table_document(table)
            key = f"{model_prefix}:table_{table.get('table_name')}"
            if key in cache:
                emb = cache[key]
            else:
                emb = self._compute_embedding(doc)
                cache[key] = emb
                cache_updated = True
            self.table_embeddings.append(np.array(emb))

        for metric in self.metrics:
            doc = build_metric_document(metric)
            key = f"{model_prefix}:metric_{metric.get('metric_name')}"
            if key in cache:
                emb = cache[key]
            else:
                emb = self._compute_embedding(doc)
                cache[key] = emb
                cache_updated = True
            self.metric_embeddings.append(np.array(emb))

        if cache_updated:
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(cache, f)
            except Exception as e:
                logger.warning("Failed to write embedding cache: %s", e)

    def _compute_embedding(self, text: str) -> list[float]:
        if not self.client:
            return []
        response = self.client.embeddings.create(
            input=text, model=self.llm_settings.embedding_model
        )
        return response.data[0].embedding

    @staticmethod
    def _cosine_similarity(vec1: np.ndarray, vec2: np.ndarray) -> float:
        norm1 = np.linalg.norm(vec1)
        norm2 = np.linalg.norm(vec2)
        if norm1 == 0 or norm2 == 0:
            return 0.0
        return float(np.dot(vec1, vec2) / (norm1 * norm2))

    def retrieve(
        self,
        question: str,
        top_k_tables: int = PIPELINE.retrieval_top_k_tables,
        top_k_metrics: int = PIPELINE.retrieval_top_k_metrics,
    ) -> RetrievedContext:
        """Return the tables and metrics most relevant to ``question``."""
        if not self._vector_mode or not self.table_embeddings:
            return self._keyword_retrieve(question, top_k_tables, top_k_metrics)

        try:
            q_emb = np.array(self._compute_embedding(question))
        except Exception as e:
            logger.warning("Live embedding failed (%s). Using keyword fallback.", type(e).__name__)
            return self._keyword_retrieve(question, top_k_tables, top_k_metrics)

        scored_tables = []
        for table, t_emb in zip(self.tables, self.table_embeddings, strict=True):
            score = self._cosine_similarity(q_emb, t_emb)
            if any(term in table.get("table_name", "").lower() for term in question.lower().split()):
                score += 0.05
            scored_tables.append((score, table))
        scored_tables.sort(key=lambda x: x[0], reverse=True)
        ranked = [t for _, t in scored_tables]

        scored_metrics = []
        for metric, m_emb in zip(self.metrics, self.metric_embeddings, strict=True):
            score = self._cosine_similarity(q_emb, m_emb)
            if any(term in metric.get("metric_name", "").lower().split("_") for term in question.lower().split()):
                score += 0.05
            scored_metrics.append((score, metric))
        scored_metrics.sort(key=lambda x: x[0], reverse=True)
        selected_metrics = [m for _, m in scored_metrics[:top_k_metrics]]
        selected_tables = self._with_join_closure(ranked[:top_k_tables], ranked, selected_metrics)

        return RetrievedContext(
            tables=selected_tables,
            metrics=selected_metrics,
            formatted_context=self._format_context(selected_tables, selected_metrics),
        )

    def _keyword_retrieve(
        self, question: str, top_k_tables: int, top_k_metrics: int
    ) -> RetrievedContext:
        """Score tables and metrics by simple keyword overlap with the question."""
        q_lower = question.lower()

        def table_score(t: dict) -> int:
            name = t.get("table_name", "").lower()
            desc = t.get("description", "").lower()
            return sum(1 for word in q_lower.split() if word in name or word in desc)

        sorted_tables = sorted(self.tables, key=table_score, reverse=True)
        ranked = sorted_tables or list(self.tables)

        def metric_score(m: dict) -> int:
            fields = " ".join(
                [m.get("metric_name", ""), m.get("description", ""), m.get("definition", "")]
            ).lower()
            return sum(1 for word in q_lower.split() if word in fields)

        sorted_metrics = sorted(self.metrics, key=metric_score, reverse=True)
        selected_metrics = sorted_metrics[:top_k_metrics] if sorted_metrics else self.metrics[:top_k_metrics]
        scored_any = any(metric_score(m) for m in self.metrics)
        selected_tables = self._with_join_closure(
            ranked[:top_k_tables], ranked, selected_metrics if scored_any else []
        )

        return RetrievedContext(
            tables=selected_tables,
            metrics=selected_metrics,
            formatted_context=self._format_context(selected_tables, selected_metrics),
        )

    def _with_join_closure(
        self, selected: list[dict], ranked: list[dict], metrics: list[dict]
    ) -> list[dict]:
        """Add the tables the selected ones have to be joined to.

        Relevance ranking alone stopped working when Phase 4 normalised the
        schema. "Total revenue by region" scores ``dim_store`` highest, because
        that is where the word *region* lives -- but ``dim_store`` holds no
        measure, so the retrieved context describes a table that cannot answer
        the question and omits the one that can.

        Two corrections, both driven by the semantic layer rather than by a list
        of table names here:

        * The table that can actually compute the top-ranked metric is pulled
          in. "Total revenue" ranks ``dim_store`` first on the word *region*, but
          the certified formula names ``units_sold`` and ``price_unit``, and only
          one table has both -- the metric identifies the fact far more reliably
          than the question's wording does.
        * Failing that, if nothing selected carries an additive measure, the
          best-ranked fact is pulled in: a question about a quantity needs the
          table holding it.
        * Every declared join partner of a selected fact is added, so the model
          is shown the dimensions it will have to join to rather than being left
          to guess that ``region`` is reachable at all.

        The result stays bounded: dimensions are only pulled in through a fact
        that was already selected, never transitively.
        """
        by_name = {t.get("table_name", "").lower(): t for t in self.tables}
        chosen = {t.get("table_name", "").lower(): t for t in selected}

        def is_fact(name: str) -> bool:
            grain = self._grains.grain(name)
            return bool(grain and grain.additive_measures)

        for metric in metrics[:1]:
            required = {
                column.lower()
                for column in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", metric.get("definition", ""))
            }
            candidates = [
                t
                for t in ranked
                if is_fact(t.get("table_name", "").lower())
                and _owns_all(self._grains, t.get("table_name", "").lower(), required)
            ]
            # More than one table can hold the metric's columns -- both the daily
            # fact and the weekly aggregate carry units_sold and price_unit. The
            # weekly one carries an *average* price, declared as a level measure,
            # so revenue computed from it is not revenue. Prefer a table that
            # holds the formula's columns exactly.
            owner = next(
                (
                    t
                    for t in candidates
                    if not _uses_level_measure(self._grains, t.get("table_name", "").lower(), required)
                ),
                candidates[0] if candidates else None,
            )
            if owner is not None:
                chosen.setdefault(owner.get("table_name", "").lower(), owner)

        if not any(is_fact(name) for name in chosen):
            best_fact = next(
                (t for t in ranked if is_fact(t.get("table_name", "").lower())), None
            )
            if best_fact is not None:
                chosen[best_fact.get("table_name", "").lower()] = best_fact

        for name in list(chosen):
            if not is_fact(name):
                continue
            for partner in by_name:
                if partner not in chosen and self._grains.has_any_path(name, partner):
                    chosen[partner] = by_name[partner]

        # Ranked order, so the most relevant table still leads the prompt.
        order = {t.get("table_name", "").lower(): i for i, t in enumerate(ranked)}
        return sorted(chosen.values(), key=lambda t: order.get(t.get("table_name", "").lower(), 99))

    @staticmethod
    def _format_context(tables: list[dict], metrics: list[dict]) -> str:
        lines = ["# Relevant Schema Context"]
        for table in tables:
            lines.append(f"\n## Table: {table['table_name']}")
            lines.append(table.get("description", ""))
            lines.append("Columns:")
            for col in table.get("columns", []):
                lines.append(f"- {col['name']} ({col.get('type', 'UNKNOWN')}): {col.get('description', '')}")

        if metrics:
            lines.append("\n# Business Metrics")
            for metric in metrics:
                lines.append(
                    f"- {metric['metric_name']}: {metric.get('definition', '')} -- {metric.get('description', '')}"
                )

        return "\n".join(lines)

"""Retrieve the tables and business metrics relevant to a question.

Two retrieval strategies share one interface:

- Vector mode: embed the semantic-layer documents once (cached to disk) and
  the incoming question each call, rank by cosine similarity.
- Keyword mode: score documents by naive word overlap with the question.

Vector mode requires an embeddings-capable provider (OpenAI; Groq does not
serve embeddings -- see LLMSettings.supports_embeddings). Keyword mode is the
always-available fallback and is what a Groq-only deployment runs today.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from semantic_query_engine.core.config import PIPELINE, VECTOR_CACHE_PATH, load_llm_settings
from semantic_query_engine.core.llm_client import build_client
from semantic_query_engine.core.logging import get_logger
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


class SemanticRetriever:
    def __init__(self, layer: SemanticLayer | None = None):
        self.layer = layer or load_semantic_layer()
        self.tables = self.layer.tables
        self.metrics = self.layer.metrics

        self.llm_settings = load_llm_settings()
        self.client = None
        if self.llm_settings.is_enabled and self.llm_settings.supports_embeddings:
            self.client = build_client(self.llm_settings)

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

        cache: dict[str, list[float]] = {}
        if os.path.exists(VECTOR_CACHE_PATH):
            try:
                with open(VECTOR_CACHE_PATH, encoding="utf-8") as f:
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
                VECTOR_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
                with open(VECTOR_CACHE_PATH, "w", encoding="utf-8") as f:
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
        selected_tables = [t for _, t in scored_tables[:top_k_tables]]

        scored_metrics = []
        for metric, m_emb in zip(self.metrics, self.metric_embeddings, strict=True):
            score = self._cosine_similarity(q_emb, m_emb)
            if any(term in metric.get("metric_name", "").lower().split("_") for term in question.lower().split()):
                score += 0.05
            scored_metrics.append((score, metric))
        scored_metrics.sort(key=lambda x: x[0], reverse=True)
        selected_metrics = [m for _, m in scored_metrics[:top_k_metrics]]

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
        selected_tables = sorted_tables[:top_k_tables] if sorted_tables else self.tables[:top_k_tables]

        def metric_score(m: dict) -> int:
            fields = " ".join(
                [m.get("metric_name", ""), m.get("description", ""), m.get("definition", "")]
            ).lower()
            return sum(1 for word in q_lower.split() if word in fields)

        sorted_metrics = sorted(self.metrics, key=metric_score, reverse=True)
        selected_metrics = sorted_metrics[:top_k_metrics] if sorted_metrics else self.metrics[:top_k_metrics]

        return RetrievedContext(
            tables=selected_tables,
            metrics=selected_metrics,
            formatted_context=self._format_context(selected_tables, selected_metrics),
        )

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

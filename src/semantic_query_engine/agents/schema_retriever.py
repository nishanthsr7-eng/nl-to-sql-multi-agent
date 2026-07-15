"""Schema retriever agent -- thin pipeline wrapper around the semantic retriever."""

from __future__ import annotations

from semantic_query_engine.semantic.retriever import RetrievedContext, SemanticRetriever


class SchemaRetrieverAgent:
    def __init__(self, retriever: SemanticRetriever | None = None):
        self.retriever = retriever or SemanticRetriever()

    def run(self, question: str) -> RetrievedContext:
        return self.retriever.retrieve(question)

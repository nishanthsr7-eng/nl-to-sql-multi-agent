"""Single place that turns :class:`LLMSettings` into an OpenAI-compatible client.

Both Groq and OpenAI are consumed through the ``openai`` SDK — Groq is
OpenAI-API-compatible and only needs a different ``base_url``. Building the
client here means the three agents that call an LLM (SQL generation, repair,
synthesis) never branch on provider themselves.
"""

from __future__ import annotations

from typing import Any, Protocol

from semantic_query_engine.core.config import LLMSettings


class ChatClient(Protocol):
    """The minimal shape every LLM-calling agent depends on.

    ``build_client`` returns a real ``openai.OpenAI`` instance, which satisfies
    this structurally. Tests substitute a small fake that returns canned JSON
    instead -- see tests/unit/fakes.py -- so the LLM-primary code paths can be
    unit tested without hitting the network or requiring an API key.
    """

    chat: Any
    embeddings: Any


def build_client(settings: LLMSettings) -> Any:
    """Construct an OpenAI SDK client configured for the resolved provider.

    Returns ``Any`` rather than ``ChatClient``: the real ``openai.OpenAI`` instance
    satisfies the protocol structurally (``.chat``, ``.embeddings``), but its
    attributes are read-only properties, which mypy won't accept as satisfying a
    (writable-by-default) Protocol attribute -- not worth fighting for what is,
    at the call sites, already typed as ``ChatClient``.

    Raises if ``settings.is_enabled`` is False — callers are expected to check
    that first (agents fall back to deterministic behavior instead of calling
    this at all).
    """
    from openai import OpenAI

    if not settings.is_enabled:
        raise RuntimeError("No LLM API key configured; cannot build a client.")

    return OpenAI(api_key=settings.api_key, base_url=settings.base_url)


def build_embedding_client(settings: LLMSettings) -> Any:
    """The client the vector-retrieval path embeds through.

    Separate from :func:`build_client` because embeddings and generation need not
    come from the same provider: a hosted generator with no embeddings endpoint
    (Groq) plus a local embedder is a working combination, and it is the one that
    keeps ``sqe matrix`` rows comparable -- otherwise the rows differ in retrieval
    as well as in the generator, and the comparison silently measures two changes.

    When neither override is set this returns a client configured exactly like
    ``build_client``'s, so the ordinary single-provider case is unaffected.
    """
    from openai import OpenAI

    api_key, base_url = settings.embedding_endpoint
    if not api_key:
        raise RuntimeError("No API key configured for embeddings; cannot build a client.")

    return OpenAI(api_key=api_key, base_url=base_url)

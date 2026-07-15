"""A minimal fake LLM client for unit-testing the LLM-primary code paths.

Structurally satisfies :class:`semantic_query_engine.core.llm_client.ChatClient` (just
``.chat.completions.create(...)``) without any network access, so agent prompt
assembly and Pydantic response parsing can be exercised deterministically.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass
class _FakeMessage:
    content: str


@dataclass
class _FakeChoice:
    message: _FakeMessage


@dataclass
class _FakeResponse:
    choices: list[_FakeChoice]


class _FakeCompletions:
    def __init__(self, payloads: list[dict[str, Any]]):
        self._payloads = list(payloads)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _FakeResponse:
        self.calls.append(kwargs)
        payload = self._payloads.pop(0) if len(self._payloads) > 1 else self._payloads[0]
        return _FakeResponse(choices=[_FakeChoice(message=_FakeMessage(content=json.dumps(payload)))])


@dataclass
class _FakeChat:
    completions: _FakeCompletions


class FakeChatClient:
    """Returns each dict in ``payloads`` in turn (repeating the last) as a JSON response.

    Usage::

        client = FakeChatClient({"sql": "SELECT 1"})
        agent = SQLGeneratorAgent(client_factory=lambda settings: client)
    """

    chat: _FakeChat

    def __init__(self, *payloads: dict[str, Any]):
        completions = _FakeCompletions(list(payloads) or [{}])
        self.chat = _FakeChat(completions=completions)

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.chat.completions.calls


class RaisingChatClient:
    """A fake client whose every call raises, to test the fallback path is used."""

    def __init__(self, exc: Exception | None = None):
        self._exc = exc or RuntimeError("simulated provider outage")
        self.chat = _FakeChat(completions=_RaisingCompletions(self._exc))


@dataclass
class _RaisingCompletions:
    exc: Exception

    def create(self, **_kwargs: Any):
        raise self.exc

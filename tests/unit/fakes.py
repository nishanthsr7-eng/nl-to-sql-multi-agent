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
class _FakeUsage:
    prompt_tokens: int
    completion_tokens: int


@dataclass
class _FakeResponse:
    choices: list[_FakeChoice]
    # Absent by default, exactly as it is from a provider that does not report
    # it: token accounting has to survive that rather than raise mid-pipeline.
    usage: _FakeUsage | None = None


class _FakeCompletions:
    def __init__(self, payloads: list[dict[str, Any]], usage: _FakeUsage | None = None):
        self._payloads = list(payloads)
        self._usage = usage
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _FakeResponse:
        self.calls.append(kwargs)
        payload = self._payloads.pop(0) if len(self._payloads) > 1 else self._payloads[0]
        return _FakeResponse(
            choices=[_FakeChoice(message=_FakeMessage(content=json.dumps(payload)))],
            usage=self._usage,
        )


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

    def __init__(
        self,
        *payloads: dict[str, Any],
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ):
        usage = (
            _FakeUsage(prompt_tokens or 0, completion_tokens or 0)
            if prompt_tokens is not None or completion_tokens is not None
            else None
        )
        completions = _FakeCompletions(list(payloads) or [{}], usage=usage)
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

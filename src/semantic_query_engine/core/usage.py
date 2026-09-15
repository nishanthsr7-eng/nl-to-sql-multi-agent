"""Token and cost accounting for one pipeline run.

The ladder in ROADMAP.md Phase 3 has a cost-per-query column, and nothing in the
system could fill it: the three LLM call sites read ``response.choices`` and threw
the rest of the provider's payload away, including ``response.usage``. This module
is the missing half.

Two decisions here are load-bearing, and both are about not reporting a number the
system does not actually know:

* **An unpriced model costs ``None``, not zero.** Every provider returns token
  counts; only some models have a price in :data:`PRICING`. If an unknown model
  defaulted to a price of zero, a ladder run against it would print "$0.0000 per
  query" -- a claim that is both false and flattering, which is the worst
  combination a measurement can have. ``None`` propagates through addition and
  renders as "n/a", so an unpriced run reports tokens and declines to report cost.
  A model served from a loopback endpoint is priced at zero *explicitly*, because
  for it zero is the truth -- and that is decided by the endpoint, not the model
  name, so renaming the local model cannot start charging for it.
* **Usage accumulates by explicit return value, not a shared counter.** Agents get
  their inputs as arguments and hand back what they did; the orchestrator adds the
  parts up per run. A module-level accumulator would be simpler and would silently
  cross-attribute tokens between concurrent API requests, which the per-request
  cursor work in Phase 1 went to some trouble to avoid elsewhere.

Prices are per *million* tokens, in USD, as published at the time of writing. They
are a recorded assumption, not a live feed -- an eval run stores the cost it
computed, so a later price change cannot retroactively rewrite a published number.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, NamedTuple


class ModelPrice(NamedTuple):
    """USD per one million tokens, input and output priced separately."""

    input_per_mtok: float
    output_per_mtok: float


# Keyed by the exact model id passed to the provider. Matching is exact and then
# by prefix, so a dated snapshot (``gpt-4o-mini-2024-07-18``) inherits the base
# model's price rather than falling through to "unpriced".
PRICING: dict[str, ModelPrice] = {
    # OpenAI -- the recommended path for a ladder run (see the Phase 3 outcome note).
    "gpt-4o-mini": ModelPrice(0.15, 0.60),
    "gpt-4o": ModelPrice(2.50, 10.00),
    "gpt-4.1-mini": ModelPrice(0.40, 1.60),
    "gpt-4.1": ModelPrice(2.00, 8.00),
    # Groq. Prefixed ids, because that is exactly what the provider is sent --
    # "openai/gpt-oss-120b" is a Groq-hosted model and must not be confused with
    # OpenAI's own catalogue above.
    #
    # The two Llama entries below are kept although GroqCloud no longer serves
    # them: the 2026-09 matrix could not run against them for that reason, and
    # deleting the prices would make the older runs that did use them unpriceable
    # on re-read. A price is a recorded assumption; removing one rewrites history.
    "llama-3.3-70b-versatile": ModelPrice(0.59, 0.79),
    "llama-3.1-8b-instant": ModelPrice(0.05, 0.08),
    # As published at https://console.groq.com/docs/models, read 2026-09-21.
    "openai/gpt-oss-120b": ModelPrice(0.15, 0.60),
    "openai/gpt-oss-20b": ModelPrice(0.075, 0.30),
    "qwen/qwen3.8-27b": ModelPrice(0.80, 4.00),
}

# Hosts that bill nothing because nothing leaves the machine. A model served from
# one of these is priced at zero *because that is true*, not as a stand-in for
# "unknown" -- which is why it is decided by endpoint rather than by model name:
# the local Ollama model is called ``sqe-coder`` (see evals/Modelfile.local), and
# a name-based rule would silently start charging for it the day it is renamed.
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal")


def is_local_endpoint(base_url: str | None) -> bool:
    if not base_url:
        return False
    return any(host in base_url for host in _LOCAL_HOSTS)


def price_for(model: str) -> ModelPrice | None:
    """The price of ``model``, or None when it is not in the table.

    Prefix matching exists for dated snapshots. It is deliberately one-directional
    -- a listed id is a prefix of the requested one, never the reverse -- so that
    ``gpt-4o`` cannot pick up the price of some future ``gpt-4o-nano``.
    """
    if model in PRICING:
        return PRICING[model]
    candidates = [key for key in PRICING if model.startswith(key)]
    if not candidates:
        return None
    return PRICING[max(candidates, key=len)]


@dataclass(frozen=True)
class TokenUsage:
    """What one run (or one call, or a whole suite) spent.

    ``cost_usd`` is ``None`` for an unpriced model and stays ``None`` through any
    sum involving it: a total that silently dropped the unpriced half of a run
    would understate the cost without saying so.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    cost_usd: float | None = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __add__(self, other: TokenUsage) -> TokenUsage:
        if self.cost_usd is None or other.cost_usd is None:
            cost: float | None = None
        else:
            cost = self.cost_usd + other.cost_usd
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            calls=self.calls + other.calls,
            cost_usd=cost,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "calls": self.calls,
            "cost_usd": None if self.cost_usd is None else round(self.cost_usd, 6),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TokenUsage:
        return cls(
            prompt_tokens=int(raw.get("prompt_tokens", 0)),
            completion_tokens=int(raw.get("completion_tokens", 0)),
            calls=int(raw.get("calls", 0)),
            cost_usd=raw.get("cost_usd", 0.0),
        )


# The identity for summing usage. A run that never called an LLM -- the
# deterministic fallback path, or an ablation rung with generation switched off --
# has a real, known cost of zero, which is why this is not ``None``.
NO_USAGE = TokenUsage()


def usage_from_response(
    response: Any, model: str, base_url: str | None = None
) -> TokenUsage:
    """Read token counts off a provider response and price them.

    Tolerant by construction: a response with no ``usage`` block, or a fake client
    in a test that returns only ``choices``, yields one call at zero tokens rather
    than raising. Accounting must never be the thing that fails a query -- a run
    that answered correctly and could not be costed is still a correct run, and
    the missing tokens show up as an implausibly cheap total rather than an
    exception in the middle of the pipeline.
    """
    raw = getattr(response, "usage", None)
    prompt = int(getattr(raw, "prompt_tokens", 0) or 0)
    completion = int(getattr(raw, "completion_tokens", 0) or 0)

    cost: float | None
    if is_local_endpoint(base_url):
        return TokenUsage(
            prompt_tokens=prompt, completion_tokens=completion, calls=1, cost_usd=0.0
        )

    price = price_for(model)
    if price is None:
        cost = None
    else:
        cost = (
            prompt * price.input_per_mtok + completion * price.output_per_mtok
        ) / 1_000_000

    return TokenUsage(
        prompt_tokens=prompt, completion_tokens=completion, calls=1, cost_usd=cost
    )


def format_cost(cost_usd: float | None) -> str:
    """Render a cost for a human. ``None`` is "n/a", never "$0.00"."""
    if cost_usd is None:
        return "n/a"
    if cost_usd < 0.01:
        return f"${cost_usd:.6f}"
    return f"${cost_usd:.4f}"


__all__ = [
    "NO_USAGE",
    "PRICING",
    "ModelPrice",
    "TokenUsage",
    "format_cost",
    "is_local_endpoint",
    "price_for",
    "usage_from_response",
]

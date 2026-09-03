"""
SpeedTrader AI — Provider-neutral LLM interface

Deliberately contains NO vendor SDK import. Concrete providers live beside it
(anthropic_provider.py, deterministic.py); everything upstream depends only on
this file, so swapping or adding a provider never touches agent code.

--------------------------------------------------------------------------------
THE ONE RULE THIS LAYER ENFORCES
--------------------------------------------------------------------------------
A provider returns TEXT. It never returns a decision, an order, a size or an
authorization, and no provider is given a broker handle. Whatever a model emits
is parsed and validated downstream against a schema whose most permissive
outcome is "do not interfere".

That is what makes model choice a reasoning-quality decision rather than a
safety one: a smarter model reasons better; it does not gain authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable


class LLMError(RuntimeError):
    """Base. Every failure here must fail CLOSED at the call site."""


class LLMUnavailable(LLMError):
    """No provider, no credentials, or the endpoint could not be reached."""


class LLMTimeout(LLMError):
    """The model did not answer inside the budget."""


class LLMMalformedOutput(LLMError):
    """The model answered, but not in the required shape."""


@dataclass(frozen=True)
class LLMRequest:
    """One bounded call. Every limit is explicit; none is left to a default
    buried in a vendor SDK."""
    system: str
    prompt: str
    max_tokens: int = 1024
    timeout_seconds: float = 30.0
    #: JSON Schema the reply must satisfy. Providers that support native
    #: structured output enforce it server-side; the rest validate locally.
    schema: Mapping[str, Any] | None = None
    temperature: float | None = None


@dataclass(frozen=True)
class LLMResponse:
    """What came back, plus enough provenance to reproduce the call.

    model and provider are recorded on the decision: an audit trail that says
    "the AI agreed" without saying which model, at what version, under what
    prompt, is not an audit trail.
    """
    text: str
    provider: str
    model: str
    prompt_version: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    stop_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def provenance(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "stop_reason": self.stop_reason,
        }


@runtime_checkable
class LLMProvider(Protocol):
    """The whole surface. Narrow on purpose: a provider that could do more than
    return text would be a provider that could do more than reason."""

    name: str

    def complete(self, request: LLMRequest) -> LLMResponse:
        ...

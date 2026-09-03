"""
SpeedTrader AI — Anthropic provider

The only file in the project that imports the Anthropic SDK. Everything upstream
depends on `base.LLMProvider`, so this can be swapped for an OpenAI, DeepSeek,
OpenRouter or local-Qwen provider without touching agent code.

Credentials come from the environment (ANTHROPIC_API_KEY) via the SDK's own
resolution. They are never read into an attribute, logged, or put in an
exception message.

Every failure — timeout, rate limit, transport, refusal, malformed reply — is
translated into an LLMError subclass. The caller treats all of them identically:
the AI abstains and the deterministic pipeline proceeds untouched. There is no
failure mode in which an unreachable model makes a trade more likely.
"""

from __future__ import annotations

import json
from typing import Any

from .base import (
    LLMMalformedOutput,
    LLMRequest,
    LLMResponse,
    LLMTimeout,
    LLMUnavailable,
)

#: Current model IDs carry no date suffix. A date-suffixed variant recalled from
#: training data (e.g. "claude-haiku-4-5-20251001") is not a valid id.
DEFAULT_MODEL = "claude-opus-5"

SUPPORTED_MODELS = (
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-haiku-4-5",
)


class AnthropicProvider:
    """Claude via the official SDK, with an explicit per-call timeout."""

    name = "anthropic"

    def __init__(self, model: str = DEFAULT_MODEL, *, client: Any = None):
        self.model = model
        self._client = client          # injectable for tests; never a real key

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import anthropic
        except ImportError as e:
            raise LLMUnavailable(
                "the 'anthropic' package is required: pip install anthropic"
            ) from e
        try:
            # Resolves ANTHROPIC_API_KEY / auth profile itself. Nothing is read
            # into this object, so nothing can leak from it.
            self._client = anthropic.Anthropic()
        except Exception as e:
            raise LLMUnavailable(f"could not construct Anthropic client: {e}") from e
        return self._client

    def complete(self, request: LLMRequest) -> LLMResponse:
        client = self._get_client()

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_tokens,
            "system": request.system,
            "messages": [{"role": "user", "content": request.prompt}],
        }
        if request.schema is not None:
            # Constrain the reply server-side where the model supports it, so a
            # shape violation is prevented rather than merely detected.
            kwargs["output_config"] = {
                "format": {
                    "type": "json_schema",
                    "schema": dict(request.schema),
                }
            }

        try:
            # Python SDK timeouts are in SECONDS.
            response = client.with_options(
                timeout=request.timeout_seconds
            ).messages.create(**kwargs)
        except Exception as e:
            raise _translate(e) from e

        # A refusal is not an answer. Treat it as an abstention upstream rather
        # than parsing whatever text came back.
        if getattr(response, "stop_reason", None) == "refusal":
            raise LLMMalformedOutput(
                "the model declined to answer (stop_reason=refusal); abstaining"
            )

        text = _text_of(response)
        if not text.strip():
            raise LLMMalformedOutput("the model returned no text")

        usage = getattr(response, "usage", None)
        return LLMResponse(
            text=text,
            provider=self.name,
            model=getattr(response, "model", self.model),
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            stop_reason=getattr(response, "stop_reason", None),
        )


def _text_of(response: Any) -> str:
    """Concatenate text blocks. content is a list of typed blocks, and only
    those of type 'text' carry a .text attribute."""
    parts: list[str] = []
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) == "text":
            parts.append(str(getattr(block, "text", "")))
    return "\n".join(parts)


def _translate(exc: Exception) -> Exception:
    """Map SDK exceptions onto the neutral error family.

    Matched by class NAME rather than by importing the SDK's exception classes,
    so this stays correct if the SDK is absent and never becomes a hard import.
    """
    name = type(exc).__name__
    if name in ("APITimeoutError", "TimeoutError"):
        return LLMTimeout(f"model call timed out: {exc}")
    if name in ("RateLimitError", "APIConnectionError", "InternalServerError",
                "APIStatusError", "AuthenticationError", "PermissionDeniedError",
                "NotFoundError", "BadRequestError"):
        return LLMUnavailable(f"{name}: {exc}")
    return LLMUnavailable(f"unexpected {name}: {exc}")


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object from model output, tolerating a fenced code block.

    Tolerant about packaging, strict about substance: anything that is not a
    JSON object raises, because a downstream parser guessing at half-formed
    output is how a malformed reply becomes a decision.
    """
    raw = text.strip()
    if raw.startswith("```"):
        lines = [ln for ln in raw.splitlines() if not ln.strip().startswith("```")]
        raw = "\n".join(lines).strip()
    try:
        parsed = json.loads(raw)
    except ValueError as e:
        raise LLMMalformedOutput(f"model output was not valid JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise LLMMalformedOutput(
            f"expected a JSON object, got {type(parsed).__name__}"
        )
    return parsed

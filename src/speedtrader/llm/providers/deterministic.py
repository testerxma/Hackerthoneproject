"""
SpeedTrader AI — Deterministic (no-credential) provider

Not a mock and not a stub that fakes intelligence. It is the provider used when
no API credentials are configured, and it exists for one reason:

    THE DEMO MUST NEVER DEPEND ON A THIRD PARTY BEING REACHABLE.

A judge cloning this repository with no API key still gets a complete, running,
end-to-end decision cycle. What they do NOT get is invented reasoning: this
provider always ABSTAINS, which is the honest answer when no model was consulted,
and abstention is defined downstream to change nothing.

That is the safe default by construction. The AI layer's maximum authority is to
veto; a provider that cannot veto therefore cannot alter a single trade.
"""

from __future__ import annotations

import json

from .base import LLMRequest, LLMResponse

ABSTAIN_REASON = (
    "No LLM provider is configured, so no model was consulted. Abstaining rather "
    "than manufacturing a rationale. The deterministic pipeline is unaffected."
)


class DeterministicProvider:
    """Always abstains. Deterministic, offline, instant."""

    name = "deterministic"
    model = "none"

    def complete(self, request: LLMRequest) -> LLMResponse:
        payload = {
            "verdict": "ABSTAIN",
            "confidence": 0.0,
            "reasoning": ABSTAIN_REASON,
            "concerns": [],
            "evidence": [],
        }
        return LLMResponse(
            text=json.dumps(payload),
            provider=self.name,
            model=self.model,
            prompt_version="n/a",
            stop_reason="deterministic_abstain",
        )

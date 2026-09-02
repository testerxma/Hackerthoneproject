"""
SpeedTrader AI — Quant Core
Spec: §15 Role of the Quant Core, §16 Quant Core Pipeline

Thin orchestration only. Its output is "this opportunity deserves investigation",
never "execute immediately".

MUST NOT and DOES NOT contain: S07 formulas (quant/strategies/s07.py), risk logic
(risk/engine.py), position sizing (risk/measures.py), execution authorization,
Alpaca calls, LLM calls, or writes to risk configuration.

Mirrors the selection loop at source L1958-1975: evaluate each enabled strategy,
keep the highest combined_priority. With one strategy enabled the ranking is
inert — and combined_priority is degenerate in R units anyway (see
expected_value.py deviation 5), so ranking must not be relied on until that is
resolved.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from ..data.schemas import CandidateSignal, MarketSnapshot
from .candidate import CandidateBuilder
from .expected_value import StrategyStatsLike
from .strategies.base import Strategy


@dataclass(frozen=True)
class QuantResult:
    """A candidate, or an explicit reason there isn't one (§75 no-trade memory)."""
    ok: bool
    candidate: CandidateSignal | None = None
    reason: str = "ok"
    code: str = "no_signal"
    evaluated: dict = None  # type: ignore[assignment]

    def __bool__(self) -> bool:
        return self.ok


class QuantCore:
    def __init__(
        self,
        strategies: Sequence[Strategy],
        execution_config: Mapping[str, Any],
    ):
        # Raises EVCostNotConfigured when no cost policy is configured, so a
        # misconfigured system fails at startup rather than silently producing
        # nothing for a whole session.
        self.builder = CandidateBuilder(execution_config)
        self.strategies = list(strategies)

    def run(
        self,
        snapshot: MarketSnapshot,
        *,
        stats_by_strategy: Mapping[str, StrategyStatsLike] | None = None,
        now: datetime | None = None,
    ) -> QuantResult:
        if not self.strategies:
            return QuantResult(False, reason="no strategies enabled",
                               code="no_strategies", evaluated={})

        stats_by_strategy = stats_by_strategy or {}
        evaluated: dict[str, str] = {}
        best: CandidateSignal | None = None

        for strat in self.strategies:
            result = strat.evaluate(snapshot)
            evaluated[strat.id] = result.reason
            if not result.ok or result.output is None:
                continue
            candidate = self.builder.build(
                result.output, snapshot,
                stats=stats_by_strategy.get(strat.id), now=now,
            )
            if best is None or candidate.combined_priority > best.combined_priority:
                best = candidate

        if best is None:
            return QuantResult(
                False,
                reason="; ".join(f"{k}: {v}" for k, v in evaluated.items()),
                code="no_signal",
                evaluated=evaluated,
            )
        return QuantResult(True, candidate=best, reason="candidate produced",
                           code="candidate", evaluated=evaluated)

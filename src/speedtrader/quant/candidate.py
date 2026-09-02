"""
SpeedTrader AI — Candidate Signal Builder
Spec: §18 Candidate Signal, §19 Why Candidate Signals Exist

    Candidate Signal != Order

This is the boundary between the quantitative system and everything downstream.
A CandidateSignal says "this opportunity deserves investigation". It carries no
quantity, no risk amount, no approval, and no authorization token. Sizing belongs
to risk/measures.size_position; authorization belongs to the risk engine alone.

IMPORT BOUNDARY (enforced by test):
    quant must not import risk.*, execution.*, alpaca.* or llm.*
Strategy statistics arrive through the StrategyStatsLike Protocol in
expected_value.py, so risk.state.StrategyStats can be passed without an import.

FAIL CLOSED ON MISSING COST CONFIGURATION.
The constructor raises so a misconfigured system fails at startup rather than
running quietly and producing nothing. build() guards again in case the
constructor is bypassed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from ..common.clock import expires_at, utcnow
from ..common.ids import IdKind, new_id
from ..data.schemas import CandidateSignal, MarketSnapshot, StrategyVote
from .cost_policy import CostPolicy, EVCostNotConfigured, cost_policy_from_config
from .expected_value import StrategyStatsLike, compute_ev
from .scoring import score_signal
from .strategies.base import StrategyOutput

DEFAULT_SIGNAL_TTL_SECONDS = 30.0

#: S07 emits "S07"; configs/strategy_config.yaml keys it "S7"; MQL5 uses
#: stratIdx 6 and displays "S" + (stratIdx+1) == "S7" (source L485).
STRATEGY_ID_TO_CONFIG_KEY = {"S07": "S7"}
STRATEGY_ID_TO_MQL5_INDEX = {"S07": 6}


class CandidateBuilder:
    """Assembles a CandidateSignal from a StrategyOutput and its snapshot."""

    def __init__(self, execution_config: Mapping[str, Any]):
        # Parse the cost policy eagerly. A misconfigured system then fails at
        # startup rather than running quietly and producing nothing for a session.
        self._default_policy: CostPolicy = cost_policy_from_config(execution_config)
        self.execution_config = execution_config
        self.ttl_seconds = float(
            execution_config.get("signal_ttl_seconds", DEFAULT_SIGNAL_TTL_SECONDS)
        )

    def build(
        self,
        output: StrategyOutput,
        snapshot: MarketSnapshot,
        *,
        stats: StrategyStatsLike | None = None,
        now: datetime | None = None,
    ) -> CandidateSignal:
        # Second guard: the constructor may have been bypassed. Re-parsing also
        # resolves any per-symbol override for this particular snapshot.
        policy = cost_policy_from_config(self.execution_config, symbol=snapshot.symbol)

        now = now or utcnow()
        mql5_index = STRATEGY_ID_TO_MQL5_INDEX.get(output.strategy_id, 0)

        # --- deterministic geometry --------------------------------
        stop_distance = abs(output.entry - output.stop_loss)
        if stop_distance <= 0:
            raise ValueError(
                f"{output.strategy_id}: zero stop distance "
                f"(entry={output.entry}, stop={output.stop_loss})"
            )
        reward_risk = abs(output.take_profit - output.entry) / stop_distance

        # --- scoring (L874-880 subset) then EV (L794-813) -----------
        score = score_signal(output, snapshot, mql5_strategy_index=mql5_index)
        ev = compute_ev(
            entry=output.entry,
            stop_loss=output.stop_loss,
            take_profit=output.take_profit,
            stop_distance=stop_distance,
            total_score=score.total_score,
            snapshot=snapshot,
            cost_policy=policy,
            direction=output.direction.value,
            stats=stats,
        )

        return CandidateSignal(
            signal_id=new_id(IdKind.SIGNAL),
            snapshot_id=snapshot.snapshot_id,
            symbol=snapshot.symbol,
            direction=output.direction,
            strategy_id=output.strategy_id,
            entry=output.entry,
            stop_loss=output.stop_loss,
            take_profit=output.take_profit,
            stop_distance=stop_distance,
            reward_risk=reward_risk,
            atr_at_signal=snapshot.features.atr,
            base_score=score.base_score,
            bonus=score.bonus,
            total_score=score.total_score,
            score_breakdown=score.breakdown,
            expected_value=ev.expected_value,
            ev_is_bootstrap=ev.is_bootstrap,
            combined_priority=ev.combined_priority,
            strategy_votes=[
                StrategyVote(
                    strategy_id=output.strategy_id,
                    direction=output.direction,
                    base_score=output.base_score,
                    # One strategy is enabled. This is NOT a multi-strategy consensus.
                    notes=f"sole enabled strategy; {output.source_reference}",
                )
            ],
            regime=snapshot.regime,
            created_at=now,
            expires_at=expires_at(now, self.ttl_seconds),
        )

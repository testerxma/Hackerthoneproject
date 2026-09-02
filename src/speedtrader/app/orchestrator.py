"""
SpeedTrader AI — Deterministic Orchestrator
Spec: §93 Orchestrator, §94 Orchestrator Is Not A Broker, §111 Failure Policy

    MarketSnapshot -> QuantCore -> CandidateSignal -> RiskEngine -> DecisionLog -> Store

WIRING ONLY. This module contains no S07 formula, no scoring or EV arithmetic, no
position sizing, no risk rule, no broker call, no LLM call and no agent logic. It
calls components that already exist and records what they returned.

AUTHORITY: Step 4 introduces none. The orchestrator cannot authorise execution
because no execution path exists, and it holds no credential, no minting function
and no import from execution/, alpaca/ (trading) or llm/.

STATE MAPPING — chosen deliberately, since Step 4 stops mid-pipeline:

    RISK_CHECK   the gate returned PASS or REDUCE and the pipeline stops there.
                 NOT "authorised": no authorisation exists yet, and reusing a
                 later state would misrepresent how far the decision travelled.
    REJECTED     any layer declined, with rejection_stage naming which one.
    FAILED       a component raised. Distinct from a rejection: a rejection is the
                 system working, a failure is the system broken.

THE FIVE OUTCOMES MUST STAY DISTINGUISHABLE (§119):

    configuration failure   raises before any decision exists; NOTHING is written
    normal no-signal        DecisionLog, REJECTED_BY_QUANT, candidate is None
    candidate build failure DecisionLog, FAILED, reason recorded
    risk rejection          DecisionLog, REJECTED_BY_RISK_ENGINE, gate attached
    infrastructure failure  raises out of run(); the store could not record it

A decision that cannot be recorded must not be reported as complete. If the store
raises, the failure propagates: silently continuing would mean acting on a decision
with no audit trail.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..common.clock import utcnow
from ..common.ids import IdKind, new_id
from ..data.schemas import (
    DecisionLog,
    MarketSnapshot,
    RejectionStage,
    RiskGateVerdict,
    SystemState,
)
from ..quant.engine import QuantCore
from ..quant.expected_value import StrategyStatsLike
from ..quant.strategies.base import Strategy
from ..risk.engine import DeterministicRiskEngine
from ..risk.state import AccountState, PortfolioState
from ..storage.decision_store import DecisionStore


@dataclass(frozen=True)
class RunResult:
    """What happened, and where it was recorded."""
    decision: DecisionLog
    stored_at: Path
    accepted: bool          # gate returned PASS or REDUCE
    reason: str


class DeterministicOrchestrator:
    """Runs one decision cycle and records it.

    account and portfolio are REQUIRED arguments with no defaults. Defaulting to a
    flat account would fabricate the portfolio state that the heat, exposure and
    correlation checks consume, and a fabricated portfolio produces a risk verdict
    that means nothing.
    """

    def __init__(
        self,
        *,
        strategies: Sequence[Strategy],
        execution_config: Mapping[str, Any],
        risk_config: Mapping[str, Any],
        store: DecisionStore,
    ):
        # Both raise at construction on misconfiguration: QuantCore via
        # EVCostNotConfigured, the engine via RiskEngineError on fail_closed.
        self.quant = QuantCore(strategies, execution_config)
        self.risk = DeterministicRiskEngine(risk_config)
        self.store = store

    # ------------------------------------------------------------------ #
    def run(
        self,
        snapshot: MarketSnapshot,
        *,
        account: AccountState,
        portfolio: PortfolioState,
        stats_by_strategy: Mapping[str, StrategyStatsLike] | None = None,
        sector: str | None = None,
        now: datetime | None = None,
    ) -> RunResult:
        now = now or utcnow()
        decision = DecisionLog(
            decision_id=new_id(IdKind.DECISION),
            snapshot_id=snapshot.snapshot_id,
            signal_id="",
            symbol=snapshot.symbol,
            state=SystemState.CREATED,
            snapshot=snapshot,
            created_at=now,
        )

        # --- Quant ------------------------------------------------------
        decision.state = SystemState.ANALYZING
        try:
            quant = self.quant.run(snapshot, stats_by_strategy=stats_by_strategy,
                                   now=now)
        except Exception as e:
            return self._finish(decision, SystemState.FAILED, None,
                                f"quant core failed: {type(e).__name__}: {e}", False)

        if not quant.ok or quant.candidate is None:
            return self._finish(decision, SystemState.REJECTED,
                                RejectionStage.QUANT, quant.reason, False)

        candidate = quant.candidate
        decision.signal_id = candidate.signal_id
        decision.candidate = candidate

        # --- Deterministic risk gate ------------------------------------
        decision.state = SystemState.RISK_CHECK
        try:
            gate = self.risk.evaluate(
                signal=candidate, account=account, portfolio=portfolio,
                stats=None, sector=sector,
                spread_pct=snapshot.spread_pct, gap_pct=snapshot.gap_pct,
                market_open=snapshot.market_open, now=now,
            )
        except Exception as e:
            # §111: the risk engine being unavailable is never an approval.
            return self._finish(decision, SystemState.FAILED, None,
                                f"risk engine failed: {type(e).__name__}: {e}", False)

        decision.risk_gate = gate

        if gate.verdict is RiskGateVerdict.REJECT:
            return self._finish(decision, SystemState.REJECTED,
                                RejectionStage.RISK_ENGINE,
                                gate.blocking_reason or "rejected", False)

        # PASS or REDUCE. Step 4 stops here: nothing downstream exists yet, and
        # no authorisation is minted.
        return self._finish(decision, SystemState.RISK_CHECK, None,
                            f"risk gate {gate.verdict.value}", True)

    # ------------------------------------------------------------------ #
    def _finish(self, decision: DecisionLog, state: SystemState,
                stage: RejectionStage | None, reason: str,
                accepted: bool) -> RunResult:
        decision.state = state
        decision.rejection_stage = stage
        decision.rejection_reason = None if accepted else reason
        decision.completed_at = utcnow()
        # If this raises, it propagates. A decision that could not be recorded
        # must not be reported as complete.
        path = self.store.append(decision)
        return RunResult(decision=decision, stored_at=path,
                         accepted=accepted, reason=reason)

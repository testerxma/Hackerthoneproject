"""
SpeedTrader AI — Options Decision Cycle

    MarketSnapshot -> QuantCore(S07) -> CandidateSignal
        -> option contract selection
        -> deterministic risk gate
        -> options sizing
        -> ExecutionAuthorization
        -> Alpaca paper execution
        -> DecisionLog -> DecisionStore

Thin by design: coordination only. No formula, no risk rule, no sizing arithmetic
and no broker payload is constructed here — each belongs to the module that owns
it, and duplicating any of them here would create a second place for them to
drift.

--------------------------------------------------------------------------------
HOW THE EQUITY RISK ENGINE STILL GOVERNS AN OPTIONS TRADE
--------------------------------------------------------------------------------
The deterministic risk engine was ported from Bot v6 and reasons about the
UNDERLYING signal: account halts, score, EV, duplicate positions, correlation,
spread, gap, TTL. Every one of those rules is still exactly as meaningful for an
option on that underlying, so the engine keeps its authority unchanged and its
verdict is still final.

What does NOT carry over is its share-count arithmetic. So the split is:

    engine  ->  MAY WE TRADE, and AT WHAT FRACTION OF RISK (size_multiplier)
    options ->  HOW MANY CONTRACTS that risk budget buys

The engine remains the sole authority on how much risk is permitted; the options
layer only converts an authorized risk budget into contracts. It can never
enlarge that budget, and the authorization is minted against the resulting
contract count, so nothing downstream can inflate it either.

A REJECT verdict ends the cycle before any contract is selected.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
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
from ..execution.authorization import AuthorizationRegistry, authorize
from ..execution.options_adapter import (
    OptionOrderRequest,
    OptionsExecutionAdapter,
    PositionIntent,
    SubmissionState,
    limit_price_for,
)
from ..options.contracts import (
    OptionContract,
    SelectionError,
    SelectionPolicy,
    select_contract,
)
from ..options.cost import OptionsCostError, estimate_options_cost
from ..options.risk import OptionsSizingPolicy, size_option_position
from ..quant.engine import QuantCore
from ..risk.engine import DeterministicRiskEngine
from ..risk.state import AccountState, PortfolioState
from ..storage.decision_store import DecisionStore


@dataclass
class OptionsRunResult:
    decision: DecisionLog
    stored_at: Any
    accepted: bool
    reason: str
    contract: OptionContract | None = None
    contracts_ordered: int = 0
    execution_state: SubmissionState | None = None
    broker_order_id: str | None = None


class OptionsOrchestrator:
    """One decision cycle, end to end. Every exit persists a DecisionLog."""

    def __init__(
        self,
        *,
        strategies: Sequence[Any],
        execution_config: Mapping[str, Any],
        risk_config: Mapping[str, Any],
        store: DecisionStore,
        chain_provider: Any,
        adapter: OptionsExecutionAdapter | None = None,
        registry: AuthorizationRegistry | None = None,
        selection_policy: SelectionPolicy | None = None,
        sizing_policy: OptionsSizingPolicy | None = None,
    ):
        self.quant = QuantCore(list(strategies), execution_config)
        self.risk = DeterministicRiskEngine(risk_config)
        self.store = store
        self.chain_provider = chain_provider
        self.adapter = adapter
        self.registry = registry or AuthorizationRegistry()
        self.selection_policy = selection_policy or SelectionPolicy()
        self.sizing_policy = sizing_policy or OptionsSizingPolicy(
            risk_per_trade_pct=float(risk_config.get("risk_per_trade_pct", 1.0))
        )
        self.execution_config = execution_config

    # ------------------------------------------------------------------ #
    def run(
        self,
        snapshot: MarketSnapshot,
        *,
        account: AccountState,
        portfolio: PortfolioState,
        sector: str | None = None,
        open_premium: float = 0.0,
        now: datetime | None = None,
        dry_run: bool = False,
    ) -> OptionsRunResult:
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

        # --- 1. Quant ---------------------------------------------------
        decision.state = SystemState.ANALYZING
        try:
            quant = self.quant.run(snapshot, now=now)
        except Exception as e:
            return self._finish(decision, SystemState.FAILED, None,
                                f"quant core failed: {type(e).__name__}: {e}", False)
        if not quant.ok or quant.candidate is None:
            return self._finish(decision, SystemState.REJECTED,
                                RejectionStage.QUANT, quant.reason, False)

        candidate = quant.candidate
        decision.signal_id = candidate.signal_id
        decision.candidate = candidate

        # --- 2. Deterministic risk gate (authority, unchanged) ----------
        decision.state = SystemState.RISK_CHECK
        try:
            gate = self.risk.evaluate(
                signal=candidate, account=account, portfolio=portfolio,
                sector=sector, spread_pct=snapshot.spread_pct,
                gap_pct=snapshot.gap_pct, market_open=snapshot.market_open, now=now,
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

        # --- 3. Contract selection --------------------------------------
        asof: date = now.date()
        try:
            chain = self.chain_provider(snapshot, asof)
        except Exception as e:
            # A data outage is NOT an empty chain and must not read as no-trade.
            return self._finish(decision, SystemState.FAILED, None,
                                f"options data unavailable: {type(e).__name__}: {e}",
                                False)
        try:
            selection = select_contract(
                chain, direction=candidate.direction.value,
                underlying_price=candidate.entry, asof=asof,
                policy=self.selection_policy,
            )
        except SelectionError as e:
            return self._finish(decision, SystemState.REJECTED,
                                RejectionStage.RISK_ENGINE,
                                f"no tradeable contract: {e}", False)

        # --- 4. Options sizing, inside the engine's risk budget ---------
        # The engine's size_multiplier (heat reduction, recovery mode, Kelly)
        # scales the risk budget. The options layer converts budget -> contracts
        # and can never widen it.
        multiplier = float(gate.size_multiplier or 1.0)
        scaled = OptionsSizingPolicy(
            risk_per_trade_pct=self.sizing_policy.risk_per_trade_pct * multiplier,
            max_contracts=self.sizing_policy.max_contracts,
            max_premium_pct_of_balance=self.sizing_policy.max_premium_pct_of_balance,
        )
        sizing = size_option_position(
            contract=selection.contract, account_balance=account.balance,
            policy=scaled, open_premium=open_premium,
        )
        if not sizing.approved:
            return self._finish(decision, SystemState.REJECTED,
                                RejectionStage.RISK_ENGINE,
                                f"options sizing rejected: {sizing.reason}", False)

        try:
            opt_cost = estimate_options_cost(
                entry_premium=sizing.premium_per_contract,
                contracts=sizing.quantity,
                multiplier=selection.contract.multiplier,
            )
        except OptionsCostError as e:
            return self._finish(decision, SystemState.FAILED, None,
                                f"options cost not computable: {e}", False)

        decision.options_trace = _trace(selection, sizing, opt_cost, multiplier)

        if dry_run or self.adapter is None:
            return self._finish(
                decision, SystemState.RISK_CHECK, None,
                f"authorized {sizing.quantity} x {selection.contract.symbol} "
                f"(dry run; no order submitted)", True,
                contract=selection.contract, contracts=sizing.quantity)

        # --- 5. Authorize, then execute ---------------------------------
        request = OptionOrderRequest(
            symbol=selection.contract.symbol,
            quantity=sizing.quantity,
            intent=PositionIntent.BUY_TO_OPEN,
            order_type="limit",
            limit_price=limit_price_for(selection.contract),
            time_in_force="day",
        )
        book = _portfolio_fingerprint(portfolio, account)
        try:
            licence = authorize(
                decision_id=decision.decision_id,
                snapshot_id=snapshot.snapshot_id,
                proposal=request.to_proposal(),
                portfolio=book,
                approved_quantity=sizing.quantity,
                ttl_seconds=float(self.execution_config.get("decision_ttl_seconds", 30)),
                now=now,
            )
        except Exception as e:
            return self._finish(decision, SystemState.FAILED, None,
                                f"authorization refused: {type(e).__name__}: {e}",
                                False, contract=selection.contract)

        decision.state = SystemState.EXECUTING
        result = self.adapter.submit(request, licence,
                                     portfolio_snapshot=book, now=now)

        if result.state is SubmissionState.SUBMITTED:
            # SUBMITTED is not FILLED. The cycle ends here and reconciliation
            # owns everything after it.
            return self._finish(
                decision, SystemState.EXECUTING, None,
                f"submitted {sizing.quantity} x {selection.contract.symbol}; "
                "awaiting reconciliation (submitted is not filled)", True,
                contract=selection.contract, contracts=sizing.quantity,
                execution_state=result.state, broker_order_id=result.broker_order_id)

        if result.state is SubmissionState.UNKNOWN:
            # Do not retry here. An order may exist.
            return self._finish(
                decision, SystemState.FAILED, None,
                f"execution outcome UNKNOWN, reconcile before retrying: "
                f"{result.reason}", False,
                contract=selection.contract, execution_state=result.state)

        return self._finish(
            decision, SystemState.REJECTED, RejectionStage.EXECUTION_GUARD,
            f"execution {result.state.value}: {result.reason}", False,
            contract=selection.contract, execution_state=result.state)

    # ------------------------------------------------------------------ #
    def _finish(self, decision, system_state, stage, reason, accepted, *,
                contract=None, contracts=0, execution_state=None,
                broker_order_id=None) -> OptionsRunResult:
        decision.state = system_state
        decision.rejection_stage = stage
        decision.rejection_reason = None if accepted else reason
        decision.completed_at = utcnow()
        # If this raises it propagates: a decision that could not be recorded
        # must never be reported as complete.
        path = self.store.append(decision)
        return OptionsRunResult(
            decision=decision, stored_at=path, accepted=accepted, reason=reason,
            contract=contract, contracts_ordered=contracts,
            execution_state=execution_state, broker_order_id=broker_order_id,
        )


def _portfolio_fingerprint(portfolio: PortfolioState,
                           account: AccountState) -> dict[str, Any]:
    """The book an authorization is bound to.

    Deliberately includes balance and every open position: if either changes
    between approval and submission, the approval was computed against a book
    that no longer exists and the licence must stop being valid.
    """
    return {
        "balance": round(float(account.balance), 2),
        "positions": sorted(
            [{"symbol": p.symbol, "qty": float(p.quantity),
              "side": p.side.value} for p in portfolio.positions],
            key=lambda d: (d["symbol"], d["side"]),
        ),
        "paused_symbols": sorted(portfolio.paused_symbols),
    }


def _trace(selection, sizing, cost, multiplier: float) -> dict[str, Any]:
    """The options half of the decision trace, structured for later query."""
    c = selection.contract
    return {
        "structure": selection.structure.value,
        "contract": {
            "symbol": c.symbol,
            "type": c.type.value,
            "strike": c.strike,
            "expiration": c.expiration.isoformat(),
            "multiplier": c.multiplier,
            "open_interest": c.open_interest,
            "bid": c.quote.bid if c.quote else None,
            "ask": c.quote.ask if c.quote else None,
        },
        "selection": {
            "reason": selection.reason,
            "considered": selection.considered,
            "rejected": selection.rejected,
        },
        "sizing": {
            "contracts": sizing.quantity,
            "premium_per_contract": sizing.premium_per_contract,
            "max_loss_per_contract": sizing.max_loss_per_contract,
            "max_loss_total": sizing.max_loss_total,
            "risk_budget": sizing.risk_budget,
            "risk_multiplier_from_engine": multiplier,
            "caps_applied": list(sizing.caps_applied),
            "reason": sizing.reason,
        },
        "estimated_fees": cost.as_breakdown(),
    }

"""
SpeedTrader AI — Deterministic Risk Engine
Spec: §50 AI Risk vs Deterministic Risk, §51-54 Risk Engine, §111 Fail Closed

    AI proposes. Deterministic controls authorise.

This module contains no LLM call, no network call and no randomness. Given the same
inputs it returns the same verdict, every time — which is precisely what makes it a
usable answer to TradingAgents' own documented non-reproducibility.

PORT PROVENANCE
  ApproveTrade()     -> evaluate(): the REJECT path, rules in original order
  ComputeFinalLot()  -> measures.size_position(): the REDUCE path

ONE DELIBERATE CHANGE FROM THE SOURCE
  Bot v6 short-circuits on the first failing rule, so you learn one reason and never
  see the rest. We evaluate every rule and record all of them, then report the first
  failure in the original order as the authoritative blocking reason. Semantics are
  identical — the same signals pass and the same signals are blocked — but the audit
  trail is complete. This is what the decision-trace UI renders.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from ..common.clock import is_expired, utcnow
from ..data.schemas import (
    CandidateSignal,
    Direction,
    RiskCheck,
    RiskGateResult,
    RiskGateVerdict,
)
from .measures import (
    has_correlated_same_direction,
    portfolio_heat_pct,
    sector_exposure_pct,
    size_position,
    symbol_exposure_pct,
)
from .state import AccountState, PortfolioState, StrategyStats

ENGINE_VERSION = "v1"


class RiskEngineError(Exception):
    """Raised only for programmer error. Never used for a trade rejection —
    a rejection is a valid RiskGateResult, not an exception."""


def _check(rule: str, passed: bool, observed=None, limit=None, reason: str = "") -> RiskCheck:
    return RiskCheck(rule=rule, passed=passed, observed=observed, limit=limit, reason=reason)


class DeterministicRiskEngine:
    """§53: may PASS, REDUCE or REJECT. May reject when every agent said BUY."""

    def __init__(self, risk_config: Mapping):
        self.cfg = risk_config
        if not self.cfg.get("fail_closed", False):
            # §113: refuse to run with fail-open configuration.
            raise RiskEngineError("risk config must set fail_closed: true")

    # ----------------------------------------------------------------- #
    def evaluate(
        self,
        *,
        signal: CandidateSignal,
        account: AccountState,
        portfolio: PortfolioState,
        stats: StrategyStats | None = None,
        closes_by_symbol: Mapping[str, Sequence[float]] | None = None,
        sector: str | None = None,
        spread_pct: float | None = None,
        gap_pct: float | None = None,
        news_blocked: bool = False,
        market_open: bool = True,
        within_entry_window: bool = True,
        now=None,
    ) -> RiskGateResult:
        """Returns a verdict and the full list of checks. Never raises on rejection."""
        cfg = self.cfg
        now = now or utcnow()
        stats = stats or StrategyStats(strategy_id=signal.strategy_id)
        checks: list[RiskCheck] = []
        risk_pct = cfg.get("risk_per_trade_pct", 1.0)

        # --- 1. Account-level halts (Bot v6 order preserved) --------------
        checks.append(_check("manual_pause", not account.manually_paused,
                             account.manually_paused, False, "manually paused"))
        checks.append(_check("daily_loss_limit", not account.halted_daily,
                             round(account.daily_drawdown_pct(), 2),
                             cfg.get("daily_loss_limit_pct"), "daily loss limit"))
        checks.append(_check("weekly_loss_limit", not account.halted_weekly,
                             account.halted_weekly, False, "weekly loss limit"))
        checks.append(_check("daily_profit_lock", not account.profit_locked,
                             account.profit_locked, False, "daily profit lock"))
        checks.append(_check("health_watchdog", account.health_ok,
                             account.health_ok, True, "health watchdog"))
        checks.append(_check("consecutive_losses",
                             account.consecutive_losses < cfg.get("max_consecutive_losses", 6),
                             account.consecutive_losses, cfg.get("max_consecutive_losses", 6),
                             "consec-loss cooldown"))

        # --- 2. Time gate (rewritten: US equity session, not FX 24/5) -----
        checks.append(_check("market_open", market_open, market_open, True, "market closed"))
        checks.append(_check("entry_window", within_entry_window,
                             within_entry_window, True, "outside entry window"))

        # --- 3. Symbol / strategy availability ----------------------------
        sym_active = signal.symbol not in portfolio.paused_symbols
        checks.append(_check("symbol_active", sym_active, signal.symbol, "active",
                             "symbol paused"))
        checks.append(_check("strategy_not_demoted", not stats.demoted,
                             stats.demoted, False, "strategy demoted"))

        # --- 4. Signal quality --------------------------------------------
        min_score = cfg.get("min_score", 48.0)
        checks.append(_check("min_score", signal.total_score >= min_score,
                             signal.total_score, min_score, "score below min"))

        # EV gate. Kept honest: while the strategy has no history the EV is a
        # bootstrap assumption (win rate 0.50), so it is structurally positive and
        # this gate is not filtering anything. We record that rather than pretend.
        if cfg.get("require_positive_ev", True):
            ev_ok = signal.expected_value > 0.0
            note = "non-positive EV" if not ev_ok else (
                "bootstrap EV — not yet a real filter" if signal.ev_is_bootstrap else ""
            )
            checks.append(_check("positive_ev", ev_ok, signal.expected_value, 0.0, note))

        min_rr = cfg.get("min_reward_risk", 1.5)
        checks.append(_check("min_reward_risk", signal.reward_risk >= min_rr,
                             round(signal.reward_risk, 2), min_rr, "reward:risk too low"))

        if signal.atr_at_signal:
            min_atr = cfg.get("min_stop_distance_atr", 0.5)
            ratio = signal.stop_distance / signal.atr_at_signal
            checks.append(_check("min_stop_distance", ratio >= min_atr,
                                 round(ratio, 2), min_atr, "stop too tight for volatility"))

        # --- 5. Duplication -----------------------------------------------
        already = portfolio.count_open(signal.symbol, signal.strategy_id)
        checks.append(_check("no_duplicate_position", already == 0, already, 0,
                             "already open (strategy+symbol)"))
        max_pos = cfg.get("max_open_positions", 5)
        checks.append(_check("max_open_positions", len(portfolio.positions) < max_pos,
                             len(portfolio.positions), max_pos, "max open positions"))

        # --- 6. Exposure ---------------------------------------------------
        heat = portfolio_heat_pct(portfolio, account, risk_pct)
        heat_max = cfg.get("portfolio_heat_max_pct", 8.0)
        checks.append(_check("portfolio_heat", heat < heat_max, heat, heat_max,
                             f"portfolio heat {heat:.1f}%"))

        sym_exp = symbol_exposure_pct(portfolio, account, signal.symbol, risk_pct) + risk_pct
        sym_max = cfg.get("max_symbol_exposure_pct", 4.0)
        checks.append(_check("symbol_exposure", sym_exp <= sym_max, round(sym_exp, 2),
                             sym_max, "symbol exposure cap"))

        if sector:
            sec_exp = sector_exposure_pct(portfolio, account, sector, risk_pct) + risk_pct
            sec_max = cfg.get("max_sector_exposure_pct", 6.0)
            checks.append(_check("sector_exposure", sec_exp <= sec_max, round(sec_exp, 2),
                                 sec_max, f"sector exposure cap ({sector})"))

        # --- 7. Correlation -------------------------------------------------
        if cfg.get("correlation_enabled", True) and closes_by_symbol:
            blocked, other, r = has_correlated_same_direction(
                signal.symbol, signal.direction.value, portfolio,
                {k: list(v) for k, v in closes_by_symbol.items()},
                cfg.get("correlation_threshold", 0.7),
                cfg.get("correlation_lookback_bars", 30),
            )
            checks.append(_check("correlation", not blocked, r,
                                 cfg.get("correlation_threshold", 0.7),
                                 f"correlated same-direction position ({other})" if blocked else ""))

        # --- 8. Microstructure -----------------------------------------------
        if spread_pct is not None:
            max_spread = cfg.get("max_spread_pct", 0.15)
            checks.append(_check("spread", spread_pct <= max_spread, round(spread_pct, 4),
                                 max_spread, "spread too wide"))

        if gap_pct is not None:
            max_gap = cfg.get("max_gap_pct", 3.0)
            checks.append(_check("overnight_gap", abs(gap_pct) <= max_gap, round(gap_pct, 2),
                                 max_gap, "overnight gap too large"))

        checks.append(_check("news_window", not news_blocked, news_blocked, False,
                             "inside news blackout window"))

        # --- 9. TTL -----------------------------------------------------------
        expired = is_expired(signal.expires_at, now)
        checks.append(_check("signal_ttl", not expired, signal.expires_at.isoformat(),
                             None, "signal expired"))

        # ---------------------------------------------------------------------
        failed = [c for c in checks if not c.passed]
        if failed:
            first = failed[0]
            return RiskGateResult(
                verdict=RiskGateVerdict.REJECT,
                checks=checks,
                blocking_reason=first.reason or first.rule,
                portfolio_heat_pct=heat,
                evaluated_at=now,
                engine_version=ENGINE_VERSION,
            )

        # --- All checks passed: size it (the REDUCE path) ----------------------
        sizing = size_position(
            account=account, portfolio=portfolio,
            stop_distance=signal.stop_distance, stats=stats, cfg=cfg,
        )

        if sizing.quantity <= 0:
            checks.append(_check("computed_quantity", False, sizing.quantity, ">0",
                                 "computed size rounds to zero shares"))
            return RiskGateResult(
                verdict=RiskGateVerdict.REJECT, checks=checks,
                blocking_reason="computed size rounds to zero shares",
                portfolio_heat_pct=heat, evaluated_at=now, engine_version=ENGINE_VERSION,
            )

        checks.append(_check("computed_quantity", True, sizing.quantity, ">0"))

        verdict = (
            RiskGateVerdict.REDUCE if sizing.multiplier < 1.0 else RiskGateVerdict.PASS
        )
        return RiskGateResult(
            verdict=verdict,
            checks=checks,
            approved_quantity=float(sizing.quantity),
            size_multiplier=sizing.multiplier,
            size_multiplier_breakdown=sizing.breakdown,
            portfolio_heat_pct=heat,
            evaluated_at=now,
            engine_version=ENGINE_VERSION,
        )

    # ----------------------------------------------------------------- #
    def revalidate(self, **kwargs) -> RiskGateResult:
        """§57: after the Portfolio Manager modifies anything material, the proposal
        goes through the engine again. Same rules, no exceptions, no shortcut."""
        return self.evaluate(**kwargs)

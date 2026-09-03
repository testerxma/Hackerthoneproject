"""
SpeedTrader AI — Risk Measures & Position Sizing
Spec: §52 Risk Engine Responsibilities, §53 PASS/REDUCE/REJECT

DELIBERATE CONSOLIDATION: the draft layout had position_sizing.py in BOTH quant/ and
risk/. In Bot v6, ComputeFinalLot() is one function and it is the risk authority — the
quant core never sizes anything. Two sizing paths would silently diverge, and the one
that diverges downward is the one that loses money quietly. There is exactly one
size_position() in this codebase and it lives here.
"""

from __future__ import annotations

import math
from typing import Mapping

from .state import AccountState, OpenPosition, PortfolioState, StrategyStats


# --------------------------------------------------------------------------
# Portfolio heat — Bot v6 PortfolioHeat()
# --------------------------------------------------------------------------

def portfolio_heat_pct(
    portfolio: PortfolioState,
    account: AccountState,
    risk_per_trade_pct: float,
) -> float:
    """Sum of per-position risk-to-stop, as % of balance.

    Positions with no stop contribute risk_per_trade_pct (Bot v6 returns
    InpRiskPerTrade when sl<=0) — assuming zero would understate real exposure.
    """
    total = 0.0
    for p in portfolio.positions:
        r = p.risk_pct(account.balance)
        total += risk_per_trade_pct if math.isnan(r) else r
    return round(total, 4)


def symbol_exposure_pct(
    portfolio: PortfolioState,
    account: AccountState,
    symbol: str,
    risk_per_trade_pct: float,
) -> float:
    """Equity replacement for Bot v6 CurrencyExposure().

    The original decomposed net risk across base/quote currency because FX pairs
    share currencies. Equities do not have base/quote, so the analogous cluster
    risk is per-symbol and per-sector. Same 4.0% ceiling, different partition.
    """
    total = 0.0
    for p in portfolio.positions_for(symbol):
        r = p.risk_pct(account.balance)
        total += risk_per_trade_pct if math.isnan(r) else r
    return round(total, 4)


def sector_exposure_pct(
    portfolio: PortfolioState,
    account: AccountState,
    sector: str | None,
    risk_per_trade_pct: float,
) -> float:
    """Sectors cluster for equities the way currencies cluster for FX."""
    if sector is None:
        return 0.0
    total = 0.0
    for p in portfolio.positions:
        if p.sector != sector:
            continue
        r = p.risk_pct(account.balance)
        total += risk_per_trade_pct if math.isnan(r) else r
    return round(total, 4)


# --------------------------------------------------------------------------
# Correlation — Bot v6 Correlation(a, b, n=30)
# --------------------------------------------------------------------------

def pearson_correlation(a: list[float], b: list[float]) -> float:
    """Pearson r on close-to-close differences. Ported exactly, including the
    original's guard: fewer than 5 usable points returns 0 (treated as uncorrelated)."""
    n = min(len(a), len(b))
    if n < 5:
        return 0.0
    a, b = a[:n], b[:n]
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b)) / n
    va = sum((x - ma) ** 2 for x in a) / n
    vb = sum((y - mb) ** 2 for y in b) / n
    if va <= 0 or vb <= 0:
        return 0.0
    return cov / math.sqrt(va * vb)


def returns_from_closes(closes: list[float]) -> list[float]:
    """Bot v6 used raw price differences (Cl(k) - Cl(k+1)), not percentage returns.

    For FX pairs of similar scale that is fine. For equities spanning $12 to $900 it
    is not: raw differences would let a high-priced stock dominate the covariance.
    We use log returns, which is the same measure Bot v6 intended at FX scale.
    """
    out = []
    for i in range(1, len(closes)):
        prev, cur = closes[i - 1], closes[i]
        if prev > 0 and cur > 0:
            out.append(math.log(cur / prev))
    return out


def has_correlated_same_direction(
    signal_symbol: str,
    signal_direction: str,
    portfolio: PortfolioState,
    closes_by_symbol: Mapping[str, list[float]],
    threshold: float,
    lookback: int,
) -> tuple[bool, str | None, float]:
    """Bot v6 CorrelatedSameDirOpen(). Returns (blocked, which_symbol, max_r)."""
    base = closes_by_symbol.get(signal_symbol)
    if not base:
        return False, None, 0.0
    base_r = returns_from_closes(base[-(lookback + 1):])

    worst_sym, worst_r = None, 0.0
    for p in portfolio.positions:
        if p.symbol == signal_symbol or p.side.value != signal_direction:
            continue
        other = closes_by_symbol.get(p.symbol)
        if not other:
            continue
        r = pearson_correlation(base_r, returns_from_closes(other[-(lookback + 1):]))
        if r > worst_r:
            worst_sym, worst_r = p.symbol, r
    return (worst_r > threshold), worst_sym, round(worst_r, 4)


# --------------------------------------------------------------------------
# Kelly — Bot v6 KellyMultiplier()
# --------------------------------------------------------------------------

def kelly_multiplier(stats: StrategyStats, account: AccountState, cfg: Mapping) -> float:
    """Fractional Kelly mapped to a multiplier around 1.0, clamped to [0.5, 1.5].

    Shadow-safe: the original returns 1.0 unless the performance layer is ACTIVE,
    so Kelly is computed and logged but does not move size until promoted. We keep
    that, which matters here because a hackathon run has no sample to promote on.
    """
    if not cfg.get("kelly_enabled", False):
        return 1.0
    if stats.trades < cfg.get("kelly_min_trades", 30):
        return 1.0
    if stats.avg_loss <= 0:
        return 1.0
    b = stats.avg_win / stats.avg_loss
    if b <= 0:
        return 1.0
    full_kelly = stats.win_rate - (1.0 - stats.win_rate) / b
    frac = full_kelly * cfg.get("kelly_fraction", 0.35)
    if not account.kelly_layer_active:
        return 1.0  # shadow mode
    return _clamp(
        1.0 + frac,
        cfg.get("kelly_multiplier_floor", 0.5),
        cfg.get("kelly_multiplier_ceiling", 1.5),
    )


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else (hi if v > hi else v)


# --------------------------------------------------------------------------
# Position sizing — Bot v6 ComputeFinalLot(). THE single sizing authority.
# --------------------------------------------------------------------------

class SizingResult:
    __slots__ = ("quantity", "risk_amount", "multiplier", "breakdown", "capped")

    def __init__(self, quantity, risk_amount, multiplier, breakdown, capped):
        self.quantity = quantity
        self.risk_amount = risk_amount
        self.multiplier = multiplier
        self.breakdown = breakdown
        self.capped = capped


def size_position(
    *,
    account: AccountState,
    portfolio: PortfolioState,
    stop_distance: float,
    stats: StrategyStats,
    cfg: Mapping,
    allow_fractional: bool = False,
) -> SizingResult:
    """Ported from ComputeFinalLot(), with pip/lot arithmetic removed.

    FX:       lots   = riskMoney / (slPips * pipValuePerLot)
    Equities: shares = riskMoney / stopDistanceInDollars

    The pip-value term disappears entirely because a share's P&L per dollar of price
    move is exactly 1. This is the one place the FX-to-equity translation makes the
    code simpler rather than harder.
    """
    risk_pct = cfg.get("risk_per_trade_pct", 1.0)
    risk_money = account.balance * risk_pct / 100.0

    if stop_distance <= 0 or account.balance <= 0:
        return SizingResult(0.0, 0.0, 0.0, {"invalid_inputs": 0.0}, False)

    base_qty = risk_money / stop_distance

    breakdown: dict[str, float] = {}
    m = 1.0

    k = kelly_multiplier(stats, account, cfg)
    if k != 1.0:
        breakdown["kelly"] = round(k, 4)
        m *= k

    # Equity-curve layer: after a >1% intraday drawdown, size down (shadow-gated).
    if account.equity_curve_layer_active and account.day_start_equity > 0:
        if account.equity < account.day_start_equity * 0.99:
            breakdown["equity_curve"] = 0.75
            m *= 0.75

    heat = portfolio_heat_pct(portfolio, account, risk_pct)
    if heat >= cfg.get("heat_reduce_level_pct", 6.0):
        red = cfg.get("heat_reduce_multiplier", 0.5)
        breakdown["heat_reduce"] = red
        m *= red

    if account.recovery_mode:
        rec = cfg.get("recovery_risk_multiplier", 0.5)
        breakdown["recovery"] = rec
        m *= rec

    m_clamped = _clamp(
        m,
        cfg.get("min_size_multiplier", 0.25),
        cfg.get("max_size_multiplier", 1.50),
    )
    if m_clamped != m:
        breakdown["clamped_to"] = round(m_clamped, 4)

    qty = base_qty * m_clamped
    qty = qty if allow_fractional else math.floor(qty)

    # Bot v6 final safety: recompute real risk and cap at 101% of the budget.
    # Keeps a rounding artefact from ever exceeding the stated risk per trade.
    real_risk = qty * stop_distance
    capped = False
    if real_risk > risk_money * 1.01:
        qty = risk_money / stop_distance
        qty = qty if allow_fractional else math.floor(qty)
        real_risk = qty * stop_distance
        capped = True

    return SizingResult(
        quantity=round(qty, 6) if allow_fractional else int(qty),
        risk_amount=round(real_risk, 2),
        multiplier=round(m_clamped, 4),
        breakdown=breakdown,
        capped=capped,
    )

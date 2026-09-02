"""
SpeedTrader AI — Expected Value Layer

PORT OF: docs/reference/SpeedTraderBot_v6.1.mq5, ComputeEV L794-813
SOURCE SHA-256: c799acaa797a4f23a8c9531c3b4f14599b73736af2151d9eeb7f42209332e8d9

--------------------------------------------------------------------------------
AUTHORITATIVE MQL5 FORMULA — PIP-DENOMINATED
--------------------------------------------------------------------------------
    L798   if(tr<20){ estWinRate=0.50; estAvgWin=tpPips; estAvgLoss=slPips; }
    L802   else estWinRate = Clamp(wr * RegimeMultiplier(si, regime), 0.15, 0.85)
    L803   estAvgWin  = avgWinPips  > 0 ? avgWinPips  : tpPips
    L804   estAvgLoss = avgLossPips > 0 ? avgLossPips : slPips
    L809   costPips   = SpreadPips + 0.5 + abs(slip)
    L810   EV         = estWinRate*estAvgWin - (1-estWinRate)*estAvgLoss - costPips
    L811   normEV     = Clamp(50 + EV, 0, 100)
    L812   priority   = totalScore*0.5 + normEV*0.5

Every term of L810 carries a `Pips` suffix (L232/233/240/255/256, L409-414, L891).
A pip has no equity definition, and SymbolPip was deliberately not emulated in the
S07 port. Pips are therefore not a portable option.

--------------------------------------------------------------------------------
EQUITY ADAPTATION — R-DENOMINATED.  NO MQL5 EV PARITY IS CLAIMED.
--------------------------------------------------------------------------------
Every term is divided by stop_distance, giving EV in R-multiples (profit per unit
of risk-to-stop). Because stop_distance > 0, this is a positive scaling and the
SIGN of EV is preserved exactly — which matters because the only consumer,
risk/engine.py, applies a sign test (`expected_value > 0.0`) and nothing else.

    MQL5:   costPips = SpreadPips + 0.5 + abs(slip)
    Equity: cost_R   = (spread + configured_cost_per_share + slippage) / stop

    spread  PORTED     — from the snapshot, same quantity, relative units
    +0.5    REPLACED   — a pip constant with no equity meaning. NOT converted.
                         Supplied by explicit configuration, or the build fails.
    slip    0.0        — measured slippage (L808) needs trade history

DEVIATIONS, carried on every EVResult:
    1. EV is R-denominated rather than pip-denominated.
    2. MQL5's +0.5 pip constant is NOT numerically converted.
    3. RegimeMultiplier (L801) fixed at 1.0 — DetectMktState is not ported and
       snapshot.regime is always UNKNOWN.
    4. Slippage fixed at 0.0 — no trade history exists.
    5. L811's +50 offset is preserved structurally but is degenerate in R units.

On deviation 5: the offset was calibrated for pip magnitudes. In pips, EV spans
roughly -20..+40, so normEV spans 30..90 and contributes 15..45 to priority —
comparable to the score half. In R units EV spans roughly -2..+3, so normEV spans
48..53 and contributes 24..26.5, a band of 2.5 points against a score contribution
spanning ~25. EV supplies about 6% of the discriminating power.

This is INERT while one strategy runs on one symbol, because combined_priority is
used only to rank competing signals (L1975). It BLOCKS enabling multi-strategy
ranking, and must be revisited before a second strategy is turned on.

--------------------------------------------------------------------------------
THE COST CONFIGURATION GAP IS DELIBERATE
--------------------------------------------------------------------------------
This repository has NO authoritative pre-trade equity transaction-cost model, and
none can be derived from the source. Rather than invent a default — including a
0.0 default, which would silently make EV optimistic — compute_ev raises
EVCostNotConfigured and no CandidateSignal is produced.

Missing safety-relevant configuration is a deployment error, not an expected
operating condition, so it raises rather than returning a result object. This
matches RiskEngineError (fail_closed absent) and AlpacaConfigError (keys absent).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

from ..data.schemas import MarketSnapshot
from .cost_policy import (  # noqa: F401  (EVCostNotConfigured re-exported)
    COST_BLOCK,
    CostPolicy,
    CostPolicyError,
    CostPolicyInvalid,
    EVCostNotConfigured,
)

SOURCE_HASH = "c799acaa797a4f23a8c9531c3b4f14599b73736af2151d9eeb7f42209332e8d9"

#: Retained for backwards compatibility of imports. The cost contract is now the
#: structured `transaction_cost` block parsed by cost_policy.cost_policy_from_config.
COST_KEY = COST_BLOCK

EV_MIN_TRADES = 20           # L798
EV_BOOTSTRAP_WIN_RATE = 0.50  # L798
WIN_RATE_FLOOR = 0.15        # L802
WIN_RATE_CEILING = 0.85      # L802
REGIME_MULTIPLIER = 1.0      # deviation 3

DEVIATIONS = (
    "EV is R-denominated, not pip-denominated (MQL5 L810)",
    "MQL5 +0.5 pip constant (L809) is replaced by configuration, NOT converted",
    "RegimeMultiplier (L801) fixed at 1.0 — DetectMktState not ported",
    "Slippage (L808) is a CONFIGURED per-share estimate, not a measured value — "
    "no trade history exists",
    "Cost is a PRE-TRADE ROUND-TRIP estimate, price-aware but quantity-unaware; "
    "the TAF per-trade cap and daily fee-aggregation rounding are not modelled",
    "L811 +50 offset preserved structurally but degenerate in R units",
)


@runtime_checkable
class StrategyStatsLike(Protocol):
    """Structural contract for strategy performance input.

    Deliberately a Protocol rather than an import of risk.state.StrategyStats:
    quant must not depend on risk. risk.state.StrategyStats satisfies this
    structurally, so the risk layer can pass its own object unchanged.
    """
    trades: int
    win_rate: float
    avg_win: float
    avg_loss: float


@dataclass(frozen=True)
class _NoStats:
    """Zeroed stats — forces the bootstrap branch (L798)."""
    trades: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0


@dataclass(frozen=True)
class EVResult:
    expected_value: float
    norm_ev: float
    combined_priority: float
    is_bootstrap: bool
    win_rate: float
    avg_win_r: float
    avg_loss_r: float
    cost_r: float
    cost_breakdown: dict = field(default_factory=dict)
    deviations: tuple[str, ...] = DEVIATIONS


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else (hi if v > hi else v)


def compute_ev(
    *,
    entry: float,
    stop_loss: float,
    take_profit: float,
    stop_distance: float,
    total_score: float,
    snapshot: MarketSnapshot,
    cost_policy: CostPolicy,
    direction: str,
    stats: StrategyStatsLike | None = None,
    ev_min_trades: int = EV_MIN_TRADES,
    ev_bootstrap_win_rate: float = EV_BOOTSTRAP_WIN_RATE,
) -> EVResult:
    """R-denominated ComputeEV.

    `cost_policy` is a validated CostPolicy. Construction of that object is where
    the fail-closed check lives (cost_policy.cost_policy_from_config), so by the
    time control reaches here the policy is known-good and no partial EVResult can
    escape on a configuration failure.
    """
    if cost_policy is None:
        raise EVCostNotConfigured("no CostPolicy supplied")

    if stop_distance <= 0:
        raise ValueError(f"stop_distance must be positive, got {stop_distance}")

    stats = stats or _NoStats()

    # --- L798 bootstrap / L802-804 live estimate ---------------------
    if stats.trades < ev_min_trades:
        win_rate = ev_bootstrap_win_rate
        avg_win_r = abs(take_profit - entry) / stop_distance
        avg_loss_r = 1.0                      # slPips / stop == 1.0 by definition
        bootstrap = True
    else:
        win_rate = _clamp(stats.win_rate * REGIME_MULTIPLIER,
                          WIN_RATE_FLOOR, WIN_RATE_CEILING)
        avg_win_r = (stats.avg_win if stats.avg_win > 0
                     else abs(take_profit - entry) / stop_distance)
        avg_loss_r = stats.avg_loss if stats.avg_loss > 0 else 1.0
        bootstrap = False

    # --- Cost, L809 adapted ------------------------------------------
    # Price-aware: `direction` decides which leg is the sell, and therefore where
    # the sell-only SEC and TAF components land. See quant/cost_policy.py.
    spread = snapshot.spread if (cost_policy.include_spread and snapshot.spread) else 0.0
    estimate = cost_policy.estimate(entry=entry, take_profit=take_profit,
                                    direction=direction, spread=spread)
    cost_r = estimate.total_per_share() / stop_distance

    # --- L810 / L811 / L812, structure preserved ---------------------
    expected_value = win_rate * avg_win_r - (1.0 - win_rate) * avg_loss_r - cost_r
    norm_ev = _clamp(50.0 + expected_value, 0.0, 100.0)
    combined_priority = total_score * 0.5 + norm_ev * 0.5

    return EVResult(
        expected_value=expected_value,
        norm_ev=norm_ev,
        combined_priority=combined_priority,
        is_bootstrap=bootstrap,
        win_rate=win_rate,
        avg_win_r=avg_win_r,
        avg_loss_r=avg_loss_r,
        cost_r=cost_r,
        cost_breakdown=cost_policy.breakdown(estimate),
    )

"""
SpeedTrader AI — Risk State
Spec: §51 Deterministic Risk Engine, §52 Responsibilities

These models replace Bot v6's global variables. In the original everything lived in
file-scope globals (g_haltedDaily, g_accountConsecLosses, g_recoveryMode, ...), which
made the risk rules untestable — you could not construct "account at 8.4% heat" without
running the whole EA. Here the engine takes state as an argument, so every rule is
testable in isolation. Behaviour is unchanged; only the plumbing is.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import ConfigDict, Field

from ..common.clock import utcnow
from ..data.schemas import Base, Direction


class OpenPosition(Base):
    """One live position. Mirrors what Alpaca's positions endpoint returns."""
    model_config = ConfigDict(extra="ignore")  # broker payloads carry extra fields

    symbol: str
    side: Direction
    quantity: float
    entry_price: float
    current_price: float | None = None
    stop_loss: float | None = None
    unrealized_pnl: float = 0.0
    strategy_id: str | None = None
    sector: str | None = None
    opened_at: datetime | None = None

    def risk_pct(self, account_balance: float) -> float:
        """Bot v6 PositionRiskPct(): money at risk to the stop, as % of balance.

        Original: if(sl<=0) return InpRiskPerTrade;  -- a position with no stop is
        assumed to carry a full unit of risk rather than zero. We keep that, because
        assuming zero risk for an unstopped position is the dangerous direction.
        """
        if account_balance <= 0:
            return 0.0
        if self.stop_loss is None or self.stop_loss <= 0:
            return float("nan")  # caller substitutes risk_per_trade_pct, see engine
        money_at_risk = abs(self.entry_price - self.stop_loss) * self.quantity
        return money_at_risk / account_balance * 100.0


class StrategyStats(Base):
    """Bot v6 StratStats. Drives Kelly sizing (§8) and decay demotion (§38)."""
    strategy_id: str
    trades: int = 0
    wins: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0          # in R units, not pips
    avg_loss: float = 0.0
    profit_factor: float = 1.0
    recent_losses: int = 0
    last_loss_at: datetime | None = None
    demoted: bool = False         # #38 strategy decay monitor


class AccountState(Base):
    """Bot v6 account-level globals, made explicit and injectable."""
    balance: float
    equity: float
    day_start_equity: float = 0.0
    week_start_equity: float = 0.0
    day_peak_equity: float = 0.0
    equity_high_water: float = 0.0

    # Halt flags — any one true blocks all new entries
    manually_paused: bool = False        # g_paused
    halted_daily: bool = False           # g_haltedDaily
    halted_weekly: bool = False          # g_haltedWeekly
    profit_locked: bool = False          # g_profitLocked (#34)
    health_ok: bool = True               # g_healthOK (#36)
    recovery_mode: bool = False          # g_recoveryMode (#44)
    consecutive_losses: int = 0          # g_accountConsecLosses

    kelly_layer_active: bool = False     # g_perfMatrixState == LAYER_ACTIVE
    equity_curve_layer_active: bool = False

    as_of: datetime = Field(default_factory=utcnow)

    def daily_drawdown_pct(self) -> float:
        if self.day_start_equity <= 0:
            return 0.0
        return max(0.0, (self.day_start_equity - self.equity) / self.day_start_equity * 100.0)

    def total_drawdown_pct(self) -> float:
        if self.equity_high_water <= 0:
            return 0.0
        return max(0.0, (self.equity_high_water - self.equity) / self.equity_high_water * 100.0)


class PortfolioState(Base):
    """Everything the risk engine needs to know about current exposure."""
    positions: list[OpenPosition] = Field(default_factory=list)
    paused_symbols: set[str] = Field(default_factory=set)   # #43 symbol pause
    orders_today: int = 0
    as_of: datetime = Field(default_factory=utcnow)

    def positions_for(self, symbol: str) -> list[OpenPosition]:
        return [p for p in self.positions if p.symbol == symbol]

    def count_open(self, symbol: str, strategy_id: str | None = None) -> int:
        """Bot v6 CountOpen(strat, sym) — the magic number encoded both."""
        return sum(
            1
            for p in self.positions
            if p.symbol == symbol
            and (strategy_id is None or p.strategy_id == strategy_id)
        )

"""
SpeedTrader AI — Deterministic Signal Backtest

--------------------------------------------------------------------------------
READ THIS FIRST: WHAT THIS DOES AND DOES NOT MEASURE
--------------------------------------------------------------------------------
This backtests the UNDERLYING S07 SIGNAL. It does NOT backtest the options
strategy, and its results must never be presented as options P&L.

The reason is a data fact, not a shortcut. Backtesting the options expression
requires historical option chains — bid and ask for each contract on each day.
That data is not in this repository. The only way to produce option prices
without it is to price them with a model (Black-Scholes and a volatility
assumption), and a backtest built that way measures THE PRICING MODEL, not the
strategy. It would produce confident-looking numbers that are an artefact of an
assumption nobody validated.

So this measures exactly what the available data supports: if S07 says BUY at
this price with this stop and this target, what happened next on the underlying?
That is a real, checkable question. Options P&L is a different question and is
left unanswered rather than answered wrongly.

--------------------------------------------------------------------------------
LOOK-AHEAD IS PREVENTED STRUCTURALLY, NOT BY CARE
--------------------------------------------------------------------------------
At each step the strategy is shown a SLICE of history, `bars[:i]`, and nothing
else. The outcome is then resolved using `bars[i:]` only. The two sets never
overlap, so the strategy cannot see the bar that decides its own result — the
mechanism makes the mistake impossible rather than relying on discipline.

--------------------------------------------------------------------------------
WHEN A BAR CONTAINS BOTH THE STOP AND THE TARGET
--------------------------------------------------------------------------------
Assume the STOP was hit. Intrabar ordering is unknowable from OHLC, and the
optimistic assumption is exactly how backtests flatter themselves. This one takes
the pessimistic branch every time, so reported results understate rather than
overstate.

--------------------------------------------------------------------------------
WHAT IS DELIBERATELY NOT REPORTED
--------------------------------------------------------------------------------
No Sharpe ratio. Sharpe on a few dozen trades from one synthetic or short series
is noise wearing a suit, and quoting it would imply a statistical confidence the
sample cannot support. Expectancy in R, win rate, profit factor and max drawdown
are reported instead — all of which degrade honestly with a small sample, and the
trade count is always printed beside them.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Sequence

from ..data.schemas import Bar

#: A trade that never resolves inside the data is not a win. It is excluded from
#: win-rate and expectancy and counted separately, because silently dropping it
#: would bias results toward whichever side happened to resolve.
class Exit(StrEnum):
    TARGET = "target"
    STOP = "stop"
    UNRESOLVED = "unresolved"       # ran out of data before either was touched


class BacktestError(ValueError):
    """The backtest cannot be run as configured. Never a silent empty result."""


@dataclass(frozen=True)
class Trade:
    index: int
    direction: str
    entry: float
    stop_loss: float
    take_profit: float
    exit: Exit
    bars_held: int
    r_multiple: float               # +2.0 at target, -1.0 at stop, before costs
    score: float = 0.0

    @property
    def resolved(self) -> bool:
        return self.exit is not Exit.UNRESOLVED


@dataclass(frozen=True)
class BacktestResult:
    trades: list[Trade]
    bars_tested: int
    cost_r: float = 0.0
    label: str = ""

    # ---- statistics ------------------------------------------------
    @property
    def resolved(self) -> list[Trade]:
        return [t for t in self.trades if t.resolved]

    @property
    def unresolved_count(self) -> int:
        return len(self.trades) - len(self.resolved)

    @property
    def wins(self) -> list[Trade]:
        return [t for t in self.resolved if t.exit is Exit.TARGET]

    @property
    def losses(self) -> list[Trade]:
        return [t for t in self.resolved if t.exit is Exit.STOP]

    @property
    def win_rate(self) -> float | None:
        """None, not 0.0, when there are no trades. Zero would read as a
        measured 0% win rate rather than an absence of evidence."""
        return len(self.wins) / len(self.resolved) if self.resolved else None

    def net_r(self, trade: Trade) -> float:
        """R after the round-trip cost assumption."""
        return trade.r_multiple - self.cost_r

    @property
    def total_r(self) -> float:
        return sum(self.net_r(t) for t in self.resolved)

    @property
    def expectancy_r(self) -> float | None:
        return self.total_r / len(self.resolved) if self.resolved else None

    @property
    def profit_factor(self) -> float | None:
        gains = sum(r for r in map(self.net_r, self.resolved) if r > 0)
        pains = -sum(r for r in map(self.net_r, self.resolved) if r < 0)
        if pains <= 0:
            return None            # undefined, not "infinite"
        return gains / pains

    @property
    def equity_curve(self) -> list[float]:
        """Cumulative R, starting at zero."""
        curve, running = [0.0], 0.0
        for t in self.resolved:
            running += self.net_r(t)
            curve.append(running)
        return curve

    @property
    def max_drawdown_r(self) -> float:
        peak, worst = 0.0, 0.0
        for value in self.equity_curve:
            peak = max(peak, value)
            worst = min(worst, value - peak)
        return abs(worst)

    @property
    def stdev_r(self) -> float | None:
        rs = [self.net_r(t) for t in self.resolved]
        return statistics.pstdev(rs) if len(rs) > 1 else None

    def summary(self) -> dict[str, Any]:
        """Every figure carries the sample size it was computed from."""
        return {
            "label": self.label,
            "bars_tested": self.bars_tested,
            "trades": len(self.trades),
            "resolved": len(self.resolved),
            "unresolved": self.unresolved_count,
            "wins": len(self.wins),
            "losses": len(self.losses),
            "win_rate": self.win_rate,
            "expectancy_r": self.expectancy_r,
            "total_r": self.total_r,
            "profit_factor": self.profit_factor,
            "max_drawdown_r": self.max_drawdown_r,
            "stdev_r": self.stdev_r,
            "cost_r": self.cost_r,
            "measures": "underlying signal only — NOT options P&L",
            "sample_warning": (
                "sample too small for statistical inference"
                if len(self.resolved) < 30 else None
            ),
        }


def _resolve(bars: Sequence[Bar], start: int, direction: str,
             stop: float, target: float) -> tuple[Exit, int]:
    """Walk forward from `start` and find which level is touched first.

    Only bars at index > start are consulted: the entry bar itself cannot
    resolve the trade it created.
    """
    for offset, bar in enumerate(bars[start + 1:], start=1):
        if direction == "BUY":
            hit_stop, hit_target = bar.l <= stop, bar.h >= target
        else:
            hit_stop, hit_target = bar.h >= stop, bar.l <= target
        if hit_stop:
            # Pessimistic on an ambiguous bar: OHLC cannot tell us which came
            # first, and assuming the target is how a backtest flatters itself.
            return Exit.STOP, offset
        if hit_target:
            return Exit.TARGET, offset
    return Exit.UNRESOLVED, len(bars) - start - 1


def run_backtest(
    bars: Sequence[Bar],
    strategy: Any,
    *,
    snapshot_builder: Any,
    warmup: int,
    cost_r: float = 0.0,
    label: str = "",
    max_trades: int | None = None,
) -> BacktestResult:
    """Walk the series forward one bar at a time.

    `snapshot_builder(slice_of_bars)` must build a snapshot from ONLY the bars it
    is given. That signature is the look-ahead guarantee: the function is never
    handed the future.
    """
    if warmup < 1:
        raise BacktestError(f"warmup must be >= 1, got {warmup}")
    if cost_r < 0:
        raise BacktestError(f"cost_r must not be negative, got {cost_r}")
    if len(bars) <= warmup:
        raise BacktestError(
            f"need more than {warmup} bars to test, got {len(bars)}. "
            "A backtest with no testable window is an error, not an empty result."
        )

    trades: list[Trade] = []
    i = warmup
    while i < len(bars):
        window = bars[:i]                      # strictly the past
        snapshot = snapshot_builder(window)
        if snapshot is None:
            i += 1
            continue
        try:
            evaluation = strategy.evaluate(snapshot)
        except Exception:
            # A strategy that raises on one window must not abort the run; the
            # window is skipped and the rest is still measured.
            i += 1
            continue

        output = getattr(evaluation, "output", None)
        if output is None:
            i += 1
            continue

        exit_kind, held = _resolve(bars, i - 1, output.direction.value,
                                   output.stop_loss, output.take_profit)
        stop_distance = abs(output.entry - output.stop_loss)
        reward = abs(output.take_profit - output.entry)
        r = {Exit.TARGET: reward / stop_distance if stop_distance else 0.0,
             Exit.STOP: -1.0, Exit.UNRESOLVED: 0.0}[exit_kind]

        trades.append(Trade(
            index=i, direction=output.direction.value, entry=output.entry,
            stop_loss=output.stop_loss, take_profit=output.take_profit,
            exit=exit_kind, bars_held=held, r_multiple=r,
            score=getattr(output, "base_score", 0.0),
        ))
        if max_trades and len(trades) >= max_trades:
            break
        # Skip past the trade so overlapping entries are not double counted.
        i += max(held, 1)

    return BacktestResult(trades=trades, bars_tested=len(bars) - warmup,
                          cost_r=cost_r, label=label)


@dataclass(frozen=True)
class WalkForwardWindow:
    index: int
    start: int
    end: int
    result: BacktestResult


def walk_forward(
    bars: Sequence[Bar], strategy: Any, *, snapshot_builder: Any,
    warmup: int, windows: int = 4, cost_r: float = 0.0,
) -> list[WalkForwardWindow]:
    """Split the series into consecutive out-of-sample windows.

    Every window is genuinely out of sample: nothing is fitted, so there are no
    parameters carried between windows. S07's constants come from the MQL5 source
    and are never tuned here — which is precisely why this is walk-forward
    VALIDATION rather than optimisation. An optimiser that chose constants per
    window would be reporting its own overfitting.
    """
    if windows < 2:
        raise BacktestError(f"walk-forward needs at least 2 windows, got {windows}")
    testable = len(bars) - warmup
    if testable < windows:
        raise BacktestError(
            f"{testable} testable bars cannot be split into {windows} windows")

    size = testable // windows
    out: list[WalkForwardWindow] = []
    for w in range(windows):
        start = w * size
        end = len(bars) if w == windows - 1 else warmup + (w + 1) * size
        segment = bars[start:end]
        if len(segment) <= warmup:
            continue
        out.append(WalkForwardWindow(
            index=w, start=start, end=end,
            result=run_backtest(segment, strategy,
                                snapshot_builder=snapshot_builder,
                                warmup=warmup, cost_r=cost_r,
                                label=f"window {w + 1}/{windows}"),
        ))
    return out


def cost_sensitivity(
    bars: Sequence[Bar], strategy: Any, *, snapshot_builder: Any, warmup: int,
    cost_levels: Sequence[float] = (0.0, 0.05, 0.10, 0.25, 0.50),
) -> list[dict[str, Any]]:
    """Re-score the SAME trades at several round-trip cost assumptions.

    The trades are found once and re-scored, not re-simulated: cost does not
    change which signals fired, only what they were worth. Re-running the walk
    per level would imply cost affects entry, which it does not here.

    The point is to show where the edge dies. An expectancy that survives 0.0R
    of cost and not 0.05R is not an edge, it is a rounding error, and this makes
    that visible instead of quoting the zero-cost number alone.
    """
    base = run_backtest(bars, strategy, snapshot_builder=snapshot_builder,
                        warmup=warmup, cost_r=0.0)
    rows: list[dict[str, Any]] = []
    for level in cost_levels:
        scored = BacktestResult(trades=base.trades, bars_tested=base.bars_tested,
                                cost_r=level, label=f"cost {level}R")
        rows.append({
            "cost_r": level,
            "expectancy_r": scored.expectancy_r,
            "total_r": scored.total_r,
            "profit_factor": scored.profit_factor,
            "max_drawdown_r": scored.max_drawdown_r,
            "still_positive": (scored.expectancy_r or 0) > 0,
        })
    return rows


@dataclass(frozen=True)
class BacktestReport:
    """The full record, reproducible from a fixed dataset and configuration."""
    overall: BacktestResult
    walk_forward: list[WalkForwardWindow] = field(default_factory=list)
    sensitivity: list[dict[str, Any]] = field(default_factory=list)
    dataset_digest: str = ""
    config: dict[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        return {
            "disclaimer": (
                "Measures the UNDERLYING S07 signal on historical bars. This is "
                "NOT options P&L and is NOT evidence of future profitability. "
                "Options results would require historical option chains, which "
                "are not available here; pricing them with a model would measure "
                "the model rather than the strategy."
            ),
            "dataset_digest": self.dataset_digest,
            "config": dict(self.config),
            "overall": self.overall.summary(),
            "walk_forward": [
                {"window": w.index, "start": w.start, "end": w.end,
                 **w.result.summary()} for w in self.walk_forward
            ],
            "cost_sensitivity": list(self.sensitivity),
        }

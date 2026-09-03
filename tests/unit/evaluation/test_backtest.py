"""
Backtest integrity.

The metrics matter less than whether they can be trusted, so most of these test
the three ways a backtest lies: look-ahead, optimistic intrabar assumptions, and
silently dropping trades that did not resolve.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import pytest  # noqa: E402

from speedtrader.data.schemas import Bar, Direction  # noqa: E402
from speedtrader.evaluation.backtest import (  # noqa: E402
    BacktestError, BacktestReport, BacktestResult, Exit, Trade, cost_sensitivity,
    run_backtest, walk_forward,
)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def bar(i, o=100.0, h=101.0, l=99.0, c=100.0, v=1000.0):
    return Bar(t=T0 + timedelta(hours=i), o=o, h=h, l=l, c=c, v=v)


class Out:
    def __init__(self, direction, entry, stop, target):
        self.direction = Direction(direction)
        self.entry, self.stop_loss, self.take_profit = entry, stop, target
        self.base_score = 50.0


class Eval:
    def __init__(self, output):
        self.output = output


class FixedStrategy:
    """Signals on a chosen set of indices. Records what it was shown."""

    def __init__(self, at=(), direction="BUY", entry=100.0, stop=98.0, target=104.0):
        self.at, self.seen = set(at), []
        self.direction, self.entry, self.stop, self.target = direction, entry, stop, target

    def evaluate(self, snapshot):
        self.seen.append(len(snapshot))
        if len(snapshot) in self.at:
            return Eval(Out(self.direction, self.entry, self.stop, self.target))
        return Eval(None)


def builder(window):
    """The window IS the snapshot here; the strategy only sees this slice."""
    return list(window)


# ============================================ LOOK-AHEAD

def test_the_strategy_never_sees_a_bar_at_or_beyond_its_own_index():
    """The structural guarantee: bars[:i] and bars[i:] never overlap."""
    bars = [bar(i) for i in range(30)]
    s = FixedStrategy()
    run_backtest(bars, s, snapshot_builder=builder, warmup=10)
    assert s.seen, "the strategy was never invoked"
    assert max(s.seen) < len(bars), "the strategy was shown the full series"
    assert all(n >= 10 for n in s.seen), "a window shorter than warmup was used"


def test_windows_grow_strictly_and_never_include_the_future():
    bars = [bar(i) for i in range(25)]
    s = FixedStrategy()
    run_backtest(bars, s, snapshot_builder=builder, warmup=5)
    assert s.seen == sorted(s.seen)
    assert s.seen[0] == 5


def test_appending_future_bars_does_not_change_earlier_signals():
    """The definitive look-ahead test: extending the series must not alter what
    the strategy decided in the part they share."""
    short = [bar(i) for i in range(20)]
    long = short + [bar(i, h=999.0) for i in range(20, 40)]
    a, b = FixedStrategy(at={12}), FixedStrategy(at={12})
    run_backtest(short, a, snapshot_builder=builder, warmup=10)
    run_backtest(long, b, snapshot_builder=builder, warmup=10)
    assert b.seen[:len(a.seen)] == a.seen


def test_a_trade_is_not_resolved_by_a_bar_the_strategy_already_saw():
    """The classic same-bar-exit error: resolving on the bar that triggered the
    entry means exiting on information you had before you entered.

    The fixture is built so the LAST BAR OF THE WINDOW would resolve the trade
    and every later bar would not. A correct backtest therefore reports
    UNRESOLVED; one that peeks backwards reports a win.
    """
    bars = [bar(i) for i in range(10)]
    bars[9] = bar(9, h=104.5, l=99.5)        # last bar SEEN — contains the target
    bars.extend(bar(i, h=100.2, l=99.8) for i in range(10, 20))   # never resolves
    s = FixedStrategy(at={10}, entry=100.0, stop=98.0, target=104.0)
    r = run_backtest(bars, s, snapshot_builder=builder, warmup=10)
    assert r.trades, "no trade was generated"
    assert r.trades[0].exit is Exit.UNRESOLVED, (
        "the trade was resolved by a bar the strategy had already seen")
    assert r.trades[0].bars_held >= 1


# ============================================ pessimistic on ambiguity

def test_a_bar_touching_both_stop_and_target_is_scored_as_a_loss():
    """OHLC cannot reveal intrabar order, and the optimistic assumption is
    exactly how backtests flatter themselves."""
    bars = [bar(i) for i in range(11)]
    bars.append(bar(11, h=104.0, l=98.0))     # both levels inside one bar
    s = FixedStrategy(at={10}, entry=100.0, stop=98.0, target=104.0)
    r = run_backtest(bars, s, snapshot_builder=builder, warmup=10)
    assert r.trades[0].exit is Exit.STOP
    assert r.trades[0].r_multiple == -1.0


def test_a_clean_target_hit_is_a_win():
    bars = [bar(i) for i in range(11)] + [bar(11, h=104.5, l=99.5)]
    s = FixedStrategy(at={10}, entry=100.0, stop=98.0, target=104.0)
    r = run_backtest(bars, s, snapshot_builder=builder, warmup=10)
    assert r.trades[0].exit is Exit.TARGET
    assert r.trades[0].r_multiple == pytest.approx(2.0)


def test_a_short_resolves_on_the_inverted_levels():
    bars = [bar(i) for i in range(11)] + [bar(11, h=100.5, l=95.5)]
    s = FixedStrategy(at={10}, direction="SELL", entry=100.0, stop=102.0, target=96.0)
    r = run_backtest(bars, s, snapshot_builder=builder, warmup=10)
    assert r.trades[0].exit is Exit.TARGET


# ============================================ unresolved trades are not wins

def test_a_trade_that_never_resolves_is_excluded_not_counted_as_a_win():
    """Dropping it silently would bias results toward whichever side resolved."""
    bars = [bar(i) for i in range(11)] + [bar(11), bar(12)]
    s = FixedStrategy(at={10}, entry=100.0, stop=90.0, target=110.0)
    r = run_backtest(bars, s, snapshot_builder=builder, warmup=10)
    assert r.trades[0].exit is Exit.UNRESOLVED
    assert r.unresolved_count == 1
    assert r.wins == [] and r.losses == []
    assert r.win_rate is None


def test_no_trades_reports_none_not_a_zero_percent_win_rate():
    """Zero would read as a measured 0%, not as an absence of evidence."""
    bars = [bar(i) for i in range(30)]
    r = run_backtest(bars, FixedStrategy(), snapshot_builder=builder, warmup=10)
    assert r.trades == []
    assert r.win_rate is None and r.expectancy_r is None


# ============================================ statistics

def make(exits):
    trades = [Trade(index=i, direction="BUY", entry=100.0, stop_loss=98.0,
                    take_profit=104.0, exit=e, bars_held=1,
                    r_multiple={Exit.TARGET: 2.0, Exit.STOP: -1.0,
                                Exit.UNRESOLVED: 0.0}[e])
              for i, e in enumerate(exits)]
    return BacktestResult(trades=trades, bars_tested=100)


def test_expectancy_and_win_rate_are_computed_over_resolved_trades_only():
    r = make([Exit.TARGET, Exit.STOP, Exit.UNRESOLVED])
    assert r.win_rate == pytest.approx(0.5)
    assert r.expectancy_r == pytest.approx(0.5)      # (2 - 1) / 2


def test_cost_is_subtracted_from_every_trade():
    trades = make([Exit.TARGET, Exit.STOP]).trades
    free = BacktestResult(trades=trades, bars_tested=100, cost_r=0.0)
    costly = BacktestResult(trades=trades, bars_tested=100, cost_r=0.25)
    assert costly.total_r == pytest.approx(free.total_r - 0.5)


def test_max_drawdown_is_measured_from_the_running_peak():
    r = make([Exit.TARGET, Exit.STOP, Exit.STOP, Exit.TARGET])
    #   curve: 0, 2, 1, 0, 2  -> peak 2, trough 0 -> drawdown 2
    assert r.max_drawdown_r == pytest.approx(2.0)


def test_profit_factor_is_undefined_rather_than_infinite_without_losses():
    assert make([Exit.TARGET, Exit.TARGET]).profit_factor is None


def test_the_equity_curve_starts_at_zero_and_tracks_each_trade():
    r = make([Exit.TARGET, Exit.STOP])
    assert r.equity_curve == pytest.approx([0.0, 2.0, 1.0])


def test_a_small_sample_is_flagged_in_the_summary():
    """Quoting statistics from a handful of trades without saying so is the
    most common way a backtest misleads."""
    s = make([Exit.TARGET] * 5).summary()
    assert s["sample_warning"] is not None
    assert s["resolved"] == 5


def test_the_summary_states_what_it_measures():
    assert "NOT options" in make([Exit.TARGET]).summary()["measures"]


# ============================================ configuration errors

@pytest.mark.parametrize("kw", [{"warmup": 0}, {"warmup": -1}, {"cost_r": -0.1}])
def test_an_invalid_configuration_raises(kw):
    bars = [bar(i) for i in range(30)]
    args = {"snapshot_builder": builder, "warmup": 10, **kw}
    with pytest.raises(BacktestError):
        run_backtest(bars, FixedStrategy(), **args)


def test_too_little_data_is_an_error_not_an_empty_result():
    with pytest.raises(BacktestError, match="need more than"):
        run_backtest([bar(i) for i in range(5)], FixedStrategy(),
                     snapshot_builder=builder, warmup=10)


def test_a_strategy_that_raises_does_not_abort_the_run():
    class Exploding:
        def evaluate(self, snapshot):
            raise RuntimeError("bad window")
    r = run_backtest([bar(i) for i in range(30)], Exploding(),
                     snapshot_builder=builder, warmup=10)
    assert r.trades == []


# ============================================ walk-forward

def test_walk_forward_splits_into_out_of_sample_windows():
    bars = [bar(i) for i in range(120)]
    windows = walk_forward(bars, FixedStrategy(), snapshot_builder=builder,
                           warmup=10, windows=4)
    assert len(windows) == 4
    assert [w.index for w in windows] == [0, 1, 2, 3]


def test_walk_forward_windows_advance_through_the_series():
    bars = [bar(i) for i in range(120)]
    windows = walk_forward(bars, FixedStrategy(), snapshot_builder=builder,
                           warmup=10, windows=3)
    starts = [w.start for w in windows]
    assert starts == sorted(starts) and len(set(starts)) == len(starts)


@pytest.mark.parametrize("n", [0, 1])
def test_walk_forward_needs_at_least_two_windows(n):
    with pytest.raises(BacktestError):
        walk_forward([bar(i) for i in range(60)], FixedStrategy(),
                     snapshot_builder=builder, warmup=10, windows=n)


def test_walk_forward_refuses_more_windows_than_data():
    with pytest.raises(BacktestError):
        walk_forward([bar(i) for i in range(15)], FixedStrategy(),
                     snapshot_builder=builder, warmup=10, windows=20)


# ============================================ cost sensitivity

def test_sensitivity_rescores_the_same_trades_at_each_cost_level():
    """Cost does not change which signals fired, only what they were worth."""
    bars = [bar(i) for i in range(11)] + [bar(11, h=104.5, l=99.5)]
    rows = cost_sensitivity(bars, FixedStrategy(at={10}), snapshot_builder=builder,
                            warmup=10, cost_levels=(0.0, 0.5, 1.0))
    assert [r["cost_r"] for r in rows] == [0.0, 0.5, 1.0]
    assert rows[0]["expectancy_r"] > rows[1]["expectancy_r"] > rows[2]["expectancy_r"]


def test_sensitivity_shows_where_the_edge_dies():
    """An expectancy that survives zero cost and not 0.05R is a rounding error,
    and this is what makes that visible."""
    bars = [bar(i) for i in range(11)] + [bar(11, h=104.5, l=99.5)]
    rows = cost_sensitivity(bars, FixedStrategy(at={10}), snapshot_builder=builder,
                            warmup=10, cost_levels=(0.0, 5.0))
    assert rows[0]["still_positive"] is True
    assert rows[1]["still_positive"] is False


# ============================================ the report

def test_the_report_leads_with_what_it_is_not():
    report = BacktestReport(overall=make([Exit.TARGET]), dataset_digest="abc123")
    rec = report.to_record()
    assert "NOT options P&L" in rec["disclaimer"]
    assert "future profitability" in rec["disclaimer"]
    assert rec["dataset_digest"] == "abc123"


def test_the_report_is_json_serialisable():
    import json
    rec = BacktestReport(overall=make([Exit.TARGET, Exit.STOP])).to_record()
    assert json.loads(json.dumps(rec)) == rec

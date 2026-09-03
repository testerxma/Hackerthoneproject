"""
S07 acceptance tests.

Every expected value is hand-computed in the comments from the deposited source,
docs/reference/SpeedTraderBot_v6.1.mq5 L1032-1047. No TA-Lib, pandas or numpy is used
as an oracle — indicator values are INJECTED into the snapshot so that each test
isolates S07's logic from the feature engine, which Step 1 already covers.

FIXTURE GEOMETRY (used by most tests)
    25 bars, chronological. MQL5 shift s maps to index 25 - s.
        shift 1  -> index 24   the bar under test
        shift 2  -> index 23   window starts here
        shift 21 -> index 4    window ends here
        shift 22 -> index 3    history guard
    Indices 4..23 (shifts 2..21) all have high=110, low=90  ->  hh=110, ll=90
    ATR is injected as 2.0, so:
        body threshold = 1.5 * 2.0 = 3.0
        stop distance  = 1.5 * 2.0 = 3.0
        target distance= 3.0 * 2.0 = 6.0
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import pytest  # noqa: E402

from speedtrader.common.clock import Freshness  # noqa: E402
from speedtrader.common.ids import IdKind, new_id  # noqa: E402
from speedtrader.data.schemas import (  # noqa: E402
    Bar,
    DataSourceMeta,
    Direction,
    MarketSnapshot,
    TechnicalFeatures,
)
from speedtrader.quant.strategies.base import Code, Strategy, StrategyOutput  # noqa: E402
from speedtrader.quant.strategies.s07 import (  # noqa: E402
    BASE_SCORE,
    CANDLE_ATR_MULT,
    HISTORY_GUARD_SHIFT,
    STOP_ATR_MULT,
    TARGET_ATR_MULT,
    WINDOW_FIRST_SHIFT,
    WINDOW_LAST_SHIFT,
    S07MomentumBreakout,
)

T0 = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
ATR = 2.0
EMA200 = 100.0

S07 = S07MomentumBreakout()


def make_bars(
    n: int = 25,
    *,
    window_high: float = 110.0,
    window_low: float = 90.0,
    last: tuple[float, float, float, float] | None = None,
    overrides: dict[int, tuple[float, float, float, float]] | None = None,
) -> list[Bar]:
    """n chronological bars. `overrides` is keyed by MQL5 SHIFT, not index."""
    bars = [
        Bar(t=T0 + timedelta(hours=i), o=100.0, h=window_high, l=window_low,
            c=100.0, v=1000.0)
        for i in range(n)
    ]
    if last:
        o, h, lo, c = last
        bars[-1] = Bar(t=bars[-1].t, o=o, h=h, l=lo, c=c, v=1000.0)
    for shift, (o, h, lo, c) in (overrides or {}).items():
        idx = n - shift
        bars[idx] = Bar(t=bars[idx].t, o=o, h=h, l=lo, c=c, v=1000.0)
    return bars


def make_snapshot(
    bars: list[Bar],
    *,
    atr: float | None = ATR,
    ema200: float | None = EMA200,
    di_plus: float | None = 30.0,
    di_minus: float | None = 10.0,
) -> MarketSnapshot:
    return MarketSnapshot(
        snapshot_id=new_id(IdKind.SNAPSHOT),
        symbol="TEST",
        price=bars[-1].c if bars else None,
        bars=bars,
        features=TechnicalFeatures(atr=atr, ema200=ema200,
                                   di_plus=di_plus, di_minus=di_minus),
        source=DataSourceMeta(vendor="replay", fetched_at=T0,
                              bars_available=len(bars), freshness=Freshness.FRESH,
                              notes="SIMULATED DATA — not live Alpaca"),
    )


# BUY bar: open 111, close 115 -> body 4.0 > 3.0 ; price 115 > hh 110 > ema200 100
BUY_BAR = (111.0, 116.0, 110.0, 115.0)
# SELL bar: open 89, close 85 -> body 4.0 > 3.0 ; price 85 < ll 90 < ema200 100
SELL_BAR = (89.0, 90.0, 84.0, 85.0)


# ==========================================================================
# 1-2. Valid signals
# ==========================================================================

def test_1_valid_buy_signal():
    r = S07.evaluate(make_snapshot(make_bars(last=BUY_BAR)))
    assert r.ok and r.code == Code.SIGNAL
    assert r.output.direction is Direction.BUY
    assert r.output.entry == pytest.approx(115.0)
    assert r.output.strategy_id == "S07"
    assert "L1032-1047" in r.output.source_reference


def test_2_valid_sell_signal():
    r = S07.evaluate(make_snapshot(
        make_bars(last=SELL_BAR), di_plus=10.0, di_minus=30.0))
    assert r.ok and r.output.direction is Direction.SELL
    assert r.output.entry == pytest.approx(85.0)


# ==========================================================================
# 3-4. Breakout condition failures
# ==========================================================================

@pytest.mark.parametrize("close_price", [110.0, 109.9, 100.0])
def test_3_buy_fails_when_price_not_above_hh(close_price):
    """L1042 uses strict `>`. price == hh must NOT fire."""
    bars = make_bars(last=(close_price - 4.0, 116.0, 90.0, close_price))
    r = S07.evaluate(make_snapshot(bars))
    assert not r.ok and r.code == Code.NO_SIGNAL


@pytest.mark.parametrize("close_price", [90.0, 90.1, 100.0])
def test_4_sell_fails_when_price_not_below_ll(close_price):
    """L1044 uses strict `<`. price == ll must NOT fire."""
    bars = make_bars(last=(close_price + 4.0, 110.0, 84.0, close_price))
    r = S07.evaluate(make_snapshot(bars, di_plus=10.0, di_minus=30.0))
    assert not r.ok and r.code == Code.NO_SIGNAL


# ==========================================================================
# 5. Candle body threshold
# ==========================================================================

def test_5_body_equal_to_threshold_is_rejected():
    """`candle > 1.5*atr` is strict. body == 3.0 with atr 2.0 must NOT fire."""
    bars = make_bars(last=(112.0, 116.0, 110.0, 115.0))   # body = 3.0 exactly
    r = S07.evaluate(make_snapshot(bars))
    assert not r.ok
    assert "candle body" in r.reason and "<=" in r.reason


def test_5b_body_below_threshold_is_rejected():
    bars = make_bars(last=(114.0, 116.0, 110.0, 115.0))   # body = 1.0
    assert not S07.evaluate(make_snapshot(bars)).ok


def test_5c_body_is_close_minus_open_not_high_minus_low():
    """L1041 is |Cl - Op|, the body. This bar has range 26 but body 1.0."""
    bars = make_bars(last=(114.0, 130.0, 104.0, 115.0))
    r = S07.evaluate(make_snapshot(bars))
    assert not r.ok, "range was used instead of body"
    assert r.detail["candle"] == pytest.approx(1.0)


# ==========================================================================
# 6. EMA200 filter
# ==========================================================================

def test_6_buy_rejected_when_price_not_above_ema200():
    r = S07.evaluate(make_snapshot(make_bars(last=BUY_BAR), ema200=120.0))
    assert not r.ok and "EMA200" in r.reason


def test_6b_sell_rejected_when_price_not_below_ema200():
    r = S07.evaluate(make_snapshot(
        make_bars(last=SELL_BAR), ema200=80.0, di_plus=10.0, di_minus=30.0))
    assert not r.ok and "EMA200" in r.reason


def test_6c_ema200_equal_to_price_is_rejected():
    """`price > st.ema200` is strict."""
    bars = make_bars(last=BUY_BAR)
    assert not S07.evaluate(make_snapshot(bars, ema200=115.0)).ok


# ==========================================================================
# 7. DI comparison
# ==========================================================================

def test_7_buy_rejected_when_di_plus_not_above_di_minus():
    r = S07.evaluate(make_snapshot(make_bars(last=BUY_BAR),
                                   di_plus=10.0, di_minus=30.0))
    assert not r.ok and "DI+" in r.reason


def test_7b_sell_rejected_when_di_minus_not_above_di_plus():
    r = S07.evaluate(make_snapshot(make_bars(last=SELL_BAR),
                                   di_plus=30.0, di_minus=10.0))
    assert not r.ok and "DI-" in r.reason


def test_7c_equal_di_is_rejected_both_directions():
    """Both branches use strict `>`; a tie fires neither."""
    assert not S07.evaluate(make_snapshot(
        make_bars(last=BUY_BAR), di_plus=20.0, di_minus=20.0)).ok
    assert not S07.evaluate(make_snapshot(
        make_bars(last=SELL_BAR), di_plus=20.0, di_minus=20.0)).ok


# ==========================================================================
# 8-11. Stop and target arithmetic
# ==========================================================================

def test_8_9_buy_stop_and_target():
    # price 115, atr 2.0
    #   sl = 115 - 1.5*2.0 = 112.0
    #   tp = 115 + 3.0*2.0 = 121.0
    out = S07.evaluate(make_snapshot(make_bars(last=BUY_BAR))).output
    assert out.stop_loss == pytest.approx(112.0)
    assert out.take_profit == pytest.approx(121.0)


def test_10_11_sell_stop_and_target():
    # price 85, atr 2.0
    #   sl = 85 + 1.5*2.0 = 88.0
    #   tp = 85 - 3.0*2.0 = 79.0
    out = S07.evaluate(make_snapshot(
        make_bars(last=SELL_BAR), di_plus=10.0, di_minus=30.0)).output
    assert out.stop_loss == pytest.approx(88.0)
    assert out.take_profit == pytest.approx(79.0)


@pytest.mark.parametrize("atr", [0.5, 2.0, 7.25, 40.0])
def test_stop_target_scale_with_atr(atr):
    body = CANDLE_ATR_MULT * atr + 1.0
    bars = make_bars(last=(115.0 - body, 130.0, 110.0, 115.0))
    out = S07.evaluate(make_snapshot(bars, atr=atr)).output
    assert out.stop_loss == pytest.approx(115.0 - 1.5 * atr)
    assert out.take_profit == pytest.approx(115.0 + 3.0 * atr)


def test_reward_risk_is_exactly_two():
    """3.0/1.5 = 2.0 by construction. W4: the R:R gate is inert for S07 and the
    MQL5 formula is authoritative — this asserts the property, it does not fix it."""
    out = S07.evaluate(make_snapshot(make_bars(last=BUY_BAR))).output
    rr = abs(out.take_profit - out.entry) / abs(out.entry - out.stop_loss)
    assert rr == pytest.approx(2.0)


# ==========================================================================
# 12. Base score
# ==========================================================================

def test_12_base_score_is_fifty_both_directions():
    buy = S07.evaluate(make_snapshot(make_bars(last=BUY_BAR))).output
    sell = S07.evaluate(make_snapshot(
        make_bars(last=SELL_BAR), di_plus=10.0, di_minus=30.0)).output
    assert buy.base_score == 50.0 and sell.base_score == 50.0
    assert BASE_SCORE == 50.0
    assert buy.breakdown == "base50 "     # InitSignal L893


# ==========================================================================
# 13-14. Window is exactly k=2..21, excluding shift 1
# ==========================================================================

def test_13_shift_21_is_inside_the_window():
    """Raising shift 21's high to 120 must raise hh above price 115 -> no signal."""
    bars = make_bars(last=BUY_BAR, overrides={21: (100.0, 120.0, 90.0, 100.0)})
    r = S07.evaluate(make_snapshot(bars))
    assert not r.ok, "shift 21 was excluded from the window"
    assert r.detail["hh"] == pytest.approx(120.0)


def test_13b_shift_22_is_outside_the_window():
    """Shift 22 at high 999 must NOT affect hh — the signal still fires."""
    bars = make_bars(last=BUY_BAR, overrides={22: (100.0, 999.0, 90.0, 100.0)})
    r = S07.evaluate(make_snapshot(bars))
    assert r.ok, "shift 22 leaked into the window"
    assert r.output.inputs["hh"] == pytest.approx(110.0)   # not 999


def test_13c_shift_2_is_inside_the_window():
    bars = make_bars(last=BUY_BAR, overrides={2: (100.0, 120.0, 90.0, 100.0)})
    r = S07.evaluate(make_snapshot(bars))
    assert not r.ok and r.detail["hh"] == pytest.approx(120.0)


def test_14_shift_1_is_excluded_from_hh_and_ll():
    """The decisive test. The bar under test has high 999 and low 1.

    If the window were k=1..21, hh would be 999 and price 115 could never break out.
    A signal proves shift 1 is excluded — the bar is not compared against itself.
    """
    bars = make_bars(last=(111.0, 999.0, 1.0, 115.0))
    r = S07.evaluate(make_snapshot(bars))
    assert r.ok, "shift 1 was included in the breakout window"
    assert r.output.inputs["hh"] == pytest.approx(110.0)   # not 999
    assert r.output.inputs["ll"] == pytest.approx(90.0)    # not 1


def test_window_constants_match_source():
    assert (WINDOW_FIRST_SHIFT, WINDOW_LAST_SHIFT) == (2, 21)
    assert HISTORY_GUARD_SHIFT == 22
    assert (STOP_ATR_MULT, TARGET_ATR_MULT, CANDLE_ATR_MULT) == (1.5, 3.0, 1.5)


def test_window_spans_exactly_twenty_bars():
    assert WINDOW_LAST_SHIFT - WINDOW_FIRST_SHIFT + 1 == 20


# ==========================================================================
# 15-16. Closed-candle indexing and look-ahead
# ==========================================================================

def test_15_entry_is_close_of_newest_closed_bar():
    """L1038 price = Cl(i,1); Series maps shift 1 to bars[-1]."""
    bars = make_bars(last=BUY_BAR)
    out = S07.evaluate(make_snapshot(bars)).output
    assert out.entry == pytest.approx(bars[-1].c)
    assert out.inputs["open_1"] == pytest.approx(bars[-1].o)


def test_16_appending_a_future_bar_does_not_change_the_prior_decision():
    """Re-evaluating the same prefix must give an identical result regardless of
    what comes after it."""
    bars = make_bars(last=BUY_BAR)
    a = S07.evaluate(make_snapshot(bars)).output
    future = Bar(t=bars[-1].t + timedelta(hours=1), o=115.0, h=500.0,
                 l=114.0, c=490.0, v=1.0)
    b = S07.evaluate(make_snapshot(bars + [future])).output
    assert b.entry != a.entry            # the newest closed bar has moved on
    c = S07.evaluate(make_snapshot(bars)).output
    assert (c.entry, c.stop_loss, c.take_profit) == (a.entry, a.stop_loss, a.take_profit)


def test_16b_strategy_never_reads_an_unclosed_bar():
    """Structural: Series.at(0) raises, so shift 0 is unreachable from S07."""
    from speedtrader.quant.features import InsufficientHistory, Series
    with pytest.raises(InsufficientHistory, match="forming bar"):
        Series(make_bars()).at(0)


# ==========================================================================
# 17-19. Guards
# ==========================================================================

def test_17_insufficient_history_rejected():
    """L1037: the bar at shift 22 must exist. 21 bars is not enough."""
    r = S07.evaluate(make_snapshot(make_bars(21, last=BUY_BAR)))
    assert not r.ok and r.code == Code.INSUFFICIENT_HISTORY
    assert r.detail == {"need_shift": 22, "bars": 21}


def test_17b_exactly_twenty_two_bars_is_sufficient():
    r = S07.evaluate(make_snapshot(make_bars(22, last=BUY_BAR)))
    assert r.ok, "the guard requires shift 22 to exist, and with 22 bars it does"


def test_17c_empty_bars_rejected():
    r = S07.evaluate(make_snapshot([]))
    assert not r.ok and r.code == Code.INSUFFICIENT_HISTORY


@pytest.mark.parametrize("atr", [0.0, -1.0, None])
def test_18_atr_unavailable_or_non_positive_rejected(atr):
    """L1036: if(st.atr<=0) return s;  None is the Python-only case."""
    r = S07.evaluate(make_snapshot(make_bars(last=BUY_BAR), atr=atr))
    assert not r.ok and r.code == Code.ATR_UNAVAILABLE


def test_18b_atr_guard_is_checked_before_history():
    """Source order: ATR guard L1036, then history guard L1037."""
    r = S07.evaluate(make_snapshot(make_bars(5, last=BUY_BAR), atr=0.0))
    assert r.code == Code.ATR_UNAVAILABLE


@pytest.mark.parametrize("kw", [
    {"ema200": None}, {"di_plus": None}, {"di_minus": None},
])
def test_19_missing_indicator_fails_closed(kw):
    """Deviation 1: MQL5 would hold 0.0; `price > 0.0` is true for every equity and
    would silently pass the EMA200 filter. None must not become 0.0."""
    r = S07.evaluate(make_snapshot(make_bars(last=BUY_BAR), **kw))
    assert not r.ok and r.code == Code.INDICATOR_UNAVAILABLE


# ==========================================================================
# 20. Contract and determinism
# ==========================================================================

def test_20_satisfies_the_strategy_protocol():
    assert isinstance(S07, Strategy)
    assert S07.id == "S07" and S07.min_bars == 22


def test_20b_no_signal_returns_a_result_not_an_exception():
    r = S07.evaluate(make_snapshot(make_bars()))
    assert r.ok is False and r.output is None
    assert bool(r) is False
    assert isinstance(r.reason, str) and r.reason


def test_20c_every_rejection_carries_a_stable_code():
    cases = [
        (make_snapshot(make_bars(last=BUY_BAR), atr=0.0), Code.ATR_UNAVAILABLE),
        (make_snapshot(make_bars(5)), Code.INSUFFICIENT_HISTORY),
        (make_snapshot(make_bars(last=BUY_BAR), ema200=None),
         Code.INDICATOR_UNAVAILABLE),
        (make_snapshot(make_bars()), Code.NO_SIGNAL),
    ]
    for snap, code in cases:
        assert S07.evaluate(snap).code == code


def test_20d_deterministic_across_repeated_evaluation():
    snap = make_snapshot(make_bars(last=BUY_BAR))
    results = {
        (r.ok, r.output.direction, r.output.entry, r.output.stop_loss,
         r.output.take_profit, r.output.base_score)
        for r in (S07.evaluate(snap) for _ in range(50))
    }
    assert len(results) == 1


def test_20e_output_is_not_a_candidate_signal():
    """Scope guard: a strategy must not acquire signal_id, scores or EV."""
    out = S07.evaluate(make_snapshot(make_bars(last=BUY_BAR))).output
    assert isinstance(out, StrategyOutput)
    for forbidden in ("signal_id", "snapshot_id", "total_score",
                      "expected_value", "expires_at", "bonus"):
        assert not hasattr(out, forbidden), f"S07 leaked {forbidden}"


def test_20f_output_is_immutable():
    out = S07.evaluate(make_snapshot(make_bars(last=BUY_BAR))).output
    with pytest.raises(Exception):
        out.entry = 999.0

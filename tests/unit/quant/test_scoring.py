"""
Scoring tests. Every expected value hand-computed from
docs/reference/SpeedTraderBot_v6.1.mq5 (SHA-256 c799acaa...32e8d9).
No TA-Lib / pandas / numpy oracle.
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
    Bar, DataSourceMeta, Direction, MarketSnapshot, TechnicalFeatures,
)
from speedtrader.quant.features import Series  # noqa: E402
from speedtrader.quant.scoring import (  # noqa: E402
    DEFERRED_BONUSES, EXCLUDED_BONUSES, SCORE_BONUS_CAP, SCORE_BONUS_FLOOR,
    _Accumulator, avg_volume, candle_quality_penalty, score_signal,
    strategy_class, volume_bonus, wick_penalty,
)
from speedtrader.quant.strategies.base import StrategyOutput  # noqa: E402

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def bars(*ohlcv):
    return [Bar(t=T0 + timedelta(hours=i), o=o, h=h, l=l, c=c, v=v)
            for i, (o, h, l, c, v) in enumerate(ohlcv)]


def one(o, h, l, c, v=1000.0):
    """A series whose shift 1 is the bar under test."""
    return Series(bars((o, h, l, c, v)))


def snap(bar_list):
    return MarketSnapshot(
        snapshot_id=new_id(IdKind.SNAPSHOT), symbol="TEST",
        price=bar_list[-1].c, bars=bar_list,
        features=TechnicalFeatures(atr=2.0, ema200=100.0, di_plus=30.0, di_minus=10.0),
        source=DataSourceMeta(vendor="replay", fetched_at=T0,
                              bars_available=len(bar_list), freshness=Freshness.FRESH))


# ================================================== StrategyClass  L331-340

def test_strategy_class_s07_is_momentum_breakout():
    """stratIdx 6 -> s+1 == 7 -> case 7 -> class 2. This is why Wick applies."""
    assert strategy_class(6) == 2


@pytest.mark.parametrize("idx,cls", [
    (0, 0), (1, 0), (5, 0),      # s+1 = 1,2,6  mean reversion
    (2, 1), (4, 1), (8, 1),      # s+1 = 3,5,9  trend
    (6, 2), (9, 2), (12, 2),     # s+1 = 7,10,13 momentum/breakout
    (3, 3), (7, 3), (13, 3),     # default     structural
])
def test_strategy_class_full_table(idx, cls):
    assert strategy_class(idx) == cls


# ================================================== Candle  L755-764

def test_candle_zero_range_returns_floor():
    # rng = h - l = 0  ->  -10   (L758)
    assert candle_quality_penalty(one(10, 10, 10, 10), Direction.BUY) == -10.0


def test_candle_body_ratio_below_030_gives_minus_8():
    # rng = 10, body = 2, br = 0.20 < 0.30  ->  -8   (L760)
    assert candle_quality_penalty(one(100, 110, 100, 102), Direction.BUY) == -8.0


def test_candle_body_ratio_below_050_gives_minus_4():
    # rng = 10, body = 4, br = 0.40  ->  -4   (L760 else-branch)
    assert candle_quality_penalty(one(100, 110, 100, 104), Direction.BUY) == -4.0


def test_candle_healthy_body_gives_zero():
    # rng = 10, body = 8, br = 0.80, close > open on a BUY -> no penalty
    assert candle_quality_penalty(one(100, 110, 100, 108), Direction.BUY) == 0.0


def test_candle_adverse_close_on_buy_gives_minus_4():
    # rng = 10, body = 7, br = 0.70 > 0.6, c < o on a BUY  ->  -4   (L761)
    assert candle_quality_penalty(one(108, 110, 100, 101), Direction.BUY) == -4.0


def test_candle_adverse_close_on_sell_gives_minus_4():
    # br = 0.70 > 0.6, c > o on a SELL  ->  -4   (L762)
    assert candle_quality_penalty(one(101, 110, 100, 108), Direction.SELL) == -4.0


def test_candle_floor_is_minus_10_when_penalties_stack():
    # br = 0.20 -> -8 ... but br must exceed 0.6 for the adverse term, so the
    # stacking path is unreachable by construction. The floor still holds.
    v = candle_quality_penalty(one(102, 110, 100, 100), Direction.BUY)
    assert v >= -10.0


def test_candle_is_never_positive():
    for o, h, l, c in [(100, 110, 100, 108), (100, 101, 99, 100.5),
                       (105, 110, 100, 101), (100, 100, 100, 100)]:
        for d in (Direction.BUY, Direction.SELL):
            assert candle_quality_penalty(one(o, h, l, c), d) <= 0.0


# ================================================== Wick  L767-776

def test_wick_zero_body_gives_minus_4():
    # body = 0  ->  -4   (L771)
    assert wick_penalty(one(100, 110, 90, 100), Direction.BUY) == -4.0


def test_wick_long_upper_on_buy_gives_minus_5():
    # o=100 c=104 body=4 ; upper = 110 - 104 = 6 > 4  ->  -5   (L773)
    assert wick_penalty(one(100, 110, 99, 104), Direction.BUY) == -5.0


def test_wick_long_lower_on_sell_gives_minus_5():
    # o=104 c=100 body=4 ; lower = 100 - 94 = 6 > 4  ->  -5   (L774)
    assert wick_penalty(one(104, 105, 94, 100), Direction.SELL) == -5.0


def test_wick_short_wick_gives_zero():
    # o=100 c=108 body=8 ; upper = 110 - 108 = 2 < 8  ->  0
    assert wick_penalty(one(100, 110, 99, 108), Direction.BUY) == 0.0


def test_wick_upper_equal_to_body_is_not_penalised():
    """L773 uses strict `>`."""
    # body = 4, upper = 110 - 104 = 6 ... construct exact equality instead:
    # o=100 c=104 body=4 ; h=108 -> upper = 4, not > 4
    assert wick_penalty(one(100, 108, 99, 104), Direction.BUY) == 0.0


def test_wick_is_never_positive():
    for d in (Direction.BUY, Direction.SELL):
        for o, h, l, c in [(100, 110, 99, 104), (104, 105, 94, 100),
                           (100, 110, 90, 100), (100, 110, 99, 108)]:
            assert wick_penalty(one(o, h, l, c), d) <= 0.0


# ================================================== Volume  L504-509, L818-823

def test_avg_volume_window_starts_at_shift_1():
    """L507: for(k=1;k<=count;k++) — the bar under test is in its own average."""
    b = bars(*[(100, 101, 99, 100, 100.0)] * 4, (100, 101, 99, 100, 500.0))
    # shifts 1..5 = volumes 500,100,100,100,100 -> mean = 180
    assert avg_volume(Series(b), 5) == pytest.approx(180.0)


def test_volume_ratio_above_1_5_gives_plus_8():
    # 19 bars at 100, newest at 400 -> avg = (400 + 19*100)/20 = 115
    # ratio = 400/115 = 3.478 > 1.5  ->  +8
    b = bars(*[(100, 101, 99, 100, 100.0)] * 19, (100, 101, 99, 100, 400.0))
    assert volume_bonus(Series(b)) == 8.0


def test_volume_ratio_between_1_2_and_1_5_gives_plus_4():
    # newest 130, others 100 -> avg = (130 + 19*100)/20 = 101.5
    # ratio = 130/101.5 = 1.281 -> in (1.2, 1.5]  ->  +4
    b = bars(*[(100, 101, 99, 100, 100.0)] * 19, (100, 101, 99, 100, 130.0))
    assert volume_bonus(Series(b)) == 4.0


def test_volume_ratio_below_1_2_gives_zero():
    b = bars(*[(100, 101, 99, 100, 100.0)] * 19, (100, 101, 99, 100, 105.0))
    assert volume_bonus(Series(b)) == 0.0


def test_volume_zero_average_gives_zero():
    """L820: if(avg<=0) return;"""
    b = bars(*[(100, 101, 99, 100, 0.0)] * 20)
    assert volume_bonus(Series(b)) == 0.0


# ================================================== Framework  L781-789

def test_zero_points_produces_no_breakdown_entry():
    """L783: if(pts==0.0) return;"""
    a = _Accumulator()
    a.add("Nothing", 0.0)
    assert a.parts == [] and a.components == {} and a.bonus == 0.0


def test_bonus_cap_is_plus_30():
    a = _Accumulator()
    a.add("A", 25.0)
    a.add("B", 25.0)
    assert a.bonus == SCORE_BONUS_CAP == 30.0


def test_breakdown_records_applied_not_requested_amount():
    """L785-787: a bonus partially absorbed by the cap logs only what landed."""
    a = _Accumulator()
    a.add("A", 25.0)
    a.add("B", 25.0)     # only +5 fits
    assert a.components["B"] == 5.0
    assert "B+5 " in "".join(a.parts)


def test_bonus_floor_is_minus_50():
    a = _Accumulator()
    for _ in range(20):
        a.add("Pen", -10.0)
    assert a.bonus == SCORE_BONUS_FLOOR == -50.0


# ================================================== score_signal

def out(direction=Direction.BUY, base=50.0):
    return StrategyOutput(strategy_id="S07", direction=direction, entry=115.0,
                          stop_loss=112.0, take_profit=121.0, base_score=base,
                          breakdown="base50 ", source_reference="L1032-1047")


def test_clean_signal_scores_base_plus_volume():
    b = bars(*[(100, 101, 99, 100, 100.0)] * 19, (100, 110, 100, 108, 400.0))
    r = score_signal(out(), snap(b), mql5_strategy_index=6)
    # candle: rng=10 body=8 br=0.8 -> 0 ; wick: upper=2 < body=8 -> 0 ; vol -> +8
    assert r.bonus == 8.0 and r.total_score == 58.0


def test_penalised_signal_falls_below_min_score_48():
    """The gate is LIVE: a real S07 signal can now be rejected on score."""
    b = bars(*[(100, 101, 99, 100, 100.0)] * 19, (100, 110, 100, 104, 100.0))
    r = score_signal(out(), snap(b), mql5_strategy_index=6)
    # candle: br = 4/10 = 0.4 -> -4 ; wick: upper = 110-104 = 6 > body 4 -> -5
    assert r.bonus == -9.0 and r.total_score == 41.0
    assert r.total_score < 48.0


def test_wick_applied_for_s07_but_not_for_a_class_0_strategy():
    b = bars(*[(100, 101, 99, 100, 100.0)] * 19, (100, 110, 99, 104, 100.0))
    s07 = score_signal(out(), snap(b), mql5_strategy_index=6)   # class 2
    s01 = score_signal(out(), snap(b), mql5_strategy_index=0)   # class 0
    assert "Wick" in s07.components
    assert "Wick" not in s01.components


def test_breakdown_starts_with_base_and_names_deferred_bonuses():
    b = bars(*[(100, 101, 99, 100, 100.0)] * 19, (100, 110, 100, 108, 400.0))
    r = score_signal(out(), snap(b), mql5_strategy_index=6)
    assert r.breakdown.startswith("base50 ")
    assert "Vol+8 " in r.breakdown
    for name in DEFERRED_BONUSES:
        assert name in r.breakdown


def test_total_score_is_base_plus_bonus():
    b = bars(*[(100, 101, 99, 100, 100.0)] * 19, (100, 110, 100, 104, 100.0))
    r = score_signal(out(), snap(b), mql5_strategy_index=6)
    assert r.total_score == r.base_score + r.bonus


def test_excluded_fx_bonuses_are_declared_and_never_applied():
    b = bars(*[(100, 101, 99, 100, 100.0)] * 19, (100, 110, 100, 108, 400.0))
    r = score_signal(out(), snap(b), mql5_strategy_index=6)
    assert set(EXCLUDED_BONUSES) == {"Basket", "Swap", "Overlap"}
    for name in EXCLUDED_BONUSES + DEFERRED_BONUSES:
        assert name not in r.components


def test_scoring_is_deterministic():
    b = bars(*[(100, 101, 99, 100, 100.0)] * 19, (100, 110, 100, 104, 250.0))
    s = snap(b)
    vals = {(score_signal(out(), s, mql5_strategy_index=6).total_score) for _ in range(50)}
    assert len(vals) == 1

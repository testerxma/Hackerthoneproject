"""
Tests for the feature engine.

EVERY EXPECTED VALUE IN THIS FILE IS COMPUTED BY HAND IN THE COMMENTS.
No pandas, no TA-Lib, no numpy, no cross-library comparison. A library agreeing with
us would only prove we match that library's conventions, and the conventions are
exactly what differs between MT5 and everything else: MT5 seeds EMA with price[0]
rather than an SMA, and MT5's ADX normalises DM by TR per bar before smoothing.
Matching a library here would mean failing the port.

INDEXING: `bars` is chronological (oldest first). MQL5 shift 1 == bars[-1].
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import pytest  # noqa: E402

from speedtrader.data.schemas import Bar  # noqa: E402
from speedtrader.quant.features import (  # noqa: E402
    FeatureEngine,
    InsufficientHistory,
    Series,
    adx,
    atr,
    ema,
    ema_warmup_bars,
    sma,
    true_range,
)

T0 = datetime(2026, 9, 1, 9, 30, tzinfo=timezone.utc)


def mk(*ohlc: tuple[float, float, float, float], vol: float = 1000.0) -> list[Bar]:
    """Build bars from (o, h, l, c) tuples, one hour apart."""
    return [
        Bar(t=T0 + timedelta(hours=i), o=o, h=h, l=lo, c=c, v=vol)
        for i, (o, h, lo, c) in enumerate(ohlc)
    ]


# ==========================================================================
# SMA
# ==========================================================================

def test_sma_hand_computed():
    # values 1..5, period 3
    #   idx2 = (1+2+3)/3 = 2.0
    #   idx3 = (2+3+4)/3 = 3.0
    #   idx4 = (3+4+5)/3 = 4.0
    r = sma([1, 2, 3, 4, 5], 3)
    assert r[0] is None and r[1] is None
    assert r[2] == pytest.approx(2.0)
    assert r[3] == pytest.approx(3.0)
    assert r[4] == pytest.approx(4.0)


def test_sma_insufficient_returns_all_none():
    assert sma([1, 2], 5) == [None, None]


# ==========================================================================
# EMA — MT5 convention: seed with values[0], pr = 2/(period+1)
# ==========================================================================

def test_ema_hand_computed_period_2():
    # pr = 2/3
    #   ema[0] = 10                                    (MT5 seeds with the price)
    #   ema[1] = 11*(2/3) + 10*(1/3)     = 10.666666...
    #   ema[2] = 12*(2/3) + 10.666667*(1/3) = 11.555555...
    r = ema([10.0, 11.0, 12.0], 2)
    assert r[0] == pytest.approx(10.0)
    assert r[1] == pytest.approx(10.0 + 2.0 / 3.0)
    assert r[2] == pytest.approx(12.0 * (2 / 3) + (10.0 + 2 / 3) * (1 / 3))


def test_ema_seeds_with_first_price_not_sma():
    """The MT5 divergence, asserted explicitly.

    An SMA-seeded EMA of [100, 100, 100, 200] period 4 would emit nothing until
    index 3. MT5 emits a value at index 0 equal to the first price.
    """
    r = ema([100.0, 100.0, 100.0, 200.0], 4)
    assert r[0] == pytest.approx(100.0)     # not None, not an SMA
    assert all(v is not None for v in r)
    # pr = 2/5 = 0.4;  ema[3] = 200*0.4 + 100*0.6 = 140
    assert r[3] == pytest.approx(140.0)


def test_ema_constant_series_is_flat():
    assert all(v == pytest.approx(50.0) for v in ema([50.0] * 20, 5))


def test_ema_warmup_bars_matches_decay_math():
    """891 bars for EMA200 is not a guess: (1-2/201)^n < 0.001."""
    n = ema_warmup_bars(200)
    alpha = 2.0 / 201.0
    assert (1 - alpha) ** (n - 200) < 0.001
    assert (1 - alpha) ** (n - 200 - 1) >= 0.001    # tight, not merely sufficient
    # And the failure this constant prevents: at 300 bars ~5% of seed error survives.
    assert 0.04 < (1 - alpha) ** 300 < 0.06


def test_ema_rejects_bad_period():
    with pytest.raises(ValueError):
        ema([1.0, 2.0], 0)


# ==========================================================================
# True Range
# ==========================================================================

def test_true_range_hand_computed():
    bars = mk(
        (9.0, 10.0, 8.0, 9.0),      # idx0 -> None, no previous close
        (9.0, 12.0, 9.0, 11.0),     # max(|12-9|=3, |12-9|=3, |9-9|=0)   = 3
        (11.0, 13.0, 11.0, 12.0),   # max(|13-11|=2,|13-11|=2,|11-11|=0) = 2
        (12.0, 12.0, 10.0, 10.0),   # max(|12-10|=2,|12-12|=0,|10-12|=2) = 2
    )
    tr = true_range(bars)
    assert tr[0] is None
    assert tr[1] == pytest.approx(3.0)
    assert tr[2] == pytest.approx(2.0)
    assert tr[3] == pytest.approx(2.0)


def test_true_range_first_bar_is_none_not_high_minus_low():
    """Substituting (H-L) for the undefined first TR biases the ATR seed."""
    assert true_range(mk((9.0, 10.0, 8.0, 9.0)))[0] is None


def test_true_range_uses_gap_across_previous_close():
    # prev close 10; bar gaps up: H=20 L=19  -> max(1, |20-10|=10, |19-10|=9) = 10
    bars = mk((10.0, 10.0, 10.0, 10.0), (19.5, 20.0, 19.0, 19.5))
    assert true_range(bars)[1] == pytest.approx(10.0)


# ==========================================================================
# ATR — MT5: seed = SMA of first `period` TRs, then Wilder smoothing
# ==========================================================================

def test_atr_hand_computed_period_2():
    # TR (from the test above) = [None, 3, 2, 2]
    #   seed at idx2 = mean(TR[1..2]) = (3+2)/2 = 2.5
    #   idx3 = 2.5 + (2 - 2.5)/2 = 2.25
    bars = mk(
        (9.0, 10.0, 8.0, 9.0),
        (9.0, 12.0, 9.0, 11.0),
        (11.0, 13.0, 11.0, 12.0),
        (12.0, 12.0, 10.0, 10.0),
    )
    a = atr(bars, period=2)
    assert a[0] is None and a[1] is None
    assert a[2] == pytest.approx(2.5)
    assert a[3] == pytest.approx(2.25)


def test_atr_uses_wilder_alpha_not_ema_alpha():
    """Wilder alpha = 1/n. An EMA would use 2/(n+1). For n=2 that is 0.5 vs 0.667.

    seed 2.5, next TR 2:
        Wilder: 2.5 + (2-2.5)/2       = 2.25
        EMA:    2*(2/3) + 2.5*(1/3)   = 2.1667
    """
    bars = mk(
        (9.0, 10.0, 8.0, 9.0),
        (9.0, 12.0, 9.0, 11.0),
        (11.0, 13.0, 11.0, 12.0),
        (12.0, 12.0, 10.0, 10.0),
    )
    a = atr(bars, period=2)
    assert a[3] == pytest.approx(2.25)
    assert a[3] != pytest.approx(2.0 * (2 / 3) + 2.5 * (1 / 3))


def test_atr_insufficient_history_all_none():
    bars = mk((1.0, 2.0, 1.0, 1.5), (1.5, 2.0, 1.0, 1.8))
    assert all(v is None for v in atr(bars, period=14))


# ==========================================================================
# ADX / DI — MT5 algorithm
# ==========================================================================

def test_adx_di_hand_computed_rising_series():
    # bars: (o,h,l,c)
    #   b0 H=10 L=8  C=9
    #   b1 H=11 L=9  C=10   dP = 11-10 = 1 ; dN = 8-9 = -1 -> 0 ; dP>dN so dN=0
    #                       tr = max(|11-9|=2, |11-9|=2, |9-9|=0) = 2
    #                       rawP = 100*1/2 = 50 ; rawN = 0
    #   b2 H=12 L=10 C=11   dP = 12-11 = 1 ; dN = 9-10 = -1 -> 0 ; dN=0
    #                       tr = max(2, |12-10|=2, |10-10|=0) = 2
    #                       rawP = 50 ; rawN = 0
    # DI+ = EMA(rawP, 2): idx1 seeds at 50 ; idx2 = 50*(2/3)+50*(1/3) = 50
    # DI- = 0 throughout
    # DX  = 100*|50-0|/50 = 100 ; ADX = EMA(DX,2) = 100
    bars = mk(
        (9.0, 10.0, 8.0, 9.0),
        (10.0, 11.0, 9.0, 10.0),
        (11.0, 12.0, 10.0, 11.0),
    )
    r = adx(bars, period=2)
    assert r.di_plus[0] is None and r.di_minus[0] is None
    assert r.di_plus[1] == pytest.approx(50.0)
    assert r.di_minus[1] == pytest.approx(0.0)
    assert r.di_plus[2] == pytest.approx(50.0)
    assert r.adx[2] == pytest.approx(100.0)


def test_adx_normalises_dm_by_tr_before_smoothing():
    """The MT5 divergence from Wilder, asserted explicitly.

    Wilder smooths +DM and TR separately then divides, so DI+ depends on the ratio
    of accumulated sums. MT5 divides per bar first. With a single directional bar the
    per-bar ratio is exactly 100*dP/tr, which is what we assert here.
    """
    bars = mk(
        (9.0, 10.0, 8.0, 9.0),
        (10.0, 14.0, 9.0, 13.0),   # dP = 4 ; tr = max(5, |14-9|=5, |9-9|=0) = 5
    )                              # rawP = 100*4/5 = 80  -> seeds DI+ at exactly 80
    r = adx(bars, period=14)
    assert r.di_plus[1] == pytest.approx(80.0)


def test_adx_equal_directional_movement_zeroes_both():
    # dP = 11-10 = 1 ; dN = 8-7 = 1 ; equal -> MT5 zeroes BOTH
    bars = mk(
        (9.0, 10.0, 8.0, 9.0),
        (9.0, 11.0, 7.0, 9.0),
    )
    r = adx(bars, period=2)
    assert r.di_plus[1] == pytest.approx(0.0)
    assert r.di_minus[1] == pytest.approx(0.0)


def test_adx_zero_true_range_does_not_divide_by_zero():
    bars = mk(
        (10.0, 10.0, 10.0, 10.0),
        (10.0, 10.0, 10.0, 10.0),
    )
    r = adx(bars, period=2)
    assert r.di_plus[1] == pytest.approx(0.0)
    assert r.di_minus[1] == pytest.approx(0.0)
    assert r.adx[1] == pytest.approx(0.0)


def test_adx_falling_series_favours_di_minus():
    bars = mk(
        (11.0, 12.0, 10.0, 11.0),
        (10.0, 11.0, 9.0, 10.0),
        (9.0, 10.0, 8.0, 9.0),
    )
    r = adx(bars, period=2)
    assert r.di_minus[2] > r.di_plus[2]
    assert r.di_plus[2] == pytest.approx(0.0)


# ==========================================================================
# Series — MQL5 shift indexing
# ==========================================================================

def test_shift_1_is_newest_closed_bar():
    """The source reads every indicator at [1] (UpdateIndicators, L614-618)."""
    bars = mk((1, 1, 1, 1), (2, 2, 2, 2), (3, 3, 3, 3))
    s = Series(bars)
    assert s.close(1) == 3.0    # newest
    assert s.close(2) == 2.0
    assert s.close(3) == 1.0


def test_shift_0_is_rejected():
    """MQL5 shift 0 is the forming bar. We hold closed bars only — guessing would
    silently shift every strategy comparison by one bar."""
    s = Series(mk((1, 1, 1, 1), (2, 2, 2, 2)))
    with pytest.raises(InsufficientHistory, match="forming bar"):
        s.at(0)


def test_shift_beyond_history_is_rejected():
    s = Series(mk((1, 1, 1, 1), (2, 2, 2, 2)))
    with pytest.raises(InsufficientHistory):
        s.at(3)


def test_exists_matches_mql5_history_guard():
    """S07 guards with `Tm(i,22)==0` before reading shifts 2..21 (source L1037)."""
    s = Series(mk(*[(1, 1, 1, 1)] * 22))
    assert s.exists(22) and not s.exists(23)
    assert not s.exists(0)


# ==========================================================================
# FeatureEngine
# ==========================================================================

def _ramp(n: int, start: float = 100.0, step: float = 0.5) -> list[Bar]:
    out = []
    for i in range(n):
        c = start + i * step
        out.append(Bar(t=T0 + timedelta(hours=i), o=c - 0.2, h=c + 0.4,
                       l=c - 0.4, c=c, v=1000.0))
    return out


def test_engine_insufficient_history_returns_none_not_zero():
    """A None indicator must never arrive downstream as 0 (§102)."""
    fs = FeatureEngine().compute(_ramp(50))
    assert fs.ema200 is None and fs.atr is None and fs.adx is None
    assert fs.bars_used == 50
    assert "insufficient history" in fs.warnings[0]


def test_engine_flags_unconverged_ema():
    fs = FeatureEngine().compute(_ramp(300))
    assert fs.ema200 is not None          # a value exists
    assert fs.ema200_converged is False   # but it is flagged as approximate
    assert "seed not fully decayed" in fs.warnings[0]


def test_engine_converged_at_recommended_bars():
    fs = FeatureEngine().compute(_ramp(FeatureEngine().recommended_bars()))
    assert fs.ema200_converged is True
    assert fs.warnings == ()
    assert fs.atr is not None and fs.di_plus is not None


def test_engine_values_correspond_to_newest_closed_bar():
    """Appending a bar must move the shift-1 values."""
    bars = _ramp(900)
    a = FeatureEngine().compute(bars)
    b = FeatureEngine().compute(bars + [Bar(
        t=T0 + timedelta(hours=900), o=500.0, h=520.0, l=499.0, c=515.0, v=9000.0)])
    assert b.ema200 != a.ema200
    assert b.atr != a.atr
    assert b.atr_prev == pytest.approx(a.atr)   # yesterday's value shifts into _prev


def test_engine_never_carries_forward():
    """Two different series must not produce identical values by reuse of state."""
    e = FeatureEngine()
    a = e.compute(_ramp(900, start=100.0, step=0.5))
    b = e.compute(_ramp(900, start=100.0, step=-0.05))
    assert a.ema200 != b.ema200
    assert a.di_plus != b.di_plus

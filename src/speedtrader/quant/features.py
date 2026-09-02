"""
SpeedTrader AI — Feature Engine
Port target: SpeedTraderBot v6.1 (SHA-256 c799acaa...32e8d9), lines 511-650

PARITY STATUS PER INDICATOR — read this before trusting any number.

    EMA   [SPEC-PARITY]   MT5 CustomMovingAverage seeds EMA with price[0], NOT an SMA.
                          Implemented that way. See _WARMUP note below.
    ATR   [SPEC-PARITY]   MT5 ATR.mq5 seeds with SMA of the first `period` TRs, then
                          Wilder smoothing. Implemented that way.
    ADX   [UNVERIFIED]    MT5's ADX.mq5 does NOT implement Wilder's textbook ADX.
    DI+/- [UNVERIFIED]    It normalises DM by TR per-bar FIRST, then applies an
                          EMA (alpha = 2/(n+1)), not Wilder smoothing (alpha = 1/n).
                          Implemented to match MT5's documented algorithm, but this
                          has NOT been numerically compared against a running MT5
                          terminal. Do not claim verified parity.

INDEXING CONVENTION — the single most important detail in this port.

MQL5 uses series indexing: shift 0 is the CURRENTLY FORMING bar, shift 1 is the last
CLOSED bar. UpdateIndicators() (source L614-618) reads every value at [1]:

    st.atr = atr[1];  st.ema200 = e200[1];  st.diPlus = dip[1];

Our `bars` list is chronological (oldest first) and contains only closed bars. So:

    MQL5 shift 1  ==  bars[-1]   (newest closed bar)
    MQL5 shift 2  ==  bars[-2]
    MQL5 shift 0  ==  NOT AVAILABLE — we never hold a forming bar

`Series.at()` enforces this mapping so strategy code can be written with the same
shift numbers as the MQL5 source and diffed against it line by line.

NO PANDAS. NO INTERPOLATION. NO CARRY-FORWARD.
Insufficient history returns None. A None indicator must never be treated as 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..data.schemas import Bar

# MT5 seeds EMA with the first price rather than an SMA, so the seed's influence
# decays as (1-alpha)^n. For EMA200 (alpha = 2/201 = 0.00995):
#     300 bars -> ~5.0% of seed error remains
#     460 bars -> ~1.0%
#     690 bars -> ~0.1%
# Anyone fetching only 300 bars for a 200-period EMA will get a materially wrong
# number and no error. This constant exists so that mistake is impossible to make
# silently.
EMA_SEED_DECAY_TARGET = 0.001


def ema_warmup_bars(period: int, target: float = EMA_SEED_DECAY_TARGET) -> int:
    """Bars needed before the EMA seed contributes less than `target` of the value."""
    import math
    alpha = 2.0 / (period + 1.0)
    return period + int(math.ceil(math.log(target) / math.log(1.0 - alpha)))


class InsufficientHistory(ValueError):
    """Raised when a caller asks for a value the data cannot support."""


# ==========================================================================
# Series — MQL5 shift indexing over a chronological list
# ==========================================================================

class Series:
    """Wraps chronological bars so strategy code can use MQL5 shift numbers."""

    __slots__ = ("_bars",)

    def __init__(self, bars: Sequence[Bar]):
        self._bars = list(bars)

    def __len__(self) -> int:
        return len(self._bars)

    def at(self, shift: int) -> Bar:
        """MQL5 shift -> Bar. shift=1 is the newest closed bar (source reads [1])."""
        if shift == 0:
            raise InsufficientHistory(
                "shift 0 is the forming bar in MQL5; SpeedTrader holds closed bars only. "
                "The source reads indicators at [1] — use shift=1 for the newest closed bar."
            )
        if shift < 0:
            raise InsufficientHistory(f"negative shift {shift}")
        idx = len(self._bars) - shift
        if idx < 0:
            raise InsufficientHistory(
                f"shift {shift} needs {shift} bars, have {len(self._bars)}"
            )
        return self._bars[idx]

    def exists(self, shift: int) -> bool:
        """MQL5 `Tm(i,22)==0` guard: does a bar exist at this shift?"""
        return 1 <= shift <= len(self._bars)

    def close(self, shift: int) -> float: return self.at(shift).c
    def open(self, shift: int) -> float: return self.at(shift).o
    def high(self, shift: int) -> float: return self.at(shift).h
    def low(self, shift: int) -> float: return self.at(shift).l
    def volume(self, shift: int) -> float: return self.at(shift).v

    @property
    def bars(self) -> list[Bar]:
        return self._bars


# ==========================================================================
# Moving averages
# ==========================================================================

def sma(values: Sequence[float], period: int) -> list[float | None]:
    if period <= 0:
        raise ValueError("period must be positive")
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    running = sum(values[:period])
    out[period - 1] = running / period
    for i in range(period, len(values)):
        running += values[i] - values[i - period]
        out[i] = running / period
    return out


def ema(values: Sequence[float], period: int) -> list[float | None]:
    """MT5 MODE_EMA. Seeded with values[0], NOT an SMA.

        pr = 2 / (period + 1)
        ema[0] = values[0]
        ema[i] = values[i] * pr + ema[i-1] * (1 - pr)

    Matches MT5's Custom Moving Average CalculateEMA. Deliberately different from
    the common convention of seeding with an SMA of the first `period` values —
    that convention would diverge from the source.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    if not values:
        return []
    pr = 2.0 / (period + 1.0)
    out: list[float | None] = [values[0]]
    prev = values[0]
    for v in values[1:]:
        prev = v * pr + prev * (1.0 - pr)
        out.append(prev)
    return out


def _ema_on_buffer(values: Sequence[float | None], period: int) -> list[float | None]:
    """MT5 ExponentialMAOnBuffer — EMA applied to an already-computed buffer.
    Used internally by MT5's ADX. Leading Nones are skipped, then seeded."""
    pr = 2.0 / (period + 1.0)
    out: list[float | None] = []
    prev: float | None = None
    for v in values:
        if v is None:
            out.append(None)
            continue
        if prev is None:
            prev = v
        else:
            prev = v * pr + prev * (1.0 - pr)
        out.append(prev)
    return out


# ==========================================================================
# True Range / ATR
# ==========================================================================

def true_range(bars: Sequence[Bar]) -> list[float | None]:
    """TR[i] = max(|H-L|, |H-prevC|, |L-prevC|).

    TR[0] is None: with no previous close the value is undefined. MT5 starts its
    TR loop at index 1 for the same reason. We return None rather than falling back
    to (H-L), because that substitution silently biases the first ATR value.
    """
    out: list[float | None] = [None]
    for i in range(1, len(bars)):
        h, l = bars[i].h, bars[i].l
        pc = bars[i - 1].c
        out.append(max(abs(h - l), abs(h - pc), abs(l - pc)))
    return out


def atr(bars: Sequence[Bar], period: int = 14) -> list[float | None]:
    """MT5 iATR. Seed = SMA of the first `period` TRs, then Wilder smoothing:

        atr[i] = atr[i-1] + (TR[i] - atr[i-1]) / period

    Equivalent to an EMA with alpha = 1/period. NOT alpha = 2/(period+1).
    """
    if period <= 0:
        raise ValueError("period must be positive")
    tr = true_range(bars)
    out: list[float | None] = [None] * len(bars)
    # TR is valid from index 1, so the seed covers indices 1..period
    if len(bars) < period + 1:
        return out
    seed = sum(tr[1: period + 1]) / period  # type: ignore[arg-type]
    out[period] = seed
    prev = seed
    for i in range(period + 1, len(bars)):
        prev = prev + (tr[i] - prev) / period  # type: ignore[operator]
        out[i] = prev
    return out


# ==========================================================================
# ADX / DI  —  MT5 algorithm, NOT Wilder's textbook version
# ==========================================================================

@dataclass(frozen=True)
class ADXResult:
    adx: list[float | None]
    di_plus: list[float | None]
    di_minus: list[float | None]


def adx(bars: Sequence[Bar], period: int = 14) -> ADXResult:
    """MT5 iADX. Buffers: 0=ADX, 1=DI+, 2=DI- (source L594-596).

    MT5's ADX.mq5 per bar i:
        dTmpP = high[i] - high[i-1],  clamped at 0
        dTmpN = low[i-1] - low[i],    clamped at 0
        whichever is smaller is zeroed; if equal, BOTH are zeroed
        tr = max(|H-L|, |H-prevC|, |L-prevC|)
        rawPDI = 100 * dTmpP / tr      (0 if tr == 0)
        rawNDI = 100 * dTmpN / tr
        DI+ = EMA(rawPDI, period)      <- EMA, alpha = 2/(n+1)
        DI- = EMA(rawNDI, period)
        DX  = 100 * |DI+ - DI-| / (DI+ + DI-)
        ADX = EMA(DX, period)

    [UNVERIFIED] This differs from Wilder's classical ADX, which smooths +DM, -DM
    and TR separately with alpha = 1/n and only then divides. The two produce
    different numbers. This implementation targets MT5 because MT5 is what produced
    the behaviour Bot v6 was tuned against — but it has not been numerically
    compared against a running terminal.

    S07 IMPACT: S07 uses only the comparison `diPlus > diMinus` (source L1042/1044),
    never an ADX threshold. A monotone-preserving difference in smoothing is far less
    likely to flip a comparison than to move an absolute level. S03 and S05 DO use
    `adx >= 20` / `adx > 25` thresholds and are therefore more exposed; verify before
    porting those.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    n = len(bars)
    if n < 2:
        return ADXResult([None] * n, [None] * n, [None] * n)

    raw_p: list[float | None] = [None]
    raw_n: list[float | None] = [None]
    for i in range(1, n):
        hi, lo = bars[i].h, bars[i].l
        prev_hi, prev_lo, prev_c = bars[i - 1].h, bars[i - 1].l, bars[i - 1].c

        tmp_p = hi - prev_hi
        tmp_n = prev_lo - lo
        if tmp_p < 0.0:
            tmp_p = 0.0
        if tmp_n < 0.0:
            tmp_n = 0.0
        if tmp_p > tmp_n:
            tmp_n = 0.0
        elif tmp_n > tmp_p:
            tmp_p = 0.0
        else:
            tmp_p = tmp_n = 0.0  # equal (incl. both zero) -> both zeroed

        tr = max(abs(hi - lo), abs(hi - prev_c), abs(lo - prev_c))
        if tr != 0.0:
            raw_p.append(100.0 * tmp_p / tr)
            raw_n.append(100.0 * tmp_n / tr)
        else:
            raw_p.append(0.0)
            raw_n.append(0.0)

    di_p = _ema_on_buffer(raw_p, period)
    di_n = _ema_on_buffer(raw_n, period)

    dx: list[float | None] = []
    for p, m in zip(di_p, di_n):
        if p is None or m is None:
            dx.append(None)
            continue
        s = p + m
        dx.append(100.0 * abs(p - m) / s if s != 0.0 else 0.0)

    return ADXResult(adx=_ema_on_buffer(dx, period), di_plus=di_p, di_minus=di_n)


# ==========================================================================
# FeatureEngine — computes the set S07 needs, at MQL5 shift 1
# ==========================================================================

@dataclass(frozen=True)
class FeatureSet:
    """Indicator values at MQL5 shift 1 (newest closed bar), plus shift 2 where the
    source keeps a previous value (st.atrPrev, source L614)."""
    ema200: float | None = None
    atr: float | None = None
    atr_prev: float | None = None
    adx: float | None = None
    di_plus: float | None = None
    di_minus: float | None = None
    bars_used: int = 0
    ema200_converged: bool = False
    warnings: tuple[str, ...] = ()


class FeatureEngine:
    """Computes indicators from a chronological bar series.

    Every returned value is either a real computed number or None. There is no
    third state: no zeros standing in for missing data, no last-known-good values
    carried forward, no interpolation across gaps.
    """

    def __init__(self, *, ema_period: int = 200, atr_period: int = 14,
                 adx_period: int = 14):
        self.ema_period = ema_period
        self.atr_period = atr_period
        self.adx_period = adx_period

    def min_bars(self) -> int:
        """Absolute minimum to produce any value. Not the same as enough for accuracy."""
        return max(self.ema_period, self.atr_period + 1, self.adx_period + 2)

    def recommended_bars(self) -> int:
        """Enough for the EMA seed to have decayed below 0.1%."""
        return ema_warmup_bars(self.ema_period)

    def compute(self, bars: Sequence[Bar]) -> FeatureSet:
        n = len(bars)
        if n < self.min_bars():
            return FeatureSet(
                bars_used=n,
                warnings=(f"insufficient history: {n} bars, need {self.min_bars()}",),
            )

        closes = [b.c for b in bars]
        ema_series = ema(closes, self.ema_period)
        atr_series = atr(bars, self.atr_period)
        adx_res = adx(bars, self.adx_period)

        warnings: list[str] = []
        converged = n >= self.recommended_bars()
        if not converged:
            warnings.append(
                f"EMA{self.ema_period} seed not fully decayed: {n} bars, "
                f"{self.recommended_bars()} recommended — value is approximate"
            )

        return FeatureSet(
            ema200=ema_series[-1],
            atr=atr_series[-1],
            atr_prev=atr_series[-2] if n >= 2 else None,
            adx=adx_res.adx[-1],
            di_plus=adx_res.di_plus[-1],
            di_minus=adx_res.di_minus[-1],
            bars_used=n,
            ema200_converged=converged,
            warnings=tuple(warnings),
        )

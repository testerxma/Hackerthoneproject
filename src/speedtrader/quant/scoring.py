"""
SpeedTrader AI — Scoring Framework

PORT OF: docs/reference/SpeedTraderBot_v6.1.mq5
SOURCE SHA-256: c799acaa797a4f23a8c9531c3b4f14599b73736af2151d9eeb7f42209332e8d9

    AddScoreBonus         L781-788    clamp [-50, +30], breakdown append
    FinalizeScore         L789        total = base + bonus
    SCORE_BONUS_CAP       L33         30.0
    Clamp                 L329
    StrategyClass         L331-340
    CandleQualityPenalty  L755-764
    WickPenalty           L767-776
    ApplyCandleBonus      L867-872
    AvgVolume             L504-509
    ApplyVolumeBonus      L818-823
    ScoreAndRank          L874-880    (subset — see below)

--------------------------------------------------------------------------------
IMPLEMENTED SUBSET — AND WHY THE OMISSIONS ARE SAFE
--------------------------------------------------------------------------------
ScoreAndRank (L876-878) applies eight bonuses. This module implements three:

    Candle  L869   -10 .. 0    Op/Cl/Hi/Lo at shift 1 only
    Wick    L871    -5 .. 0    Op/Cl/Hi/Lo at shift 1 only, gated on class 2
    Volume  L818     0/+4/+8   bar volumes at shifts 1..20

Deferred — require inputs FeatureEngine does not yet compute:
    Squeeze L824   0/+10   needs squeezeActive <- Bollinger + 20-bar width history (L640)
    Fib     L825   0/+8    needs fibValid/fib382/500/618; tolerance is 8.0*pip
    ORB     L849   0/+8    needs the session opening range (L685)

Permanently excluded — FX-only, no equity meaning:
    Basket  L843   0/+6    CurrencyMomentum across pairs sharing a currency (L832)
    Swap    L861   +-3     SYMBOL_SWAP_LONG / SYMBOL_SWAP_SHORT
    Overlap L856   +5/-8   London-New York session overlap

EVERY DEFERRED BONUS IS NON-NEGATIVE. The implemented subset can therefore only
UNDER-score a signal, never over-score it. For a minimum-score gate that is the
conservative direction: a signal may be wrongly rejected, never wrongly admitted.

Setting bonus to zero instead would NOT have been neutral. Candle and Wick return
values in [-10, 0] and [-5, 0] — they are penalties. Zeroing them would discard
penalties the source always applies, scoring every signal HIGHER than the
authoritative strategy, and would pin total_score at exactly 50.0 so that the
min_score gate could never fire.

--------------------------------------------------------------------------------
min_score IS NOT A PARITY VALUE
--------------------------------------------------------------------------------
risk_config.yaml sets min_score: 48.0. The source (L76) sets InpMinScore = 55.0.

48.0 is AN ENGINEERING THRESHOLD SET FOR THIS PROJECT, NOT AN MQL5-PARITY VALUE.
It was derived by subtracting the FX-only bonuses (Basket +6, Swap +3, Overlap +5
= 14 points) from the achievable ceiling. Three further bonuses (+26) are now also
deferred, so the gap between the reachable ceiling and the original 55 is wider
than that derivation assumed. Re-deriving the threshold would be speculation, so it
is left at 48.0 and the widened gap is recorded here.

--------------------------------------------------------------------------------
VOLUME IS NOT TICK VOLUME
--------------------------------------------------------------------------------
Source L501: Vol() is iTickVolume — a count of price changes, not shares traded.
Alpaca returns share volume, and the free IEX feed reports only a fraction of
consolidated volume. The bonus uses a RATIO (current / 20-bar mean), which is more
portable than an absolute, but TICK-VOLUME PARITY IS NOT CLAIMED.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..data.schemas import Direction, MarketSnapshot
from .features import Series
from .strategies.base import StrategyOutput

SOURCE_HASH = "c799acaa797a4f23a8c9531c3b4f14599b73736af2151d9eeb7f42209332e8d9"

SCORE_BONUS_CAP = 30.0       # L33
SCORE_BONUS_FLOOR = -50.0    # L785
VOLUME_LOOKBACK = 20         # L820 AvgVolume(sig.symIdx, 20)

#: Bonuses in ScoreAndRank that this module does not compute (L824/L825/L849).
#: All non-negative, so their absence can only lower a score.
DEFERRED_BONUSES = ("Squeeze", "Fib", "ORB")

#: FX-only, permanently excluded (L843 / L861 / L856).
EXCLUDED_BONUSES = ("Basket", "Swap", "Overlap")


def clamp(v: float, lo: float, hi: float) -> float:
    """L329."""
    return lo if v < lo else (hi if v > hi else v)


# ==========================================================================
# StrategyClass — L331-340
# ==========================================================================

def strategy_class(mql5_index: int) -> int:
    """L331-340. NOTE the source switches on `s+1`, not `s`.

    S07 has stratIdx 6, so s+1 == 7, which falls in `case 7` -> class 2
    (momentum/breakout). This is why the Wick penalty APPLIES to S07 (L870-871).
    """
    n = mql5_index + 1
    if n in (1, 2, 6):
        return 0        # mean reversion
    if n in (3, 5, 9):
        return 1        # trend
    if n in (7, 10, 13):
        return 2        # momentum / breakout
    return 3            # structural


# ==========================================================================
# Candle quality — L755-764
# ==========================================================================

def candle_quality_penalty(series: Series, direction: Direction) -> float:
    """L755-764. Returns a value in [-10, 0]. Never positive.

        rng = h - l
        if rng <= 0: return -10
        br = |c - o| / rng
        br < 0.30 -> -8   elif br < 0.50 -> -4
        BUY  and c < o and br > 0.6 -> additional -4
        SELL and c > o and br > 0.6 -> additional -4
        floor at -10
    """
    o, c = series.open(1), series.close(1)
    h, l = series.high(1), series.low(1)
    rng = h - l
    if rng <= 0:
        return -10.0
    body = abs(c - o)
    br = body / rng
    pen = 0.0
    if br < 0.30:
        pen -= 8.0
    elif br < 0.50:
        pen -= 4.0
    if direction is Direction.BUY and c < o and br > 0.6:
        pen -= 4.0
    if direction is Direction.SELL and c > o and br > 0.6:
        pen -= 4.0
    return max(pen, -10.0)


# ==========================================================================
# Wick penalty — L767-776
# ==========================================================================

def wick_penalty(series: Series, direction: Direction) -> float:
    """L767-776. Returns -5, -4 or 0. Never positive.

    A long upper wick on an up-break (or lower wick on a down-break) means the
    move was rejected inside the bar that produced the breakout signal.
    """
    o, c = series.open(1), series.close(1)
    h, l = series.high(1), series.low(1)
    body = abs(c - o)
    if body <= 0:
        return -4.0
    upper = h - max(o, c)
    lower = min(o, c) - l
    if direction is Direction.BUY and upper > body:
        return -5.0
    if direction is Direction.SELL and lower > body:
        return -5.0
    return 0.0


# ==========================================================================
# Volume — L504-509, L818-823
# ==========================================================================

def avg_volume(series: Series, count: int = VOLUME_LOOKBACK) -> float:
    """L504-509. Mean of Vol(k) for k = 1..count.

    NOTE: the window STARTS AT SHIFT 1, so the bar under test is included in its
    own average. That is the source's behaviour and is preserved — it damps the
    ratio slightly relative to a 2..21 window.
    """
    total = 0.0
    n = 0
    for k in range(1, count + 1):
        if not series.exists(k):
            break
        total += series.volume(k)
        n += 1
    return 0.0 if n == 0 else total / n


def volume_bonus(series: Series, count: int = VOLUME_LOOKBACK) -> float:
    """L818-823. avg <= 0 yields no bonus (the source returns early)."""
    avg = avg_volume(series, count)
    if avg <= 0:
        return 0.0
    if not series.exists(1):
        return 0.0
    r = series.volume(1) / avg
    if r > 1.5:
        return 8.0
    if r > 1.2:
        return 4.0
    return 0.0


# ==========================================================================
# Framework — L781-789
# ==========================================================================

@dataclass(frozen=True)
class ScoreResult:
    base_score: float
    bonus: float
    total_score: float
    breakdown: str
    components: dict            # {"Candle": -4.0, "Vol": 8.0} — applied amounts
    deferred: tuple[str, ...] = DEFERRED_BONUSES
    excluded: tuple[str, ...] = EXCLUDED_BONUSES


class _Accumulator:
    """Mirrors AddScoreBonus (L781-788), including its two subtleties:

    1. pts == 0.0 is skipped entirely — no breakdown entry (L783).
    2. The breakdown records the APPLIED amount after clamping, not the requested
       amount (L785-787). A bonus partially absorbed by the cap shows the part
       that actually landed.
    """

    __slots__ = ("bonus", "parts", "components")

    def __init__(self) -> None:
        self.bonus = 0.0
        self.parts: list[str] = []
        self.components: dict[str, float] = {}

    def add(self, name: str, pts: float) -> None:
        if pts == 0.0:
            return
        before = self.bonus
        self.bonus = clamp(self.bonus + pts, SCORE_BONUS_FLOOR, SCORE_BONUS_CAP)
        applied = self.bonus - before
        if applied == 0.0:
            return
        self.parts.append(f"{name}{applied:+.0f} ")
        self.components[name] = self.components.get(name, 0.0) + applied


def score_signal(output: StrategyOutput, snapshot: MarketSnapshot,
                 *, mql5_strategy_index: int) -> ScoreResult:
    """ScoreAndRank subset (L874-880), in source order: Candle (with Wick), Volume.

    FinalizeScore (L789) is applied at the end. ComputeEV is NOT called here —
    it lives in expected_value.py so that the cost-configuration failure path
    stays out of the scoring layer.
    """
    series = Series(snapshot.bars)
    acc = _Accumulator()

    # --- ApplyCandleBonus, L867-872 ---------------------------------
    acc.add("Candle", candle_quality_penalty(series, output.direction))
    if strategy_class(mql5_strategy_index) == 2:
        acc.add("Wick", wick_penalty(series, output.direction))

    # --- ApplyVolumeBonus, L818-823 ---------------------------------
    acc.add("Vol", volume_bonus(series))

    # Squeeze / Fib / ORB would run here (L876-877). Basket / Swap / Overlap
    # would follow (L877-878) and are permanently excluded.

    total = output.base_score + acc.bonus          # FinalizeScore, L789
    breakdown = (
        output.breakdown                            # "base50 " from InitSignal L893
        + "".join(acc.parts)
        + f"[deferred: {','.join(DEFERRED_BONUSES)}]"
    )
    return ScoreResult(
        base_score=output.base_score,
        bonus=acc.bonus,
        total_score=total,
        breakdown=breakdown,
        components=acc.components,
    )

"""
S07 — Momentum Breakout (ATR)

PORT OF: docs/reference/SpeedTraderBot_v6.1.mq5, lines 1032-1047
SOURCE SHA-256: c799acaa797a4f23a8c9531c3b4f14599b73736af2151d9eeb7f42209332e8d9
PROVENANCE DOC: docs/quant/S07_port.md

Source, verbatim:

    TradeSignal Strategy_S7(int i)
    {
       TradeSignal s; s.valid=false; SymbolState st=g_sym[i];
       if(st.atr<=0) return s;                                          // L1036
       if(Tm(i,22)==0) return s;   // need enough H1 history             // L1037
       double price=Cl(i,1);                                            // L1038
       double hh=-DBL_MAX,ll=DBL_MAX;                                   // L1039
       for(int k=2;k<=21;k++){ hh=MathMax(hh,Hi(i,k)); ll=MathMin(ll,Lo(i,k)); }  // L1040
       double candle=MathAbs(Cl(i,1)-Op(i,1));                          // L1041
       if(price>hh && candle>1.5*st.atr && price>st.ema200 && st.diPlus>st.diMinus)
       { double sl=price-1.5*st.atr, tp=price+3.0*st.atr; InitSignal(s,i,6,ORDER_TYPE_BUY,price,sl,tp,50.0); }
       else if(price<ll && candle>1.5*st.atr && price<st.ema200 && st.diMinus>st.diPlus)
       { double sl=price+1.5*st.atr, tp=price-3.0*st.atr; InitSignal(s,i,6,ORDER_TYPE_SELL,price,sl,tp,50.0); }
       return s;
    }

NOTHING IN THE FORMULA IS CHANGED. Not the 1.5/3.0 ATR multipliers, not the k=2..21
window, not the EMA200 filter, not the DI comparison, not the base score of 50, and no
condition is added or removed. Where the port differs from the source it is because
Python has a state MQL5 does not — see DEVIATIONS below.

--------------------------------------------------------------------------------
[UNVERIFIED] ADX / DI PARITY
--------------------------------------------------------------------------------
S07's fourth condition reads st.diPlus and st.diMinus, which come from
iADX(symbol, PERIOD_H1, 14) buffers 1 and 2 (source L520, L594-596).

quant/features.py::adx() implements MT5's documented ADX algorithm — DM normalised by
TR per bar, then smoothed with alpha = 2/(n+1). This differs from Wilder's textbook
ADX, which smooths +DM, -DM and TR separately with alpha = 1/n. The implementation
follows the deposited MQL5 specification, but it has NOT been compared numerically
against a running MetaTrader 5 terminal. Its status is UNVERIFIED, and no part of this
module may be described as having verified MT5 parity.

Exposure for S07 specifically is low but not zero: the strategy uses only the ordering
`diPlus > diMinus`, never an absolute threshold. A difference in smoothing shifts
levels more readily than it flips an ordering. S03 (`adx >= 20`) and S05 (`adx > 25`)
are materially more exposed and must not be ported until this is resolved.

--------------------------------------------------------------------------------
INDEXING
--------------------------------------------------------------------------------
MQL5 shift 1 is the last CLOSED bar; shift 0 is the forming bar. UpdateIndicators()
reads every indicator at [1] (source L614-618). Series enforces this: shift 1 maps to
bars[-1] and shift 0 raises. S07 therefore cannot read an unclosed bar — the guard is
structural, not a convention, so no look-ahead is possible through this path.

--------------------------------------------------------------------------------
DEVIATIONS FROM THE SOURCE, AND WHY
--------------------------------------------------------------------------------
1. Missing indicators. MQL5's SymbolState always holds a double; an unavailable
   indicator would arrive as 0.0. Python holds None. `ema200 is None` is treated as
   INDICATOR_UNAVAILABLE (no signal), not as 0.0 — because `price > 0.0` is true for
   every equity and would silently pass the EMA200 filter on missing data. This is a
   fail-closed necessity, not a formula change.

2. Diagnostic detail. The source returns an invalid signal with no reason. This port
   records which conditions failed. Evaluation ORDER and OUTCOME are unchanged: BUY is
   tested first and short-circuits (the source's `else if`); diagnostics are collected
   only on the path that produces no signal.

3. NormalizePrice / slPips. InitSignal (source L888, L891) rounds to broker tick size
   and converts the stop to pips via SymbolPip. Neither has an equity meaning. Prices
   are emitted unrounded here; tick rounding belongs to the execution layer against
   Alpaca asset metadata. SymbolPip is deliberately not emulated.
"""

from __future__ import annotations

from ...data.schemas import Direction, MarketSnapshot
from ..features import InsufficientHistory, Series
from .base import Code, StrategyOutput, StrategyResult

# --- Constants, all taken from the source. Do not tune. ---------------------
STRATEGY_ID = "S07"
SOURCE_REFERENCE = "SpeedTraderBot_v6.1.mq5 L1032-1047"
MQL5_STRATEGY_INDEX = 6          # InitSignal(s, i, 6, ...) — zero-based (L1043/L1045)

HISTORY_GUARD_SHIFT = 22         # Tm(i,22)==0                            (L1037)
WINDOW_FIRST_SHIFT = 2           # for(int k=2; ...)                      (L1040)
WINDOW_LAST_SHIFT = 21           # ... k<=21; k++)                        (L1040)
CANDLE_ATR_MULT = 1.5            # candle > 1.5*st.atr                    (L1042/L1044)
STOP_ATR_MULT = 1.5              # sl = price -/+ 1.5*st.atr              (L1043/L1045)
TARGET_ATR_MULT = 3.0            # tp = price +/- 3.0*st.atr              (L1043/L1045)
BASE_SCORE = 50.0                # InitSignal(..., 50.0)                  (L1043/L1045)


class S07MomentumBreakout:
    """Momentum breakout with an ATR-sized stop and target."""

    id = STRATEGY_ID
    source_reference = SOURCE_REFERENCE
    # The source guards on shift 22 existing — one more than the window needs.
    # That is the source's choice and is preserved exactly.
    min_bars = HISTORY_GUARD_SHIFT

    # ------------------------------------------------------------------ #
    def evaluate(self, snapshot: MarketSnapshot) -> StrategyResult:
        f = snapshot.features

        # --- L1036: if(st.atr<=0) return s; ---------------------------
        atr = f.atr
        if atr is None or atr <= 0.0:
            return StrategyResult(
                False,
                reason="ATR unavailable or non-positive",
                code=Code.ATR_UNAVAILABLE,
                detail={"atr": atr},
            )

        series = Series(snapshot.bars)

        # --- L1037: if(Tm(i,22)==0) return s; -------------------------
        # iTime returns 0 when the bar does not exist. Series.exists is the
        # equivalent test.
        if not series.exists(HISTORY_GUARD_SHIFT):
            return StrategyResult(
                False,
                reason=(f"insufficient H1 history: need bar at shift "
                        f"{HISTORY_GUARD_SHIFT}, have {len(series)} bars"),
                code=Code.INSUFFICIENT_HISTORY,
                detail={"need_shift": HISTORY_GUARD_SHIFT, "bars": len(series)},
            )

        # --- Deviation 1: indicators MQL5 would have as doubles --------
        ema200, di_plus, di_minus = f.ema200, f.di_plus, f.di_minus
        missing = [n for n, v in (("ema200", ema200), ("di_plus", di_plus),
                                  ("di_minus", di_minus)) if v is None]
        if missing:
            return StrategyResult(
                False,
                reason=f"indicator unavailable: {', '.join(missing)}",
                code=Code.INDICATOR_UNAVAILABLE,
                detail={"missing": missing},
            )

        # --- L1038: double price=Cl(i,1); ------------------------------
        try:
            price = series.close(1)
            open_1 = series.open(1)
        except InsufficientHistory as e:  # unreachable after the shift-22 guard
            return StrategyResult(False, reason=str(e), code=Code.INSUFFICIENT_HISTORY)

        # --- L1039-1040: hh/ll over k=2..21 ----------------------------
        # The window starts at shift 2, so the bar being tested (shift 1) is NOT part
        # of the extreme it must exceed. A window of 1..20 would compare the bar to
        # itself and essentially never fire.
        hh = None
        ll = None
        for k in range(WINDOW_FIRST_SHIFT, WINDOW_LAST_SHIFT + 1):
            h, lo = series.high(k), series.low(k)
            hh = h if hh is None or h > hh else hh
            ll = lo if ll is None or lo < ll else ll
        # The source seeds with -DBL_MAX / +DBL_MAX. The shift-22 guard makes the loop
        # non-empty, so hh/ll are always real prices; asserting it keeps a sentinel
        # from ever reaching a price comparison.
        assert hh is not None and ll is not None

        # --- L1041: candle body, not range -----------------------------
        candle = abs(price - open_1)

        # --- Shared threshold, evaluated once (source repeats it) ------
        body_threshold = CANDLE_ATR_MULT * atr
        body_ok = candle > body_threshold

        inputs = {
            "price": price, "open_1": open_1, "hh": hh, "ll": ll,
            "candle": candle, "atr": atr, "body_threshold": body_threshold,
            "ema200": ema200, "di_plus": di_plus, "di_minus": di_minus,
        }

        # --- L1042-1043: BUY. Tested first; the source uses `else if`. --
        if price > hh and body_ok and price > ema200 and di_plus > di_minus:
            return StrategyResult(
                True,
                output=StrategyOutput(
                    strategy_id=STRATEGY_ID,
                    direction=Direction.BUY,
                    entry=price,
                    stop_loss=price - STOP_ATR_MULT * atr,
                    take_profit=price + TARGET_ATR_MULT * atr,
                    base_score=BASE_SCORE,
                    breakdown=f"base{BASE_SCORE:.0f} ",
                    source_reference=SOURCE_REFERENCE,
                    inputs=inputs,
                ),
                reason="momentum breakout above 20-bar high",
                code=Code.SIGNAL,
            )

        # --- L1044-1045: SELL ------------------------------------------
        if price < ll and body_ok and price < ema200 and di_minus > di_plus:
            return StrategyResult(
                True,
                output=StrategyOutput(
                    strategy_id=STRATEGY_ID,
                    direction=Direction.SELL,
                    entry=price,
                    stop_loss=price + STOP_ATR_MULT * atr,
                    take_profit=price - TARGET_ATR_MULT * atr,
                    base_score=BASE_SCORE,
                    breakdown=f"base{BASE_SCORE:.0f} ",
                    source_reference=SOURCE_REFERENCE,
                    inputs=inputs,
                ),
                reason="momentum breakdown below 20-bar low",
                code=Code.SIGNAL,
            )

        # --- No signal. Diagnostics only; outcome already decided. ------
        failed: list[str] = []
        if price <= hh and price >= ll:
            failed.append(f"no breakout (price {price} within [{ll}, {hh}])")
        else:
            if not body_ok:
                failed.append(
                    f"candle body {candle:.4f} <= {CANDLE_ATR_MULT}*ATR "
                    f"({body_threshold:.4f})"
                )
            if price > hh:
                if price <= ema200:
                    failed.append(f"price {price} not above EMA200 {ema200}")
                if di_plus <= di_minus:
                    failed.append(f"DI+ {di_plus:.4f} not above DI- {di_minus:.4f}")
            elif price < ll:
                if price >= ema200:
                    failed.append(f"price {price} not below EMA200 {ema200}")
                if di_minus <= di_plus:
                    failed.append(f"DI- {di_minus:.4f} not above DI+ {di_plus:.4f}")

        return StrategyResult(
            False,
            reason="; ".join(failed) if failed else "no signal",
            code=Code.NO_SIGNAL,
            detail=inputs,
        )

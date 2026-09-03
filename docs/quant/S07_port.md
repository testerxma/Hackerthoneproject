# S07 — Momentum Breakout (ATR): Port Provenance

**Status: NOT PORTED.** This document records the authoritative source and every
dependency S07 requires. It is written before implementation so that when S07 is
written, a reviewer can diff it line-against-line rather than take it on trust.

---

## 1. Authoritative source

| | |
|---|---|
| **Location** | `docs/reference/SpeedTraderBot_v6.1.mq5` |
| **SHA-256** | `c799acaa797a4f23a8c9531c3b4f14599b73736af2151d9eeb7f42209332e8d9` |
| **Size** | 87,197 bytes · 2,138 lines |
| **Deposited** | 2026-09-02, read-only (`chmod 444`) |
| **Hash verified** | Yes — matches on deposit |

The file must not be edited. Any change invalidates every parity claim below. To
re-verify at any time:

```bash
sha256sum docs/reference/SpeedTraderBot_v6.1.mq5
# c799acaa797a4f23a8c9531c3b4f14599b73736af2151d9eeb7f42209332e8d9
```

---

## 2. S07 — source lines 1032–1047, verbatim

```cpp
// S7 Momentum Breakout (ATR)
TradeSignal Strategy_S7(int i)
{
   TradeSignal s; s.valid=false; SymbolState st=g_sym[i];
   if(st.atr<=0) return s;
   if(Tm(i,22)==0) return s;   // need enough H1 history
   double price=Cl(i,1);
   double hh=-DBL_MAX,ll=DBL_MAX;
   for(int k=2;k<=21;k++){ hh=MathMax(hh,Hi(i,k)); ll=MathMin(ll,Lo(i,k)); }
   double candle=MathAbs(Cl(i,1)-Op(i,1));
   if(price>hh && candle>1.5*st.atr && price>st.ema200 && st.diPlus>st.diMinus)
   { double sl=price-1.5*st.atr, tp=price+3.0*st.atr; InitSignal(s,i,6,ORDER_TYPE_BUY,price,sl,tp,50.0); }
   else if(price<ll && candle>1.5*st.atr && price<st.ema200 && st.diMinus>st.diPlus)
   { double sl=price+1.5*st.atr, tp=price-3.0*st.atr; InitSignal(s,i,6,ORDER_TYPE_SELL,price,sl,tp,50.0); }
   return s;
}
```

### Condition table

| # | Element | Source line | Exact semantics |
|---|---|---|---|
| 1 | ATR guard | 1036 | `st.atr <= 0` → no signal |
| 2 | History guard | 1037 | `Tm(i,22) == 0` → no signal. Bar at shift 22 must exist |
| 3 | Price | 1038 | `Cl(i,1)` — close of the **last closed bar** |
| 4 | Highest high | 1040 | `max(Hi(k))` for `k = 2..21` — **20 bars, excluding shift 1** |
| 5 | Lowest low | 1040 | `min(Lo(k))` for `k = 2..21` — same window |
| 6 | Candle body | 1041 | `abs(Cl(i,1) - Op(i,1))` — body only, not range |
| 7 | BUY | 1042 | `price > hh` ∧ `candle > 1.5*atr` ∧ `price > ema200` ∧ `diPlus > diMinus` |
| 8 | BUY SL | 1043 | `price − 1.5 * atr` |
| 9 | BUY TP | 1043 | `price + 3.0 * atr` |
| 10 | SELL | 1044 | `price < ll` ∧ `candle > 1.5*atr` ∧ `price < ema200` ∧ `diMinus > diPlus` |
| 11 | SELL SL | 1045 | `price + 1.5 * atr` |
| 12 | SELL TP | 1045 | `price − 3.0 * atr` |
| 13 | Base score | 1043/1045 | `50.0` |
| 14 | Strategy index | 1043/1045 | `6` (zero-based; "S7" is the 7th strategy) |
| 15 | Branch order | 1042/1044 | `else if` — BUY is evaluated first and wins ties |

### Non-obvious details a careless port would get wrong

- **The breakout window excludes shift 1.** `k` runs `2..21`, so the bar being tested
  is not part of the high/low it must exceed. A window of `1..20` would make the
  condition compare the bar to itself and almost never fire.
- **`candle` is the body, not the range.** `|close − open|`, not `high − low`.
- **The history guard requires shift 22**, one more than the window's shift 21.
- **`hh`/`ll` seed from `±DBL_MAX`**, so an empty loop would produce a sentinel rather
  than a price. Our guard (#2) makes the loop non-empty, but a Python port must not
  rely on `float('inf')` leaking into a comparison.
- **Reward:risk is fixed at 2.0** by construction (3.0 ATR / 1.5 ATR). This clears
  `risk_config.min_reward_risk = 1.5` unconditionally — the R:R gate will never
  reject an S07 signal. That is not a bug, but it means the gate is inert for this
  strategy and should not be presented as filtering it.

### Signal construction — `InitSignal`, source lines 885–894

```cpp
sig.entry = NormalizePrice(i, entry);   // broker tick-size rounding
sig.slPips = |entry − sl| / SymbolPip(i);
sig.breakdown = "base50 ";
```

`slPips` and `SymbolPip` are FX constructs with no equity equivalent. In the port,
the stop is expressed in absolute currency (`stop_distance` in `CandidateSignal`) and
`NormalizePrice` becomes tick-size rounding against Alpaca's asset metadata.
**`SymbolPip` must not be emulated.**

---

## 3. Indicator dependencies

Declared at source lines 518–520:

| Symbol | Source line | MQL5 call | Timeframe | Period |
|---|---|---|---|---|
| `st.ema200` | 518 | `iMA(s, PERIOD_H1, 200, 0, MODE_EMA, PRICE_CLOSE)` | H1 | 200 |
| `st.atr` | 519 | `iATR(s, PERIOD_H1, 14)` | H1 | 14 |
| `st.diPlus`, `st.diMinus` | 520 | `iADX(s, PERIOD_H1, 14)` | H1 | 14 |

ADX handle buffers, read at source lines 594–596:

```
buffer 0 → ADX      buffer 1 → DI+      buffer 2 → DI-
```

S07 uses **DI+ and DI− only**. It never reads the ADX line and has no ADX threshold.

Python implementations: `src/speedtrader/quant/features.py` — `ema()`, `atr()`, `adx()`.

---

## 4. Indexing convention — `[1]` closed-candle semantics

`UpdateIndicators()` (source lines 587–650) assigns every value from index `[1]`:

```cpp
st.atr    = atr[1];    st.atrPrev = atr[2];
st.ema200 = e200[1];
st.adx    = adx[1];    st.diPlus  = dip[1];   st.diMinus = dim[1];
```

`ReadBuf` (line 552) calls `ArraySetAsSeries(out, true)`, so index 0 is the
**currently forming** bar and index 1 is the **last closed** bar. Every value S07
consumes therefore describes a completed candle.

Mapping to the Python port:

| MQL5 shift | Meaning | Python (`bars` chronological) |
|---|---|---|
| 0 | forming bar | **not held** — `Series.at(0)` raises |
| 1 | last closed bar | `bars[-1]` |
| 2 | one before that | `bars[-2]` |
| 22 | history guard | `bars[-22]` |

Enforced by `Series` in `quant/features.py`; asserted by `test_shift_0_is_rejected`
and `test_shift_1_is_newest_closed_bar`.

**Look-ahead consequence:** because index 0 is unavailable by construction, S07 cannot
read a bar that has not closed. The guard is structural, not a convention.

---

## 5. Parity vocabulary — used precisely throughout this project

| Term | Meaning | Achievable here |
|---|---|---|
| **Specification parity** | Python matches the documented MQL5 algorithm, with a line-referenced mapping and hand-computed test vectors | Yes |
| **Verified parity** | Both implementations run on identical input and outputs are compared numerically | **No** — requires a MetaTrader 5 terminal, which this project does not have |

**No component of SpeedTrader may be described as having verified parity** until a
MT5 run produces a comparison artefact. Current status:

| Indicator | Status | Basis |
|---|---|---|
| EMA | `SPEC-PARITY` | MT5 seeds with `price[0]`, `pr = 2/(period+1)` |
| ATR | `SPEC-PARITY` | MT5 seeds with SMA of first `period` TRs, then Wilder `+= (TR−atr)/period` |
| ADX / DI± | **`UNVERIFIED`** | See §6 |

---

## 6. Unresolved parity questions

### Q1 — ADX/DI algorithm (open, material)

MT5's built-in `ADX.mq5` does **not** implement Wilder's textbook ADX:

| | Wilder (textbook) | MT5 `iADX` |
|---|---|---|
| Order of operations | smooth `+DM`, `−DM`, `TR` separately, then divide | divide `DM/TR` **per bar**, then smooth the ratio |
| Smoothing constant | `α = 1/n` | `α = 2/(n+1)` |

The two produce different numbers. `features.py::adx()` implements the MT5 variant,
because MT5 is what produced the behaviour Bot v6 was tuned against.

**This has not been numerically compared against a running terminal.**

*Exposure by strategy:*

- **S07 — low.** Uses only `diPlus > diMinus`, a comparison. A smoothing difference is
  far more likely to shift an absolute level than to flip an ordering.
- **S03, S05 — high.** Use `adx >= 20` and `adx > 25`, absolute thresholds. A few
  points of drift changes which bars qualify. **Resolve Q1 before porting either.**

*Resolution path:* export `iADX(H1,14)` buffers 0/1/2 for a known symbol and date
range from MT5, commit as a fixture under `tests/fixtures/`, and add a regression test
comparing `features.adx()` against it. Only then may the status change to
`VERIFIED-PARITY`.

### Q2 — EMA200 warm-up depth (resolved in code, open operationally)

MT5 seeds EMA with `price[0]`, so seed influence decays as `(1−α)ⁿ` with
`α = 2/201 ≈ 0.00995`:

| Bars | Residual seed influence |
|---|---|
| 300 | ~5.0% |
| 460 | ~1.0% |
| 891 | 0.1% |

`FeatureEngine.recommended_bars()` returns **891**, and `SnapshotBuilder` rejects
shorter history with `ema_not_converged` by default.

**Open question:** 891 H1 bars is roughly 37 calendar days of US equity trading.
Whether Alpaca's free `iex` feed reliably returns that depth is **untested** — it
requires a live call. If it does not, the options are to accept a documented
approximation via `require_ema_convergence=False`, or to fetch from a longer
timeframe and resample.

### Q3 — `NormalizePrice` tick rounding (open, minor)

The source rounds entry/SL/TP to the broker's tick size. Alpaca's equivalent comes
from asset metadata and is not yet wired. Until it is, computed prices may carry more
decimal places than Alpaca will accept on an order. Affects order submission, not
signal generation.

---

## 7. Port checklist — for whoever implements S07

- [ ] `strategy_id = "S07"`, `source_reference = "SpeedTraderBot_v6.1.mq5 L1032-1047"`
- [ ] ATR guard `atr <= 0` → `None`
- [ ] History guard: bar at shift 22 exists
- [ ] Window `k = 2..21` — **verify it excludes shift 1**
- [ ] `candle` = `|close − open|`, body not range
- [ ] All four BUY conditions; all four SELL conditions
- [ ] SL/TP multipliers 1.5 / 3.0, correct sign per direction
- [ ] `base_score = 50.0`
- [ ] BUY branch evaluated first (`else if`)
- [ ] No `SymbolPip` emulation — absolute stop distance
- [ ] Test vectors constructed by hand from the condition table above
- [ ] Q1 status restated in the module docstring

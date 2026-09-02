"""
SpeedTrader AI — Snapshot Builder
Spec: §20 Market Snapshot, §21 Single Source of Truth, §22 Data Freshness,
      §102 No Hallucinated Market Data, §111 Fail Closed

Produces the canonical MarketSnapshot every downstream stage shares. One snapshot_id
per decision; every agent, the quant core and the risk engine cite the same one.

PROVENANCE IS EXPLICIT AND NEVER FAKED (mandatory constraint 4).
The builder inspects the provider it was handed and stamps the vendor accordingly:

    AlpacaMarketData   -> vendor "alpaca"
    FixtureMarketData  -> vendor "replay",  is_simulated = True
    anything else      -> vendor "replay",  is_simulated = True   (fail closed)

An unknown provider is treated as simulated, not as Alpaca. Mislabelling fixture data
as live is the one error in this module that could put a fabricated number in front of
a judge or a risk decision, so the default leans the safe way.

BUILD FAILURE IS A RESULT, NOT AN EXCEPTION.
Insufficient history, stale bars and malformed data are expected operating conditions,
not bugs. They return SnapshotResult(ok=False, reason=...) so the caller records the
reason in the decision log (§75 no-trade memory) instead of catching an exception and
losing why.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..alpaca.market_data import MAX_AGE_SECONDS, MarketDataError, MarketDataProvider
from ..common.clock import Freshness, classify_freshness, utcnow
from ..common.ids import IdKind, new_id
from ..data.schemas import (
    Bar,
    DataSourceMeta,
    MarketRegime,
    MarketSnapshot,
    TechnicalFeatures,
)
from ..quant.features import FeatureEngine, FeatureSet
from .validators import Code, validate_freshness, validate_series


@dataclass(frozen=True)
class SnapshotResult:
    """Either a snapshot or an explicit, logged reason there isn't one."""
    ok: bool
    snapshot: MarketSnapshot | None = None
    reason: str = "ok"
    code: str = Code.OK
    detail: dict | None = None

    def __bool__(self) -> bool:
        return self.ok


def _classify_provider(provider: Any) -> tuple[str, bool]:
    """Returns (vendor, is_simulated). Unknown providers are treated as simulated."""
    name = type(provider).__name__
    if name == "AlpacaMarketData":
        return "alpaca", False
    if name == "FixtureMarketData":
        return "replay", True
    return "replay", True  # fail closed: never claim Alpaca provenance by default


class SnapshotBuilder:
    """Builds a validated MarketSnapshot from a MarketDataProvider."""

    def __init__(
        self,
        provider: MarketDataProvider,
        *,
        feature_engine: FeatureEngine | None = None,
        timeframe: str = "1Hour",
        bars_limit: int | None = None,
        require_fresh: bool = True,
        require_ema_convergence: bool = True,
    ):
        self.provider = provider
        self.features = feature_engine or FeatureEngine()
        self.timeframe = timeframe
        # Default to the EMA warmup requirement, not the indicator period. Fetching
        # 300 bars for a 200-period EMA returns a number that looks fine and is wrong.
        self.bars_limit = bars_limit or self.features.recommended_bars()
        self.require_fresh = require_fresh
        self.require_ema_convergence = require_ema_convergence

    # ------------------------------------------------------------------ #
    def build(self, symbol: str, *, now: datetime | None = None) -> SnapshotResult:
        now = now or utcnow()
        symbol = symbol.upper()
        vendor, is_simulated = _classify_provider(self.provider)

        # --- 1. Fetch --------------------------------------------------
        try:
            bars: list[Bar] = list(
                self.provider.get_bars(symbol, self.timeframe, self.bars_limit)
            )
        except MarketDataError as e:
            return SnapshotResult(False, reason=f"market data unavailable: {e}",
                                  code="data_unavailable")
        except Exception as e:  # broker/network failure -> no snapshot (§111)
            return SnapshotResult(False, reason=f"provider error: {type(e).__name__}: {e}",
                                  code="provider_error")

        # --- 2. Structural validation ----------------------------------
        min_required = max(self.features.min_bars(), 2)
        v = validate_series(bars, min_required=min_required)
        if not v.ok:
            return SnapshotResult(False, reason=v.reason, code=v.code, detail=v.detail)

        # --- 3. Freshness ----------------------------------------------
        max_age = MAX_AGE_SECONDS.get(self.timeframe, 10_800)
        f = validate_freshness(bars, max_age_seconds=max_age, now=now)
        if self.require_fresh and not f.ok:
            return SnapshotResult(False, reason=f.reason, code=f.code, detail=f.detail)
        freshness = classify_freshness(bars[-1].t, max_age, now=now)

        # --- 4. Features -----------------------------------------------
        fs: FeatureSet = self.features.compute(bars)
        if fs.atr is None or fs.ema200 is None:
            return SnapshotResult(
                False,
                reason="indicator computation returned no value: "
                       + ("; ".join(fs.warnings) if fs.warnings else "unknown"),
                code="indicator_unavailable",
                detail={"bars_used": fs.bars_used},
            )
        if self.require_ema_convergence and not fs.ema200_converged:
            return SnapshotResult(
                False,
                reason=fs.warnings[0] if fs.warnings else "EMA not converged",
                code="ema_not_converged",
                detail={"bars_used": fs.bars_used,
                        "recommended": self.features.recommended_bars()},
            )

        # --- 5. Quote (optional; absence is not fatal) ------------------
        bid = ask = spread = spread_pct = None
        try:
            q = self.provider.get_latest_quote(symbol)
        except Exception:
            q = None  # a missing quote degrades the snapshot, it does not invalidate it
        last = bars[-1]
        if q:
            bid, ask = q.get("bid"), q.get("ask")
            if bid and ask and ask > 0:
                spread = ask - bid
                spread_pct = spread / ask * 100.0

        # --- 6. Market state -------------------------------------------
        try:
            market_open = bool(self.provider.is_market_open())
        except Exception:
            market_open = False  # unknown session state -> not tradeable (§113)

        # --- 7. Assemble ------------------------------------------------
        snap = MarketSnapshot(
            snapshot_id=new_id(IdKind.SNAPSHOT),
            timestamp=now,
            symbol=symbol,
            price=last.c,
            bid=bid,
            ask=ask,
            spread=spread,
            spread_pct=spread_pct,
            volume=last.v,
            bars=bars,
            features=TechnicalFeatures(
                ema200=fs.ema200,
                atr=fs.atr,
                atr_prev=fs.atr_prev,
                adx=fs.adx,
                di_plus=fs.di_plus,
                di_minus=fs.di_minus,
            ),
            # Regime detection is DetectMktState() in the source and is NOT part of
            # Step 1. UNKNOWN is the honest value; a guessed regime would feed the
            # strategy-compatibility check with a fabricated input.
            regime=MarketRegime.UNKNOWN,
            source=DataSourceMeta(
                vendor=vendor,
                fetched_at=now,
                bar_timeframe=self.timeframe,
                bars_available=len(bars),
                freshness=freshness,
                notes=("SIMULATED DATA — not live Alpaca" if is_simulated else None),
            ),
            market_open=market_open,
            minutes_to_close=None,
            # prior_close / gap_pct deliberately left None. Overnight-gap semantics
            # need daily bars and a session calendar; deriving them from consecutive
            # H1 bars would produce a wrong number that the risk engine's
            # `overnight_gap` check would then act on. None makes that check skip,
            # which is correct until Step 2 supplies real daily data.
            prior_close=None,
            gap_pct=None,
        )
        return SnapshotResult(True, snapshot=snap)

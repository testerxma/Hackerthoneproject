"""
SpeedTrader AI — Market Data
Spec: §8 Alpaca market data, §16 Quant Core Pipeline, §22 Data Freshness, §102 No Hallucinated Data

TWO IMPLEMENTATIONS OF ONE PROTOCOL:

    AlpacaMarketData   — live/paper data from Alpaca
    FixtureMarketData  — deterministic bars from stored JSON

This is not a testing convenience bolted on afterwards. It is the mechanism that makes
§89 Decision Replay possible: replaying a decision means re-running the exact pipeline
against the snapshot that produced it, which requires the data layer to be swappable
for one that returns recorded bars. Building it now costs nothing and means replay is
a configuration change later rather than a rewrite.

§102: when data is missing this layer returns None or raises. It never interpolates,
never carries forward a stale bar, never substitutes a plausible number.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol, Sequence, runtime_checkable

from ..common.clock import Freshness, classify_freshness, utcnow
from ..data.schemas import Bar
from .client import AlpacaClient, AlpacaUnavailable

# Bot v6 ran H1 primary / M30 entry / H4 trend. Same structure, Alpaca's names.
TIMEFRAME_MAP = {
    "1Min": ("Minute", 1), "5Min": ("Minute", 5), "15Min": ("Minute", 15),
    "30Min": ("Minute", 30), "1Hour": ("Hour", 1), "4Hour": ("Hour", 4), "1Day": ("Day", 1),
}

# Max age before a bar set counts as stale, per timeframe. Roughly two bar periods:
# one missing bar is a feed hiccup, two means we are trading on old information.
MAX_AGE_SECONDS = {
    "1Min": 180, "5Min": 900, "15Min": 2_700, "30Min": 5_400,
    "1Hour": 10_800, "4Hour": 43_200, "1Day": 259_200,
}


class MarketDataError(RuntimeError):
    pass


@runtime_checkable
class MarketDataProvider(Protocol):
    """The contract the quant core and snapshot builder depend on."""

    def get_bars(self, symbol: str, timeframe: str, limit: int) -> list[Bar]: ...
    def get_latest_quote(self, symbol: str) -> dict | None: ...
    def is_market_open(self) -> bool: ...


# ==========================================================================
# Live implementation
# ==========================================================================

class AlpacaMarketData:
    """Bars and quotes from Alpaca.

    Note on the free tier: the IEX feed covers a fraction of consolidated volume, so
    volume-based signals (Bot v6's volume bonus, S12 liquidity sweeps, S14's low-volume
    breakout check) will read lower than the true tape. This is a real limitation of the
    data, not of the code, and it is recorded here so nobody later mistakes a weak
    volume signal for a bug. SIP is the paid fix.
    """

    def __init__(self, client: AlpacaClient):
        self.client = client
        self.feed = client.feed

    def get_bars(self, symbol: str, timeframe: str, limit: int = 300) -> list[Bar]:
        if timeframe not in TIMEFRAME_MAP:
            raise MarketDataError(f"unsupported timeframe {timeframe!r}; known: {sorted(TIMEFRAME_MAP)}")
        try:
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        except ImportError as e:  # pragma: no cover
            raise AlpacaUnavailable("alpaca-py is not installed") from e

        unit_name, amount = TIMEFRAME_MAP[timeframe]
        tf = TimeFrame(amount, getattr(TimeFrameUnit, unit_name))

        # Over-request the window: markets close, weekends exist, and asking for
        # "300 bars back" in wall-clock time under-delivers on any intraday timeframe.
        span = self._lookback_span(timeframe, limit)
        req = StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=tf,
            start=utcnow() - span, limit=limit, feed=self.feed,
        )
        try:
            resp = self.client.stock_data.get_stock_bars(req)
        except Exception as e:
            raise AlpacaUnavailable(f"bars unavailable for {symbol}: {e}") from e

        raw = resp.data.get(symbol, []) if hasattr(resp, "data") else []
        bars = [
            Bar(t=b.timestamp, o=float(b.open), h=float(b.high),
                l=float(b.low), c=float(b.close), v=float(b.volume))
            for b in raw
        ]
        bars.sort(key=lambda b: b.t)
        return bars

    #: Calendar time per unit of TRADING time. A calendar week holds 168 hours
    #: but only about 32.5 regular market hours, so an intraday request framed
    #: in wall-clock time under-delivers by roughly five to one. Alpaca's
    #: intraday bars include extended hours, which softens that to about 3.7;
    #: 6.0 leaves room for holidays and early closes.
    #:
    #: This is not a tuning constant, it is a correction for a unit mismatch,
    #: and getting it wrong is not a near miss. Found live: at the previous
    #: value of 3.0 a request for the 891 hourly bars an EMA200 needs to
    #: converge returned 660, so the snapshot builder refused EVERY hourly
    #: symbol for want of history. It failed closed, which is right, but it
    #: could never have failed any other way — the system was structurally
    #: unable to trade its default timeframe and the fixtures could not show it.
    INTRADAY_CALENDAR_FACTOR = 6.0
    #: Five trading days per seven calendar days, plus holidays.
    DAILY_CALENDAR_FACTOR = 1.6

    @staticmethod
    def _lookback_span(timeframe: str, limit: int) -> timedelta:
        per_bar = {
            "1Min": 60, "5Min": 300, "15Min": 900, "30Min": 1_800,
            "1Hour": 3_600, "4Hour": 14_400, "1Day": 86_400,
        }[timeframe]
        factor = (AlpacaMarketData.DAILY_CALENDAR_FACTOR if timeframe == "1Day"
                  else AlpacaMarketData.INTRADAY_CALENDAR_FACTOR)
        return timedelta(seconds=per_bar * limit * factor)

    def get_latest_quote(self, symbol: str) -> dict | None:
        try:
            from alpaca.data.requests import StockLatestQuoteRequest
        except ImportError as e:  # pragma: no cover
            raise AlpacaUnavailable("alpaca-py is not installed") from e
        try:
            req = StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=self.feed)
            q = self.client.stock_data.get_stock_latest_quote(req).get(symbol)
        except Exception as e:
            raise AlpacaUnavailable(f"quote unavailable for {symbol}: {e}") from e
        if q is None:
            return None
        return {
            "bid": float(q.bid_price) if q.bid_price else None,
            "ask": float(q.ask_price) if q.ask_price else None,
            "timestamp": q.timestamp,
        }

    def is_market_open(self) -> bool:
        return self.client.is_market_open()


# ==========================================================================
# Fixture implementation — tests, replay, offline development
# ==========================================================================

class FixtureMarketData:
    """Deterministic bars from memory or disk. No network, no clock dependence.

    Used by: unit tests, strategy regression tests (§90 REGRESSION TEST replay mode),
    and read-only replay of recorded decisions (§89).
    """

    def __init__(
        self,
        bars: dict[tuple[str, str], list[Bar]] | None = None,
        quotes: dict[str, dict] | None = None,
        market_open: bool = True,
    ):
        self._bars = bars or {}
        self._quotes = quotes or {}
        self._market_open = market_open

    @classmethod
    def from_directory(cls, path: str | Path) -> "FixtureMarketData":
        """Loads <symbol>_<timeframe>.json files of {"bars": [{t,o,h,l,c,v}, ...]}."""
        p = Path(path)
        bars: dict[tuple[str, str], list[Bar]] = {}
        for f in sorted(p.glob("*.json")):
            stem = f.stem
            if "_" not in stem:
                continue
            symbol, timeframe = stem.rsplit("_", 1)
            payload = json.loads(f.read_text())
            bars[(symbol.upper(), timeframe)] = [
                Bar(t=datetime.fromisoformat(b["t"]), o=b["o"], h=b["h"],
                    l=b["l"], c=b["c"], v=b["v"])
                for b in payload["bars"]
            ]
        return cls(bars=bars)

    def get_bars(self, symbol: str, timeframe: str, limit: int = 300) -> list[Bar]:
        key = (symbol.upper(), timeframe)
        if key not in self._bars:
            # §102: absent data is absent. Do not fabricate a series.
            raise MarketDataError(f"no fixture bars for {symbol} {timeframe}")
        return self._bars[key][-limit:]

    def get_latest_quote(self, symbol: str) -> dict | None:
        return self._quotes.get(symbol.upper())

    def is_market_open(self) -> bool:
        return self._market_open


# ==========================================================================
# Shared helpers
# ==========================================================================

def bars_freshness(bars: Sequence[Bar], timeframe: str, now: datetime | None = None) -> Freshness:
    """§22. Freshness of a bar series, judged against its own timeframe."""
    if not bars:
        return Freshness.MISSING
    return classify_freshness(
        bars[-1].t, MAX_AGE_SECONDS.get(timeframe, 10_800), now=now
    )


def validate_bars(bars: Sequence[Bar], *, min_required: int) -> tuple[bool, str]:
    """Delegates to the canonical validator in data/validators.py.

    This function predates that module. It is kept so existing callers and their
    tests keep working, but it is no longer a second implementation: two validators
    drift, and the drift is silent — a series rejected by one path and accepted by
    the other produces indicators on data the system already judged untrustworthy.

    Imported inside the function to avoid an alpaca -> data -> alpaca import cycle
    (data/snapshot.py imports MAX_AGE_SECONDS from this module).
    """
    from ..data.validators import validate_bars as _canonical
    return _canonical(bars, min_required=min_required)

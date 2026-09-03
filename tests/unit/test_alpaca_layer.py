"""Tests for the Alpaca layer. No network required — that is the point of the protocol."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pytest  # noqa: E402

from speedtrader.alpaca.client import (  # noqa: E402
    AlpacaConfigError, AlpacaCredentials, load_credentials,
)
from speedtrader.alpaca.market_data import (  # noqa: E402
    FixtureMarketData, MarketDataError, MarketDataProvider,
    bars_freshness, validate_bars,
)
from speedtrader.common.clock import Freshness  # noqa: E402
from speedtrader.data.schemas import Bar  # noqa: E402

PAPER = {"environment": "paper"}
LIVE = {"environment": "live"}
ENV = {"ALPACA_API_KEY": "PKTEST123456", "ALPACA_SECRET_KEY": "secret", "ALPACA_PAPER": "true"}


# ------------------------------------------------------------ credentials

def test_paper_credentials_load():
    c = load_credentials(PAPER, env=ENV)
    assert c.paper and "paper-api" in c.base_url


def test_missing_keys_fail_closed():
    with pytest.raises(AlpacaConfigError):
        load_credentials(PAPER, env={})


def test_live_requires_both_config_and_explicit_flag():
    """One mistake must not be enough to route real money."""
    with pytest.raises(AlpacaConfigError, match="allow_live"):
        load_credentials(LIVE, env={**ENV, "ALPACA_PAPER": "false"})
    c = load_credentials(LIVE, env={**ENV, "ALPACA_PAPER": "false"}, allow_live=True)
    assert not c.paper


def test_conflicting_paper_flags_refuse_to_guess():
    with pytest.raises(AlpacaConfigError, match="Conflict"):
        load_credentials(PAPER, env={**ENV, "ALPACA_PAPER": "false"})


def test_secret_never_appears_in_repr_or_str():
    c = AlpacaCredentials(api_key="PKLIVEKEY9999", secret_key="TOPSECRET")
    for s in (repr(c), str(c), f"{c}", f"{[c]}"):
        assert "TOPSECRET" not in s and "PKLIVEKEY" not in s


def test_invalid_environment_rejected():
    with pytest.raises(AlpacaConfigError):
        load_credentials({"environment": "prod"}, env=ENV)


# ------------------------------------------------------------ market data

def mkbars(n=10, start=100.0, step=1.0, end=None):
    end = end or datetime.now(timezone.utc)
    out = []
    for i in range(n):
        c = start + i * step
        out.append(Bar(t=end - timedelta(hours=n - i), o=c - 0.5, h=c + 1.0,
                       l=c - 1.0, c=c, v=1000.0 + i))
    return out


def test_fixture_satisfies_the_protocol():
    assert isinstance(FixtureMarketData(), MarketDataProvider)


def test_fixture_returns_stored_bars_and_respects_limit():
    p = FixtureMarketData(bars={("AAPL", "1Hour"): mkbars(50)})
    assert len(p.get_bars("AAPL", "1Hour", limit=10)) == 10
    assert len(p.get_bars("aapl", "1Hour", limit=999)) == 50   # case-insensitive


def test_missing_fixture_raises_rather_than_fabricating():
    """§102: absent data is absent. Never a plausible substitute."""
    with pytest.raises(MarketDataError):
        FixtureMarketData().get_bars("NOPE", "1Hour")


def test_freshness_of_bar_series():
    now = datetime.now(timezone.utc)
    assert bars_freshness(mkbars(5, end=now), "1Hour", now=now) is Freshness.FRESH
    old = mkbars(5, end=now - timedelta(hours=10))
    assert bars_freshness(old, "1Hour", now=now) is Freshness.STALE
    assert bars_freshness([], "1Hour") is Freshness.MISSING


# ------------------------------------------------------------ validation

def test_valid_series_passes():
    assert validate_bars(mkbars(300), min_required=200)[0]


def test_insufficient_history_rejected():
    """A 200-period EMA on 50 bars produces a confident, wrong number."""
    ok, msg = validate_bars(mkbars(50), min_required=200)
    assert not ok and "insufficient history" in msg


@pytest.mark.parametrize("bad,expect", [
    (Bar(t=datetime.now(timezone.utc), o=100, h=95, l=99, c=98, v=1), "below low"),
    (Bar(t=datetime.now(timezone.utc), o=200, h=105, l=95, c=100, v=1), "outside high-low"),
    (Bar(t=datetime.now(timezone.utc), o=0, h=105, l=-5, c=0, v=1), "non-positive"),
    (Bar(t=datetime.now(timezone.utc), o=100, h=105, l=95, c=100, v=-5), "negative volume"),
])
def test_malformed_bars_rejected(bad, expect):
    ok, msg = validate_bars([bad], min_required=1)
    assert not ok and expect in msg


def test_out_of_order_bars_rejected():
    """Reversed bars silently invert every momentum calculation downstream."""
    b = mkbars(10)
    b[3], b[7] = b[7], b[3]
    ok, msg = validate_bars(b, min_required=5)
    assert not ok and "chronological" in msg


def test_fixture_directory_roundtrip(tmp_path):
    import json
    bars = mkbars(20)
    (tmp_path / "AAPL_1Hour.json").write_text(json.dumps(
        {"bars": [{"t": b.t.isoformat(), "o": b.o, "h": b.h,
                   "l": b.l, "c": b.c, "v": b.v} for b in bars]}))
    p = FixtureMarketData.from_directory(tmp_path)
    loaded = p.get_bars("AAPL", "1Hour")
    assert len(loaded) == 20
    assert loaded[-1].c == pytest.approx(bars[-1].c)
    assert validate_bars(loaded, min_required=20)[0]


# ============================ the bar-request window is in the wrong unit
# Alpaca's bars endpoint is bounded by a wall-clock START time, but a request is
# framed in BARS. A calendar week holds 168 hours and only ~32.5 regular market
# hours, so an intraday window computed in calendar time under-delivers about
# five to one.
#
# Found by running the live script, not by a fixture: at the old factor of 3.0 a
# request for the 891 hourly bars an EMA200 needs to converge came back with 660,
# so the snapshot builder refused EVERY hourly symbol for want of history. It
# failed closed, which is correct — but it could never have failed any other way.
# The system was structurally unable to trade its own default timeframe.

from speedtrader.alpaca.market_data import AlpacaMarketData  # noqa: E402

#: 6.5 regular market hours a day, five days in seven.
TRADING_HOURS_PER_CALENDAR_HOUR = (6.5 * 5) / (24 * 7)


@pytest.mark.parametrize("timeframe,seconds_per_bar", [
    ("1Min", 60), ("5Min", 300), ("15Min", 900), ("30Min", 1_800),
    ("1Hour", 3_600), ("4Hour", 14_400),
])
def test_an_intraday_window_covers_the_bars_it_asks_for(timeframe, seconds_per_bar):
    """The requested window must contain `limit` TRADING bars, not `limit`
    calendar ones. Priced with regular hours only, so extended-hours bars are
    margin rather than the thing that makes it work."""
    limit = 891
    span = AlpacaMarketData._lookback_span(timeframe, limit)
    calendar_bars = span.total_seconds() / seconds_per_bar
    assert calendar_bars * TRADING_HOURS_PER_CALENDAR_HOUR >= limit


def test_the_daily_window_covers_weekends():
    """Five trading days per seven calendar days, plus holidays."""
    limit = 300
    span = AlpacaMarketData._lookback_span("1Day", limit)
    assert span.total_seconds() / 86_400 * (5 / 7) >= limit


def test_the_hourly_window_reaches_ema200_convergence():
    """The regression itself, in the exact shape that broke: the default
    timeframe must be able to supply the bars the default feature set needs."""
    from speedtrader.quant.features import FeatureEngine

    needed = FeatureEngine().recommended_bars()
    span = AlpacaMarketData._lookback_span("1Hour", needed)
    available = span.total_seconds() / 3_600 * TRADING_HOURS_PER_CALENDAR_HOUR
    assert available >= needed, (
        f"a {span.days}-day window yields ~{available:.0f} regular-hours bars, "
        f"short of the {needed} an EMA200 needs — the snapshot builder would "
        f"refuse every symbol"
    )


def test_an_unknown_timeframe_is_refused_rather_than_defaulted():
    with pytest.raises(KeyError):
        AlpacaMarketData._lookback_span("3Hour", 100)

"""
Tests for validators and SnapshotBuilder.

INDEXING: bars are chronological (oldest first); MQL5 shift 1 == bars[-1].
PROVENANCE: fixture-sourced snapshots must never be labelled as live Alpaca data.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import pytest  # noqa: E402

from speedtrader.alpaca.market_data import FixtureMarketData  # noqa: E402
from speedtrader.common.clock import Freshness  # noqa: E402
from speedtrader.data.schemas import Bar, MarketRegime  # noqa: E402
from speedtrader.data.snapshot import SnapshotBuilder, _classify_provider  # noqa: E402
from speedtrader.data.validators import (  # noqa: E402
    Code,
    validate_bars,
    validate_freshness,
    validate_series,
)
from speedtrader.quant.features import FeatureEngine  # noqa: E402

NOW = datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc)


def ramp(n: int, *, end: datetime = NOW, start: float = 100.0,
         step: float = 0.3) -> list[Bar]:
    """n chronological hourly bars ending at `end`."""
    out = []
    for i in range(n):
        c = start + i * step
        out.append(Bar(t=end - timedelta(hours=n - 1 - i), o=c - 0.2, h=c + 0.4,
                       l=c - 0.4, c=c, v=1000.0 + i))
    return out


# ==========================================================================
# validate_series
# ==========================================================================

def test_valid_series_passes():
    r = validate_series(ramp(300), min_required=200)
    assert r.ok and bool(r) and r.code == Code.OK


def test_empty_series_rejected():
    r = validate_series([], min_required=1)
    assert not r and r.code == Code.EMPTY


def test_insufficient_history_rejected_with_counts():
    r = validate_series(ramp(50), min_required=200)
    assert not r and r.code == Code.INSUFFICIENT
    assert r.detail == {"have": 50, "need": 200}


@pytest.mark.parametrize("bar,code", [
    (Bar(t=NOW, o=100, h=95, l=99, c=98, v=1), Code.HIGH_BELOW_LOW),
    (Bar(t=NOW, o=200, h=105, l=95, c=100, v=1), Code.OHLC_OUT_OF_RANGE),
    (Bar(t=NOW, o=100, h=105, l=95, c=100, v=-1), Code.NEGATIVE_VOLUME),
    (Bar(t=NOW, o=float("nan"), h=105, l=95, c=100, v=1), Code.NON_FINITE),
    (Bar(t=NOW, o=100, h=float("inf"), l=95, c=100, v=1), Code.NON_FINITE),
])
def test_malformed_bar_rejected(bar, code):
    r = validate_series([bar], min_required=1)
    assert not r and r.code == code


def test_non_positive_price_rejected():
    r = validate_series([Bar(t=NOW, o=0.0, h=0.0, l=0.0, c=0.0, v=1)], min_required=1)
    assert not r and r.code == Code.NON_POSITIVE_PRICE


def test_naive_timestamp_rejected():
    """A naive timestamp makes every freshness comparison meaningless."""
    naive = Bar(t=datetime(2026, 9, 1, 15, 0), o=100, h=101, l=99, c=100, v=1)
    r = validate_series([naive], min_required=1)
    assert not r and r.code == Code.NAIVE_TIMESTAMP


def test_out_of_order_bars_rejected():
    """Reversed bars silently invert every momentum calculation downstream."""
    b = ramp(10)
    b[3], b[7] = b[7], b[3]
    r = validate_series(b, min_required=5)
    assert not r and r.code == Code.NOT_CHRONOLOGICAL


def test_duplicate_timestamp_rejected():
    """A duplicated bar double-counts in every moving average."""
    b = ramp(10)
    b[5] = Bar(t=b[4].t, o=b[5].o, h=b[5].h, l=b[5].l, c=b[5].c, v=b[5].v)
    r = validate_series(b, min_required=5)
    assert not r and r.code == Code.DUPLICATE_TIMESTAMP


def test_legacy_shim_preserves_tuple_signature():
    """alpaca/market_data.validate_bars callers must keep working, one implementation."""
    ok, msg = validate_bars(ramp(300), min_required=200)
    assert ok is True and msg == "ok"
    ok, msg = validate_bars(ramp(5), min_required=200)
    assert ok is False and "insufficient history" in msg


# ==========================================================================
# validate_freshness
# ==========================================================================

def test_fresh_series_passes():
    assert validate_freshness(ramp(10), max_age_seconds=10_800, now=NOW).ok


def test_stale_series_rejected():
    old = ramp(10, end=NOW - timedelta(hours=10))
    r = validate_freshness(old, max_age_seconds=10_800, now=NOW)
    assert not r and r.code == Code.STALE
    assert r.detail["freshness"] == Freshness.STALE.value


# ==========================================================================
# Provenance — mandatory constraint 4
# ==========================================================================

def test_fixture_provider_is_never_labelled_alpaca():
    vendor, simulated = _classify_provider(FixtureMarketData())
    assert vendor == "replay" and simulated is True


def test_unknown_provider_defaults_to_simulated():
    """Fail closed: an unrecognised provider must not inherit Alpaca provenance."""
    class MysteryProvider:
        pass
    vendor, simulated = _classify_provider(MysteryProvider())
    assert vendor == "replay" and simulated is True


def test_snapshot_from_fixture_carries_simulated_marker():
    n = FeatureEngine().recommended_bars()
    p = FixtureMarketData(bars={("AAPL", "1Hour"): ramp(n)})
    r = SnapshotBuilder(p).build("AAPL", now=NOW)
    assert r.ok
    assert r.snapshot.source.vendor == "replay"
    assert "SIMULATED" in r.snapshot.source.notes


# ==========================================================================
# SnapshotBuilder — success path
# ==========================================================================

def _builder_and_provider(n: int | None = None, **kw):
    n = n or FeatureEngine().recommended_bars()
    p = FixtureMarketData(
        bars={("AAPL", "1Hour"): ramp(n)},
        quotes={"AAPL": {"bid": 100.0, "ask": 100.2, "timestamp": NOW}},
    )
    return SnapshotBuilder(p, **kw), p


def test_build_produces_complete_snapshot():
    b, _ = _builder_and_provider()
    r = b.build("AAPL", now=NOW)
    assert r.ok
    s = r.snapshot
    assert s.snapshot_id.startswith("snap_")
    assert s.symbol == "AAPL"
    assert s.price is not None
    assert s.features.ema200 is not None
    assert s.features.atr is not None
    assert s.features.di_plus is not None
    assert s.source.freshness is Freshness.FRESH
    assert s.is_tradeable()


def test_snapshot_price_is_newest_closed_bar():
    """MQL5 reads at shift 1; our newest bar is that bar."""
    b, p = _builder_and_provider()
    bars = p.get_bars("AAPL", "1Hour", limit=10_000)
    assert b.build("AAPL", now=NOW).snapshot.price == pytest.approx(bars[-1].c)


def test_quote_populates_spread():
    b, _ = _builder_and_provider()
    s = b.build("AAPL", now=NOW).snapshot
    assert s.spread == pytest.approx(0.2)
    assert s.spread_pct == pytest.approx(0.2 / 100.2 * 100.0)


def test_missing_quote_degrades_but_does_not_invalidate():
    n = FeatureEngine().recommended_bars()
    b = SnapshotBuilder(FixtureMarketData(bars={("AAPL", "1Hour"): ramp(n)}))
    r = b.build("AAPL", now=NOW)
    assert r.ok
    assert r.snapshot.bid is None and r.snapshot.spread is None


def test_regime_is_unknown_not_guessed():
    """DetectMktState is not part of Step 1. A guessed regime would feed the
    strategy-compatibility check with a fabricated input."""
    b, _ = _builder_and_provider()
    assert b.build("AAPL", now=NOW).snapshot.regime is MarketRegime.UNKNOWN


def test_gap_fields_left_none_rather_than_derived_from_hourly_bars():
    """The risk engine acts on gap_pct. A wrong number is worse than no number:
    None makes the overnight_gap check skip, which is correct until daily bars exist."""
    b, _ = _builder_and_provider()
    s = b.build("AAPL", now=NOW).snapshot
    assert s.prior_close is None and s.gap_pct is None


# ==========================================================================
# SnapshotBuilder — failure paths, all explicit results, no exceptions
# ==========================================================================

def test_missing_symbol_returns_result_not_exception():
    r = SnapshotBuilder(FixtureMarketData()).build("NOPE", now=NOW)
    assert not r.ok and r.code == "data_unavailable"
    assert r.snapshot is None


def test_insufficient_history_rejected():
    p = FixtureMarketData(bars={("AAPL", "1Hour"): ramp(50)})
    r = SnapshotBuilder(p).build("AAPL", now=NOW)
    assert not r.ok and r.code == Code.INSUFFICIENT


def test_unconverged_ema_rejected_by_default():
    """300 bars for a 200-period EMA leaves ~5% of the seed error. Rejected."""
    p = FixtureMarketData(bars={("AAPL", "1Hour"): ramp(300)})
    r = SnapshotBuilder(p, bars_limit=300).build("AAPL", now=NOW)
    assert not r.ok and r.code == "ema_not_converged"
    assert r.detail["bars_used"] == 300


def test_unconverged_ema_can_be_opted_into_explicitly():
    p = FixtureMarketData(bars={("AAPL", "1Hour"): ramp(300)})
    r = SnapshotBuilder(p, bars_limit=300,
                        require_ema_convergence=False).build("AAPL", now=NOW)
    assert r.ok and r.snapshot.features.ema200 is not None


def test_stale_bars_rejected():
    n = FeatureEngine().recommended_bars()
    p = FixtureMarketData(bars={("AAPL", "1Hour"): ramp(n, end=NOW - timedelta(hours=12))})
    r = SnapshotBuilder(p).build("AAPL", now=NOW)
    assert not r.ok and r.code == Code.STALE


def test_malformed_bars_rejected():
    n = FeatureEngine().recommended_bars()
    bars = ramp(n)
    bars[100] = Bar(t=bars[100].t, o=100, h=95, l=99, c=98, v=1)
    p = FixtureMarketData(bars={("AAPL", "1Hour"): bars})
    r = SnapshotBuilder(p).build("AAPL", now=NOW)
    assert not r.ok and r.code == Code.HIGH_BELOW_LOW


def test_provider_exception_fails_closed():
    class Exploding:
        def get_bars(self, *a, **k): raise RuntimeError("network down")
        def get_latest_quote(self, s): return None
        def is_market_open(self): return True
    r = SnapshotBuilder(Exploding()).build("AAPL", now=NOW)
    assert not r.ok and r.code == "provider_error"


def test_unknown_market_state_marks_snapshot_not_tradeable():
    """§113: unknown session state must not be read as open."""
    n = FeatureEngine().recommended_bars()
    class NoClock(FixtureMarketData):
        def is_market_open(self): raise RuntimeError("clock unavailable")
    p = NoClock(bars={("AAPL", "1Hour"): ramp(n)})
    r = SnapshotBuilder(p).build("AAPL", now=NOW)
    assert r.ok
    assert r.snapshot.market_open is False
    assert r.snapshot.is_tradeable() is False


def test_default_bars_limit_covers_ema_warmup():
    """The builder must not default to a limit that guarantees an unconverged EMA."""
    b, _ = _builder_and_provider()
    assert b.bars_limit >= FeatureEngine().recommended_bars()


def test_each_snapshot_gets_a_unique_id():
    b, _ = _builder_and_provider()
    ids = {b.build("AAPL", now=NOW).snapshot.snapshot_id for _ in range(5)}
    assert len(ids) == 5

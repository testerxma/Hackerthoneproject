"""
Tests for the deterministic risk engine.

These exist because the risk engine is the one component where a silent bug costs
money rather than embarrassment. Every test below asserts a rule that was in
SpeedTraderBot.mq5 and must survive the port unchanged.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pytest  # noqa: E402

from speedtrader.common.clock import expires_at, utcnow  # noqa: E402
from speedtrader.common.ids import IdKind, new_id  # noqa: E402
from speedtrader.data.schemas import (  # noqa: E402
    CandidateSignal,
    Direction,
    MarketRegime,
    RiskGateVerdict,
)
from speedtrader.risk.engine import DeterministicRiskEngine, RiskEngineError  # noqa: E402
from speedtrader.risk.measures import (  # noqa: E402
    pearson_correlation,
    portfolio_heat_pct,
    sector_exposure_pct,
    size_position,
)
from speedtrader.risk.state import (  # noqa: E402
    AccountState,
    OpenPosition,
    PortfolioState,
    StrategyStats,
)

CFG = {
    "fail_closed": True,
    "risk_per_trade_pct": 1.0,
    "min_size_multiplier": 0.25,
    "max_size_multiplier": 1.50,
    "daily_loss_limit_pct": 3.0,
    "max_consecutive_losses": 6,
    "portfolio_heat_max_pct": 8.0,
    "heat_reduce_level_pct": 6.0,
    "heat_reduce_multiplier": 0.5,
    "max_symbol_exposure_pct": 4.0,
    "max_sector_exposure_pct": 6.0,
    "max_open_positions": 5,
    "correlation_enabled": True,
    "correlation_threshold": 0.7,
    "correlation_lookback_bars": 30,
    "min_score": 48.0,
    "require_positive_ev": True,
    "min_reward_risk": 1.5,
    "min_stop_distance_atr": 0.5,
    "max_spread_pct": 0.15,
    "max_gap_pct": 3.0,
    "kelly_enabled": True,
    "kelly_fraction": 0.35,
    "kelly_min_trades": 30,
    "kelly_multiplier_floor": 0.5,
    "kelly_multiplier_ceiling": 1.5,
    "recovery_risk_multiplier": 0.5,
}


def make_signal(**over) -> CandidateSignal:
    now = utcnow()
    atr = 2.85
    entry = 231.40
    d = dict(
        signal_id=new_id(IdKind.SIGNAL),
        snapshot_id=new_id(IdKind.SNAPSHOT),
        symbol="AAPL",
        direction=Direction.BUY,
        strategy_id="S7",
        entry=entry,
        stop_loss=entry - 1.5 * atr,
        take_profit=entry + 3.0 * atr,
        stop_distance=1.5 * atr,
        reward_risk=2.0,
        atr_at_signal=atr,
        base_score=50.0,
        bonus=8.0,
        total_score=58.0,
        expected_value=0.75,
        ev_is_bootstrap=True,
        combined_priority=54.4,
        regime=MarketRegime.WEAK_UP,
        expires_at=expires_at(now, 30),
    )
    d.update(over)
    return CandidateSignal(**d)


def make_account(**over) -> AccountState:
    d = dict(balance=100_000.0, equity=100_000.0, day_start_equity=100_000.0,
             equity_high_water=100_000.0)
    d.update(over)
    return AccountState(**d)


ENGINE = DeterministicRiskEngine(CFG)


# ---------------------------------------------------------------- happy path

def test_clean_signal_passes_and_sizes():
    r = ENGINE.evaluate(signal=make_signal(), account=make_account(),
                        portfolio=PortfolioState())
    assert r.verdict is RiskGateVerdict.PASS
    # 1% of 100k = $1000 risk / $4.275 stop = 233 shares
    assert r.approved_quantity == 233
    assert r.failed_checks == []


# ------------------------------------------------------- the golden rule §54

def test_rejects_on_heat_even_with_perfect_signal():
    """§54: the engine may reject when every agent said BUY."""
    positions = [
        # qty 1000 x $2.00 stop = $2,000 = 2.0% of a $100k account
        OpenPosition(symbol=s, side=Direction.BUY, quantity=1000,
                     entry_price=100.0, stop_loss=98.0)
        for s in ("MSFT", "NVDA", "AMD", "TSLA")            # = 8.0% heat
    ]
    r = ENGINE.evaluate(signal=make_signal(total_score=99.0),
                        account=make_account(),
                        portfolio=PortfolioState(positions=positions))
    assert r.verdict is RiskGateVerdict.REJECT
    assert "heat" in r.blocking_reason.lower()
    assert r.portfolio_heat_pct == pytest.approx(8.0)


def test_all_checks_recorded_not_short_circuited():
    """Our one change from the source: the full audit trail must survive."""
    r = ENGINE.evaluate(signal=make_signal(total_score=10.0),
                        account=make_account(manually_paused=True),
                        portfolio=PortfolioState())
    assert r.verdict is RiskGateVerdict.REJECT
    assert len(r.checks) > 10                       # everything evaluated
    assert len(r.failed_checks) >= 2                # pause AND score both recorded
    assert r.blocking_reason == "manually paused"   # first in original order wins


# ---------------------------------------------------------------- each rule

@pytest.mark.parametrize("kwargs,expect", [
    (dict(manually_paused=True), "manually paused"),
    (dict(halted_daily=True), "daily loss limit"),
    (dict(halted_weekly=True), "weekly loss limit"),
    (dict(profit_locked=True), "daily profit lock"),
    (dict(health_ok=False), "health watchdog"),
    (dict(consecutive_losses=6), "consec-loss cooldown"),
])
def test_account_halts_block(kwargs, expect):
    r = ENGINE.evaluate(signal=make_signal(), account=make_account(**kwargs),
                        portfolio=PortfolioState())
    assert r.verdict is RiskGateVerdict.REJECT
    assert r.blocking_reason == expect


def test_score_below_minimum_blocks():
    r = ENGINE.evaluate(signal=make_signal(total_score=47.9), account=make_account(),
                        portfolio=PortfolioState())
    assert r.verdict is RiskGateVerdict.REJECT
    assert r.blocking_reason == "score below min"


def test_negative_ev_blocks():
    r = ENGINE.evaluate(signal=make_signal(expected_value=-0.1), account=make_account(),
                        portfolio=PortfolioState())
    assert r.verdict is RiskGateVerdict.REJECT
    assert r.blocking_reason == "non-positive EV"


def test_bootstrap_ev_is_flagged_not_hidden():
    """Honesty check: a bootstrap EV passes but says so in the audit trail."""
    r = ENGINE.evaluate(signal=make_signal(), account=make_account(),
                        portfolio=PortfolioState())
    ev = next(c for c in r.checks if c.rule == "positive_ev")
    assert ev.passed and "bootstrap" in ev.reason


def test_duplicate_position_blocks():
    p = PortfolioState(positions=[
        OpenPosition(symbol="AAPL", side=Direction.BUY, quantity=10,
                     entry_price=230.0, stop_loss=228.0, strategy_id="S7")
    ])
    r = ENGINE.evaluate(signal=make_signal(), account=make_account(), portfolio=p)
    assert r.verdict is RiskGateVerdict.REJECT
    assert "already open" in r.blocking_reason


def test_expired_signal_blocks():
    old = utcnow() - timedelta(seconds=120)
    r = ENGINE.evaluate(signal=make_signal(expires_at=expires_at(old, 30)),
                        account=make_account(), portfolio=PortfolioState())
    assert r.verdict is RiskGateVerdict.REJECT
    assert r.blocking_reason == "signal expired"


def test_wide_spread_and_large_gap_block():
    for kw, expect in ((dict(spread_pct=0.5), "spread too wide"),
                       (dict(gap_pct=-5.0), "overnight gap too large")):
        r = ENGINE.evaluate(signal=make_signal(), account=make_account(),
                            portfolio=PortfolioState(), **kw)
        assert r.verdict is RiskGateVerdict.REJECT
        assert r.blocking_reason == expect


def test_correlated_same_direction_blocks():
    import math
    base = [100 * math.exp(0.01 * i) for i in range(40)]
    p = PortfolioState(positions=[
        OpenPosition(symbol="MSFT", side=Direction.BUY, quantity=10,
                     entry_price=400.0, stop_loss=396.0)
    ])
    r = ENGINE.evaluate(signal=make_signal(), account=make_account(), portfolio=p,
                        closes_by_symbol={"AAPL": base, "MSFT": base})
    assert r.verdict is RiskGateVerdict.REJECT
    assert "correlated" in r.blocking_reason


# ------------------------------------------------------------- REDUCE path

def test_heat_above_reduce_level_halves_size():
    """§53 REDUCE: heat between 6% and 8% must size down, not reject."""
    positions = [
        # qty 1000 x $2.50 stop = $2,500 = 2.5% each -> 7.5% total,
        # which sits between heat_reduce_level (6%) and heat_max (8%)
        OpenPosition(symbol=s, side=Direction.BUY, quantity=1000,
                     entry_price=100.0, stop_loss=97.5)
        for s in ("MSFT", "NVDA", "AMD")
    ]
    r = ENGINE.evaluate(signal=make_signal(), account=make_account(),
                        portfolio=PortfolioState(positions=positions))
    assert r.verdict is RiskGateVerdict.REDUCE
    assert r.size_multiplier == 0.5
    assert r.approved_quantity == 116          # half of 233


def test_recovery_mode_halves_size():
    r = ENGINE.evaluate(signal=make_signal(),
                        account=make_account(recovery_mode=True),
                        portfolio=PortfolioState())
    assert r.verdict is RiskGateVerdict.REDUCE
    assert r.size_multiplier_breakdown["recovery"] == 0.5


def test_multipliers_clamp_to_floor():
    """heat 0.5 x recovery 0.5 = 0.25, exactly the floor. Must not go below."""
    positions = [
        OpenPosition(symbol=s, side=Direction.BUY, quantity=1000,
                     entry_price=100.0, stop_loss=97.5)
        for s in ("MSFT", "NVDA", "AMD")
    ]
    r = ENGINE.evaluate(signal=make_signal(),
                        account=make_account(recovery_mode=True),
                        portfolio=PortfolioState(positions=positions))
    assert r.size_multiplier == 0.25


# ------------------------------------------------------------ measures unit

def test_position_without_stop_counts_as_full_risk():
    """Bot v6 PositionRiskPct returns InpRiskPerTrade when sl<=0. Assuming zero
    would understate exposure, which is the dangerous direction."""
    p = PortfolioState(positions=[
        OpenPosition(symbol="X", side=Direction.BUY, quantity=10, entry_price=100.0)
    ])
    assert portfolio_heat_pct(p, make_account(), 1.0) == 1.0


def test_sizing_never_exceeds_risk_budget():
    for stop in (0.01, 0.5, 4.275, 50.0, 300.0):
        s = size_position(account=make_account(), portfolio=PortfolioState(),
                          stop_distance=stop, stats=StrategyStats(strategy_id="S7"),
                          cfg=CFG)
        assert s.risk_amount <= 100_000 * 0.01 * 1.01 + 1e-6


def test_kelly_stays_shadow_until_layer_active():
    stats = StrategyStats(strategy_id="S7", trades=100, win_rate=0.6,
                          avg_win=2.0, avg_loss=1.0)
    shadow = size_position(account=make_account(), portfolio=PortfolioState(),
                           stop_distance=4.275, stats=stats, cfg=CFG)
    live = size_position(account=make_account(kelly_layer_active=True),
                         portfolio=PortfolioState(), stop_distance=4.275,
                         stats=stats, cfg=CFG)
    assert "kelly" not in shadow.breakdown
    assert live.breakdown["kelly"] > 1.0


def test_correlation_matches_known_values():
    a = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert pearson_correlation(a, a) == pytest.approx(1.0)
    assert pearson_correlation(a, list(reversed(a))) == pytest.approx(-1.0)
    assert pearson_correlation([1.0, 2.0], [1.0, 2.0]) == 0.0   # n<5 guard


# ------------------------------------------------------------ fail closed

def test_engine_refuses_fail_open_config():
    with pytest.raises(RiskEngineError):
        DeterministicRiskEngine({**CFG, "fail_closed": False})


def test_determinism():
    """Same inputs, same verdict — 50 times. This is the property the pitch claims."""
    sig, acc, pf = make_signal(), make_account(), PortfolioState()
    results = {(r.verdict, r.approved_quantity, r.size_multiplier)
               for r in (ENGINE.evaluate(signal=sig, account=acc, portfolio=pf)
                         for _ in range(50))}
    assert len(results) == 1


# ================================================================ sector cap
# The sector cap is the equity analogue of Bot v6's CurrencyExposure() and is the
# only exposure rule that was translated rather than ported. Until now it had no
# test at all, so nothing proved the translated rule actually blocks anything.

def _tech(symbol: str, sector: str | None = "Technology") -> OpenPosition:
    """qty 1000 x $2.00 stop = $2,000 = 2.0% of a $100k account."""
    return OpenPosition(symbol=symbol, side=Direction.BUY, quantity=1000,
                        entry_price=100.0, stop_loss=98.0, sector=sector)


def test_sector_exposure_cap_blocks():
    #   3 x 2.0% = 6.0% already in Technology, + 1.0% for this trade = 7.0% > 6.0%
    pf = PortfolioState(positions=[_tech(s) for s in ("MSFT", "NVDA", "AMD")])
    r = ENGINE.evaluate(signal=make_signal(), account=make_account(),
                        portfolio=pf, sector="Technology")
    assert r.verdict is RiskGateVerdict.REJECT
    assert "sector exposure" in r.blocking_reason.lower()
    assert "Technology" in r.blocking_reason


def test_sector_exposure_counts_only_the_matching_sector():
    """An Energy book must not consume the Technology budget."""
    pf = PortfolioState(positions=[_tech(s, "Energy") for s in ("XOM", "CVX", "COP")])
    r = ENGINE.evaluate(signal=make_signal(), account=make_account(),
                        portfolio=pf, sector="Technology")
    assert r.verdict is not RiskGateVerdict.REJECT
    sec = [c for c in r.checks if c.rule == "sector_exposure"][0]
    assert sec.passed and sec.observed == pytest.approx(1.0)   # this trade only


def test_sector_check_absent_when_sector_unknown():
    """No sector supplied means the rule cannot be evaluated, so it is not
    recorded as a silent pass — it is simply not among the checks."""
    r = ENGINE.evaluate(signal=make_signal(), account=make_account(),
                        portfolio=PortfolioState(), sector=None)
    assert [c for c in r.checks if c.rule == "sector_exposure"] == []


def test_sector_exposure_pct_treats_an_unstopped_position_as_full_risk():
    """Mirrors PositionRiskPct(): no stop is assumed to be a full unit of risk,
    never zero. Assuming zero is the dangerous direction."""
    pf = PortfolioState(positions=[
        OpenPosition(symbol="MSFT", side=Direction.BUY, quantity=1000,
                     entry_price=100.0, stop_loss=None, sector="Technology"),
    ])
    exp = sector_exposure_pct(pf, make_account(), "Technology", 1.0)
    assert exp == pytest.approx(1.0)          # substituted, not NaN, not 0.0
    assert exp == exp                          # not NaN


def test_sector_exposure_pct_is_zero_without_a_sector():
    assert sector_exposure_pct(PortfolioState(positions=[_tech("MSFT")]),
                               make_account(), None, 1.0) == 0.0


# ====================================================== size rounds to zero
# A sub-one-share result must REJECT, not silently submit a 0-share order.

def test_size_rounding_to_zero_shares_is_rejected():
    #   1% of $400 = $4.00 risk budget / $4.275 stop = 0.94 shares -> floor 0
    tiny = make_account(balance=400.0, equity=400.0,
                        day_start_equity=400.0, equity_high_water=400.0)
    r = ENGINE.evaluate(signal=make_signal(), account=tiny,
                        portfolio=PortfolioState())
    assert r.verdict is RiskGateVerdict.REJECT
    assert "zero shares" in r.blocking_reason
    assert r.approved_quantity is None or r.approved_quantity == 0


def test_zero_share_rejection_is_recorded_as_a_check_not_just_a_reason():
    tiny = make_account(balance=400.0, equity=400.0,
                        day_start_equity=400.0, equity_high_water=400.0)
    r = ENGINE.evaluate(signal=make_signal(), account=tiny,
                        portfolio=PortfolioState())
    q = [c for c in r.checks if c.rule == "computed_quantity"]
    assert len(q) == 1 and not q[0].passed


# ========================================================= revalidation §57
# The Portfolio Manager may modify a proposal, and every modification must come
# back through this engine. These tests pin the property that makes that safe.

def test_revalidate_applies_the_identical_rule_set():
    sig, acc, pf = make_signal(), make_account(), PortfolioState()
    a = ENGINE.evaluate(signal=sig, account=acc, portfolio=pf)
    b = ENGINE.revalidate(signal=sig, account=acc, portfolio=pf)
    assert (a.verdict, a.approved_quantity, a.size_multiplier) == \
           (b.verdict, b.approved_quantity, b.size_multiplier)
    assert [c.rule for c in a.checks] == [c.rule for c in b.checks]


def test_revalidate_rejects_what_evaluate_rejects():
    """Revalidation is not a second chance. A breach stays a breach."""
    pf = PortfolioState(positions=[_tech(s) for s in ("MSFT", "NVDA", "AMD")])
    kw = dict(signal=make_signal(), account=make_account(),
              portfolio=pf, sector="Technology")
    assert ENGINE.evaluate(**kw).verdict is RiskGateVerdict.REJECT
    assert ENGINE.revalidate(**kw).verdict is RiskGateVerdict.REJECT


def test_revalidate_cannot_be_handed_a_quantity_to_approve():
    """The engine is the sole sizing authority: there is no parameter through
    which a caller can propose, request or inject an approved quantity."""
    kw = dict(signal=make_signal(), account=make_account(),
              portfolio=PortfolioState())
    with pytest.raises(TypeError):
        ENGINE.revalidate(**kw, approved_quantity=10_000)
    with pytest.raises(TypeError):
        ENGINE.revalidate(**kw, quantity=10_000)


def test_revalidate_after_a_portfolio_change_reflects_the_new_portfolio():
    """The PM cannot revalidate against the portfolio the proposal was sized on."""
    sig, acc = make_signal(), make_account()
    assert ENGINE.evaluate(signal=sig, account=acc,
                           portfolio=PortfolioState()).verdict is RiskGateVerdict.PASS
    filled_up = PortfolioState(positions=[_tech(s) for s in
                                          ("MSFT", "NVDA", "AMD", "TSLA")])
    assert ENGINE.revalidate(signal=sig, account=acc,
                             portfolio=filled_up).verdict is RiskGateVerdict.REJECT

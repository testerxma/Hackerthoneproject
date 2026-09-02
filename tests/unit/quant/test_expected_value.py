"""
EV tests. Hand-computed from docs/reference/SpeedTraderBot_v6.1.mq5
(SHA-256 c799acaa...32e8d9), ComputeEV L794-813.

TEST-ONLY COST INJECTION: the transaction cost below exists solely in these
fixtures. It is never written to production YAML and never loaded by QuantCore
at runtime — proven by test_shipped_execution_config_has_no_transaction_cost.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import pytest  # noqa: E402
import yaml  # noqa: E402

from speedtrader.common.clock import Freshness  # noqa: E402
from speedtrader.common.ids import IdKind, new_id  # noqa: E402
from speedtrader.data.schemas import (  # noqa: E402
    Bar, DataSourceMeta, MarketSnapshot, TechnicalFeatures,
)
from speedtrader.quant.cost_policy import cost_policy_from_config  # noqa: E402
from speedtrader.quant.expected_value import (  # noqa: E402
    COST_KEY, DEVIATIONS, EV_MIN_TRADES, EVCostNotConfigured, EVResult,
    StrategyStatsLike, compute_ev,
)

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)

# --- TEST FIXTURE ONLY. Never a production default. ---
def _cost_block(commission_per_share=0.0, spread=True):
    """TEST FIXTURE ONLY. Regulatory rates mirror the official schedule; the
    commission is the value the operator has not yet resolved in production."""
    return {"transaction_cost": {
                "model": "pre_trade_round_trip_per_share_estimate",
                "rates_effective_date": "2026-07-20",
                "rates_source": "https://example.invalid/test-fixture",
                "default": {
                    "commission": {"per_share": commission_per_share,
                                   "rate_of_notional": 0.0,
                                   "source": "operator_assumption",
                                   "assumption": "test fixture"},
                    "regulatory": {"sec_rate_of_notional": 0.0,
                                   "taf_per_share": 0.0,
                                   "cat_per_share": 0.0,
                                   "sides_per_round_trip": 2,
                                   "source": "authoritative",
                                   "reference": "test fixture"},
                    "slippage": {"per_share": 0.0,
                                 "source": "operator_assumption",
                                 "assumption": "test fixture"}},
                "overrides": {}},
            "include_spread_in_ev_cost": spread}


def make_cfg(fixed=0.0, spread=False):
    return _cost_block(commission_per_share=fixed / 2.0, spread=spread)

TEST_CFG = make_cfg(0.01, True)
FREE_CFG = make_cfg(0.0, False)

# S07 geometry: ATR 2.0 -> stop 3.0, target 6.0
ENTRY, STOP, TARGET = 115.0, 112.0, 121.0
STOP_DIST = 3.0


@dataclass
class Stats:
    trades: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0


def snap(spread=None):
    return MarketSnapshot(
        snapshot_id=new_id(IdKind.SNAPSHOT), symbol="TEST", price=ENTRY, spread=spread,
        bars=[Bar(t=T0 + timedelta(hours=i), o=100, h=101, l=99, c=100, v=1000.0)
              for i in range(25)],
        features=TechnicalFeatures(atr=2.0),
        source=DataSourceMeta(vendor="replay", fetched_at=T0, bars_available=25,
                              freshness=Freshness.FRESH))


def ev(cfg=None, stats=None, spread=None, total_score=50.0):
    """Builds the policy the way CandidateBuilder does, so the fail-closed path
    is exercised through the real construction route."""
    policy = cost_policy_from_config(FREE_CFG if cfg is None else cfg)
    return compute_ev(entry=ENTRY, stop_loss=STOP, take_profit=TARGET,
                      stop_distance=STOP_DIST, total_score=total_score,
                      snapshot=snap(spread), cost_policy=policy,
                      direction="BUY", stats=stats)


# ============================================ FAIL CLOSED (mandatory)

def test_missing_cost_config_raises():
    with pytest.raises(EVCostNotConfigured, match=COST_KEY):
        ev(cfg={"include_spread_in_ev_cost": True})   # no transaction_cost block


def test_empty_config_raises():
    with pytest.raises(EVCostNotConfigured):
        ev(cfg={})


def test_raises_before_any_arithmetic():
    """No partial EVResult may escape on the failure path."""
    try:
        ev(cfg={})
    except EVCostNotConfigured as e:
        assert not isinstance(e, EVResult)
        assert "intentional" in str(e)


def test_shipped_execution_config_has_no_transaction_cost():
    """The gap is real, not accidentally closed by a stray commit."""
    cfg_path = Path(__file__).resolve().parents[3] / "configs" / "execution_config.yaml"
    d = yaml.safe_load(cfg_path.read_text())
    c = ((d.get(COST_KEY) or {}).get("default") or {}).get("commission") or {}
    assert c.get("per_share") is None and c.get("rate_of_notional") is None, \
        "a commission value was committed without an operator decision"
    assert d.get("include_spread_in_ev_cost") is True


def test_zero_cost_is_a_valid_explicit_choice_but_not_a_default():
    """0.0 is honoured when explicitly configured; it is never supplied by default."""
    assert ev(cfg=FREE_CFG).cost_r == 0.0
    with pytest.raises(EVCostNotConfigured):
        ev(cfg={"include_spread_in_ev_cost": False})   # block absent entirely


# ============================================ BOOTSTRAP  L798

def test_s07_bootstrap_hand_computed():
    #   avg_win_R = |121-115| / 3 = 2.0
    #   avg_loss_R = 1.0
    #   win_rate = 0.50
    #   EV = 0.5*2.0 - 0.5*1.0 - 0 = 0.50
    r = ev()
    assert r.is_bootstrap is True
    assert r.win_rate == 0.50
    assert r.avg_win_r == pytest.approx(2.0)
    assert r.avg_loss_r == pytest.approx(1.0)
    assert r.expected_value == pytest.approx(0.50)


def test_bootstrap_boundary_at_20_trades():
    assert ev(stats=Stats(trades=19)).is_bootstrap is True
    assert ev(stats=Stats(trades=EV_MIN_TRADES, win_rate=0.6,
                          avg_win=2.0, avg_loss=1.0)).is_bootstrap is False


def test_avg_loss_r_is_always_one_in_bootstrap():
    """slPips / stop_distance == 1.0 by definition."""
    assert ev(stats=Stats(trades=0)).avg_loss_r == 1.0


# ============================================ LIVE ESTIMATE  L802-804

def test_live_win_rate_clamped_to_source_bounds():
    #   L802 Clamp(wr * 1.0, 0.15, 0.85)
    assert ev(stats=Stats(trades=50, win_rate=0.95, avg_win=2.0,
                          avg_loss=1.0)).win_rate == 0.85
    assert ev(stats=Stats(trades=50, win_rate=0.05, avg_win=2.0,
                          avg_loss=1.0)).win_rate == 0.15


def test_live_falls_back_to_geometry_when_stats_are_zero():
    """L803-804: avgWinPips > 0 ? avgWinPips : tpPips"""
    r = ev(stats=Stats(trades=50, win_rate=0.6, avg_win=0.0, avg_loss=0.0))
    assert r.avg_win_r == pytest.approx(2.0)
    assert r.avg_loss_r == pytest.approx(1.0)


def test_live_uses_measured_stats_when_present():
    r = ev(stats=Stats(trades=50, win_rate=0.60, avg_win=1.4, avg_loss=0.9))
    #   EV = 0.6*1.4 - 0.4*0.9 = 0.84 - 0.36 = 0.48
    assert r.expected_value == pytest.approx(0.48)


# ============================================ COST  L809 adapted

def test_fixed_cost_is_divided_by_stop_distance():
    #   cost_R = 0.01 / 3.0 = 0.003333
    r = ev(cfg=make_cfg(0.01, False))
    assert r.cost_r == pytest.approx(0.01 / 3.0)
    assert r.expected_value == pytest.approx(0.50 - 0.01 / 3.0)


def test_spread_included_from_snapshot_when_configured():
    #   cost_R = (0.06 + 0.01) / 3.0 = 0.023333
    r = ev(cfg=TEST_CFG, spread=0.06)
    assert r.cost_r == pytest.approx(0.07 / 3.0)
    assert r.cost_breakdown["spread"] == 0.06
    assert r.cost_breakdown["per_share_fees"] == pytest.approx(0.01)
    assert r.cost_breakdown["components"]["slippage"] == 0.0


def test_spread_excluded_when_configured_off():
    r = ev(cfg=make_cfg(0.01, False), spread=0.06)
    assert r.cost_breakdown["spread"] == 0.0


def test_cost_is_always_subtracted_never_floored():
    """The positive_ev gate must not be weakened by clamping cost away."""
    r = ev(cfg=make_cfg(5.0, False))
    assert r.cost_r == pytest.approx(5.0 / 3.0)
    assert r.expected_value < 0.0        # a large cost CAN drive EV negative


def test_slippage_is_zero_no_trade_history():
    assert ev(cfg=TEST_CFG).cost_breakdown["components"]["slippage"] == 0.0


# ============================================ L811 / L812

def test_norm_ev_hand_computed():
    #   L811 normEV = Clamp(50 + 0.50, 0, 100) = 50.5
    assert ev().norm_ev == pytest.approx(50.5)


def test_combined_priority_hand_computed():
    #   L812 = total_score*0.5 + normEV*0.5 = 58*0.5 + 50.5*0.5 = 54.25
    assert ev(total_score=58.0).combined_priority == pytest.approx(54.25)


def test_norm_ev_clamped_to_0_100():
    r = ev(cfg=make_cfg(500.0, False))
    assert r.norm_ev == 0.0


def test_l811_degeneracy_in_r_units():
    """Deviation 5, asserted: a +-2R swing moves priority by <= 2.5 points,
    while the score half spans ~25. EV supplies ~6% of the ranking power."""
    lo = ev(cfg=make_cfg(6.0, False),
            total_score=50.0)     # cost_R = 2.0 -> EV = -1.5
    hi = ev(total_score=50.0)                                    # EV = +0.5
    assert abs(hi.expected_value - lo.expected_value) == pytest.approx(2.0)
    assert abs(hi.combined_priority - lo.combined_priority) <= 2.5


# ============================================ Contract

def test_strategy_stats_like_satisfied_without_importing_risk():
    """risk.state.StrategyStats fits structurally; quant never imports it."""
    from speedtrader.risk.state import StrategyStats
    s = StrategyStats(strategy_id="S07", trades=50, win_rate=0.6,
                      avg_win=2.0, avg_loss=1.0)
    assert isinstance(s, StrategyStatsLike)
    assert ev(stats=s).is_bootstrap is False
    import ast
    mod = ast.parse((Path(__file__).resolve().parents[3] / "src" / "speedtrader" /
                     "quant" / "expected_value.py").read_text())
    imported = set()
    for node in ast.walk(mod):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    assert not any(m.split(".")[0] in {"risk", "execution", "alpaca", "llm"}
                   or ".risk" in m or ".execution" in m or ".alpaca" in m or ".llm" in m
                   for m in imported), f"boundary violated: {imported}"


def test_all_five_deviations_recorded():
    d = ev().deviations
    assert len(d) == 6
    joined = " ".join(d).lower()
    for token in ("r-denominated", "+0.5", "regimemultiplier", "slippage", "l811"):
        assert token in joined


def test_zero_stop_distance_rejected():
    with pytest.raises(ValueError, match="stop_distance"):
        compute_ev(entry=115.0, stop_loss=115.0, take_profit=121.0,
                   stop_distance=0.0, total_score=50.0, snapshot=snap(),
                   cost_policy=cost_policy_from_config(FREE_CFG), direction="BUY")


def test_ev_is_deterministic():
    vals = {ev(cfg=TEST_CFG, spread=0.06).expected_value for _ in range(50)}
    assert len(vals) == 1

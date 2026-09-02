"""
CandidateBuilder and QuantCore tests.

TEST-ONLY COST INJECTION: TEST_CFG below exists solely in this fixture. It is
never written to production YAML and never loaded by QuantCore at runtime.
"""
from __future__ import annotations

import ast
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import pytest  # noqa: E402
import yaml  # noqa: E402

from speedtrader.common.clock import Freshness  # noqa: E402
from speedtrader.common.ids import IdKind, new_id  # noqa: E402
from speedtrader.data.schemas import (  # noqa: E402
    Bar, DataSourceMeta, Direction, MarketRegime, MarketSnapshot,
    RiskGateVerdict, TechnicalFeatures,
)
from speedtrader.quant.candidate import (  # noqa: E402
    STRATEGY_ID_TO_CONFIG_KEY, STRATEGY_ID_TO_MQL5_INDEX, CandidateBuilder,
)
from speedtrader.quant.engine import QuantCore  # noqa: E402
from speedtrader.quant.expected_value import COST_KEY, EVCostNotConfigured  # noqa: E402
from speedtrader.quant.strategies.s07 import S07MomentumBreakout  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
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


TEST_CFG = {**_cost_block(), "signal_ttl_seconds": 30}

S07 = S07MomentumBreakout()
ATR, EMA200 = 2.0, 100.0
BUY_BAR = (111.0, 116.0, 110.0, 115.0)


def make_bars(n=25, last=BUY_BAR, vol=1000.0, last_vol=1000.0):
    bars = [Bar(t=T0 + timedelta(hours=i), o=100.0, h=110.0, l=90.0, c=100.0, v=vol)
            for i in range(n)]
    o, h, l, c = last
    bars[-1] = Bar(t=bars[-1].t, o=o, h=h, l=l, c=c, v=last_vol)
    return bars


def make_snap(bars=None, spread=0.06):
    bars = bars if bars is not None else make_bars()
    return MarketSnapshot(
        snapshot_id=new_id(IdKind.SNAPSHOT), symbol="TEST", price=bars[-1].c,
        spread=spread, bars=bars,
        features=TechnicalFeatures(atr=ATR, ema200=EMA200, di_plus=30.0, di_minus=10.0),
        regime=MarketRegime.UNKNOWN,
        source=DataSourceMeta(vendor="replay", fetched_at=T0, bars_available=len(bars),
                              freshness=Freshness.FRESH,
                              notes="SIMULATED DATA — not live Alpaca"))


def build_one(**kw):
    b = CandidateBuilder(TEST_CFG)
    out = S07.evaluate(make_snap()).output
    return b.build(out, make_snap(), **kw)


# ============================================ FAIL CLOSED (mandatory)

def test_builder_raises_on_construction_without_cost():
    with pytest.raises(EVCostNotConfigured, match=COST_KEY):
        CandidateBuilder({"signal_ttl_seconds": 30})


def test_builder_raises_on_build_if_constructor_bypassed():
    b = CandidateBuilder(TEST_CFG)
    object.__setattr__(b, "execution_config", {"signal_ttl_seconds": 30})
    with pytest.raises(EVCostNotConfigured):
        b.build(S07.evaluate(make_snap()).output, make_snap())


def test_quantcore_refuses_to_start_without_cost():
    """Fails at startup, not silently for a whole session."""
    with pytest.raises(EVCostNotConfigured):
        QuantCore([S07], {"signal_ttl_seconds": 30})


def test_production_config_builds_a_quant_core():
    """Commission was resolved 2026-09-02, so the shipped config now runs."""
    prod = yaml.safe_load((ROOT / "configs" / "execution_config.yaml").read_text())
    QuantCore([S07], prod)          # must not raise


def test_production_config_recloses_if_the_commission_decision_is_removed():
    """The fail-closed gate is satisfied, not deleted."""
    prod = yaml.safe_load((ROOT / "configs" / "execution_config.yaml").read_text())
    del prod[COST_KEY]["default"]["commission"]
    with pytest.raises(EVCostNotConfigured):
        QuantCore([S07], prod)


def test_shipped_config_states_a_basis_for_every_rate():
    """Guards the original intent — no fee rate without a recorded source."""
    prod = yaml.safe_load((ROOT / "configs" / "execution_config.yaml").read_text())
    default = ((prod.get(COST_KEY) or {}).get("default") or {})
    for name in ("commission", "regulatory", "slippage"):
        assert default[name].get("source"), f"{name} rate committed with no source"
    assert prod.get("include_spread_in_ev_cost") is True


# ============================================ Mapping S07 -> CandidateSignal

def test_prices_preserved_exactly():
    out = S07.evaluate(make_snap()).output
    c = CandidateBuilder(TEST_CFG).build(out, make_snap())
    assert c.entry == out.entry == 115.0
    assert c.stop_loss == out.stop_loss == 112.0
    assert c.take_profit == out.take_profit == 121.0


def test_stop_distance_and_reward_risk_derived_from_geometry():
    c = build_one()
    assert c.stop_distance == pytest.approx(3.0)
    assert c.reward_risk == pytest.approx(2.0)
    assert c.reward_risk == pytest.approx(
        abs(c.take_profit - c.entry) / abs(c.entry - c.stop_loss))


def test_snapshot_derived_fields():
    s = make_snap()
    c = CandidateBuilder(TEST_CFG).build(S07.evaluate(s).output, s)
    assert c.snapshot_id == s.snapshot_id
    assert c.symbol == s.symbol
    assert c.atr_at_signal == s.features.atr
    assert c.regime is s.regime is MarketRegime.UNKNOWN


def test_strategy_id_and_legacy_mapping():
    c = build_one()
    assert c.strategy_id == "S07"
    assert STRATEGY_ID_TO_CONFIG_KEY["S07"] == "S7"
    assert STRATEGY_ID_TO_MQL5_INDEX["S07"] == 6


def test_base_score_from_strategy_ev_from_layer():
    c = build_one()
    assert c.base_score == 50.0
    assert c.ev_is_bootstrap is True
    #   fixture: all fee rates 0.0, so the only cost is the snapshot spread
    #   EV = 0.5*2.0 - 0.5*1.0 - 0.06/3.0 = 1.0 - 0.5 - 0.02 = 0.48
    assert c.expected_value == pytest.approx(0.48)


def test_single_strategy_vote_not_presented_as_consensus():
    c = build_one()
    assert len(c.strategy_votes) == 1
    v = c.strategy_votes[0]
    assert v.strategy_id == "S07" and v.direction is Direction.BUY
    assert "sole enabled strategy" in v.notes


def test_ttl_is_thirty_seconds():
    c = build_one(now=T0)
    assert (c.expires_at - c.created_at).total_seconds() == 30.0


def test_zero_stop_distance_rejected():
    from speedtrader.quant.strategies.base import StrategyOutput
    bad = StrategyOutput(strategy_id="S07", direction=Direction.BUY, entry=100.0,
                         stop_loss=100.0, take_profit=110.0, base_score=50.0)
    with pytest.raises(ValueError, match="zero stop distance"):
        CandidateBuilder(TEST_CFG).build(bad, make_snap())


def test_schema_geometry_validator_still_fires():
    from speedtrader.quant.strategies.base import StrategyOutput
    inverted = StrategyOutput(strategy_id="S07", direction=Direction.BUY, entry=100.0,
                              stop_loss=110.0, take_profit=120.0, base_score=50.0)
    with pytest.raises(Exception, match="geometry"):
        CandidateBuilder(TEST_CFG).build(inverted, make_snap())


# ============================================ What it must NOT contain

@pytest.mark.parametrize("forbidden", [
    "quantity", "risk_amount", "risk_percent", "approved", "authorization",
    "approval", "verdict", "size_multiplier", "order_id", "client_order_id",
])
def test_candidate_carries_no_execution_authority(forbidden):
    assert not hasattr(build_one(), forbidden), f"candidate leaked {forbidden}"


def test_candidate_cannot_substitute_for_an_authorization_type():
    from speedtrader.data.schemas import RiskGateResult
    assert not isinstance(build_one(), RiskGateResult)


# ============================================ Import boundary

@pytest.mark.parametrize("module", [
    "candidate.py", "engine.py", "scoring.py", "expected_value.py",
])
def test_quant_modules_import_nothing_from_risk_execution_alpaca_llm(module):
    tree = ast.parse((ROOT / "src" / "speedtrader" / "quant" / module).read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
    banned = {"risk", "execution", "alpaca", "llm"}
    for n in names:
        parts = n.split(".")
        assert not (banned & set(parts)), f"{module} imports {n}"


# ============================================ Risk engine acceptance

def _engine():
    from speedtrader.config import get_config
    from speedtrader.risk.engine import DeterministicRiskEngine
    return DeterministicRiskEngine(get_config().risk)


def _account():
    from speedtrader.risk.state import AccountState
    return AccountState(balance=100_000, equity=100_000,
                        day_start_equity=100_000, equity_high_water=100_000)


def test_candidate_accepted_by_unmodified_risk_engine():
    from speedtrader.risk.state import PortfolioState
    bars = make_bars(last=(100.0, 110.0, 100.0, 108.0), last_vol=4000.0)
    bars[-1] = Bar(t=bars[-1].t, o=111.0, h=116.0, l=110.0, c=115.0, v=4000.0)
    s = make_snap(bars)
    c = CandidateBuilder(TEST_CFG).build(S07.evaluate(s).output, s)
    r = _engine().evaluate(signal=c, account=_account(), portfolio=PortfolioState())
    assert r.verdict in (RiskGateVerdict.PASS, RiskGateVerdict.REDUCE)


def test_penalised_candidate_rejected_on_score_by_unmodified_engine():
    """The min_score gate is LIVE, not a constant."""
    from speedtrader.risk.state import PortfolioState
    bars = make_bars(last=(111.0, 130.0, 110.0, 115.0), last_vol=1000.0)
    s = make_snap(bars)
    c = CandidateBuilder(TEST_CFG).build(S07.evaluate(s).output, s)
    assert c.total_score < 48.0
    r = _engine().evaluate(signal=c, account=_account(), portfolio=PortfolioState())
    assert r.verdict is RiskGateVerdict.REJECT
    assert r.blocking_reason == "score below min"


# ============================================ QuantCore

def test_quantcore_produces_a_candidate():
    r = QuantCore([S07], TEST_CFG).run(make_snap())
    assert r.ok and r.candidate is not None
    assert r.candidate.strategy_id == "S07"


def test_quantcore_returns_reason_when_no_signal():
    flat = make_bars(last=(100.0, 101.0, 99.0, 100.0))
    r = QuantCore([S07], TEST_CFG).run(make_snap(flat))
    assert not r.ok and r.candidate is None
    assert "S07" in r.evaluated and r.evaluated["S07"]


def test_quantcore_with_no_strategies():
    r = QuantCore([], TEST_CFG).run(make_snap())
    assert not r.ok and r.code == "no_strategies"


def test_quantcore_contains_no_strategy_or_risk_logic():
    src = (ROOT / "src" / "speedtrader" / "quant" / "engine.py").read_text()
    for token in ("1.5*", "3.0*", "atr *", "portfolio_heat", "size_position",
                  "submit_order", "ExecutionAuthorization"):
        assert token not in src, f"engine.py contains {token}"


def test_strategy_config_enabled_list_matches_disk():
    d = yaml.safe_load((ROOT / "configs" / "strategy_config.yaml").read_text())
    enabled = {k for k, v in d["strategies"].items() if v.get("enabled")}
    on_disk = {f.stem.upper().replace("S0", "S")
               for f in (ROOT / "src" / "speedtrader" / "quant" / "strategies").glob("s*.py")
               if f.stem not in ("base",)}
    assert enabled == on_disk == {"S7"}


# ============================================ Determinism

def test_deterministic_except_for_id_and_timestamp():
    s = make_snap()
    out = S07.evaluate(s).output
    b = CandidateBuilder(TEST_CFG)
    a, c = b.build(out, s, now=T0), b.build(out, s, now=T0)
    assert a.signal_id != c.signal_id
    for f in ("entry", "stop_loss", "take_profit", "stop_distance", "reward_risk",
              "base_score", "bonus", "total_score", "expected_value",
              "combined_priority", "created_at", "expires_at"):
        assert getattr(a, f) == getattr(c, f), f


def test_breakdown_names_deferred_bonuses():
    c = build_one()
    assert c.score_breakdown.startswith("base50 ")
    for name in ("Squeeze", "Fib", "ORB"):
        assert name in c.score_breakdown

"""Orchestrator tests: wiring, outcome distinguishability, authority absence."""
from __future__ import annotations

import ast
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import pytest  # noqa: E402
import yaml  # noqa: E402

from speedtrader.app.orchestrator import DeterministicOrchestrator  # noqa: E402
from speedtrader.common.clock import Freshness  # noqa: E402
from speedtrader.common.ids import IdKind, new_id  # noqa: E402
from speedtrader.data.schemas import (  # noqa: E402
    Bar, DataSourceMeta, MarketRegime, MarketSnapshot, RejectionStage,
    SystemState, TechnicalFeatures,
)
from speedtrader.quant.cost_policy import EVCostNotConfigured  # noqa: E402
from speedtrader.quant.strategies.s07 import S07MomentumBreakout  # noqa: E402
from speedtrader.risk.engine import RiskEngineError  # noqa: E402
from speedtrader.risk.state import AccountState, OpenPosition, PortfolioState  # noqa: E402
from speedtrader.storage.decision_store import DecisionStore, StoreUnwritable  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
T0 = datetime(2026, 9, 2, 14, 0, tzinfo=timezone.utc)

# --- TEST FIXTURE ONLY ---
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


EXEC_CFG = {**_cost_block(), "signal_ttl_seconds": 30}
RISK_CFG = yaml.safe_load((ROOT / "configs" / "risk_config.yaml").read_text())

BUY_BAR = (111.0, 116.0, 110.0, 115.0)


def bars(n=25, last=BUY_BAR, last_vol=4000.0):
    b = [Bar(t=T0 - timedelta(hours=n - i), o=100., h=110., l=90., c=100., v=1000.)
         for i in range(n)]
    o, h, l, c = last
    b[-1] = Bar(t=b[-1].t, o=o, h=h, l=l, c=c, v=last_vol)
    return b


def snap(b=None, spread_pct=0.01, market_open=True):
    b = b if b is not None else bars()
    return MarketSnapshot(
        snapshot_id=new_id(IdKind.SNAPSHOT), symbol="TEST", price=b[-1].c,
        spread=0.02, spread_pct=spread_pct, bars=b,
        features=TechnicalFeatures(atr=2.0, ema200=100.0, di_plus=30.0, di_minus=10.0),
        regime=MarketRegime.UNKNOWN, market_open=market_open,
        source=DataSourceMeta(vendor="replay", fetched_at=T0, bars_available=len(b),
                              freshness=Freshness.FRESH, notes="SIMULATED DATA"))


def account():
    return AccountState(balance=100_000, equity=100_000,
                        day_start_equity=100_000, equity_high_water=100_000)


def orch(tmp_path, exec_cfg=None, risk_cfg=None):
    return DeterministicOrchestrator(
        strategies=[S07MomentumBreakout()],
        execution_config=exec_cfg or EXEC_CFG,
        risk_config=risk_cfg or RISK_CFG,
        store=DecisionStore(tmp_path))


# ============================================ happy path

def test_accepted_decision_is_stored(tmp_path):
    r = orch(tmp_path).run(snap(), account=account(), portfolio=PortfolioState())
    assert r.accepted
    assert r.decision.state is SystemState.RISK_CHECK
    assert r.decision.candidate is not None
    assert r.decision.risk_gate is not None
    assert r.decision.rejection_stage is None
    assert r.stored_at.exists()


def test_agent_fields_remain_none(tmp_path):
    """Step 4 has no AI stages. Their slots must stay empty, not stubbed."""
    d = orch(tmp_path).run(snap(), account=account(),
                           portfolio=PortfolioState()).decision
    for f in ("bull", "bear", "research", "trader_proposal", "risk_assessment",
              "portfolio_decision", "revalidation_gate", "execution_guard",
              "execution", "outcome"):
        assert getattr(d, f) is None, f
    assert d.analyst_reports == [] and d.selected_agents == []


def test_signal_and_snapshot_ids_linked(tmp_path):
    s = snap()
    d = orch(tmp_path).run(s, account=account(), portfolio=PortfolioState()).decision
    assert d.snapshot_id == s.snapshot_id
    assert d.signal_id == d.candidate.signal_id
    assert d.decision_id.startswith("dec_")


# ============================================ five distinguishable outcomes

def test_outcome_1_configuration_failure_writes_nothing(tmp_path):
    """Distinct from every other outcome: no DecisionLog exists at all."""
    with pytest.raises(EVCostNotConfigured):
        DeterministicOrchestrator(
            strategies=[S07MomentumBreakout()],
            execution_config={"signal_ttl_seconds": 30},
            risk_config=RISK_CFG, store=DecisionStore(tmp_path))
    assert list(tmp_path.glob("*.jsonl")) == []


def test_outcome_2_no_signal_is_rejected_by_quant(tmp_path):
    flat = bars(last=(100.0, 101.0, 99.0, 100.0))
    r = orch(tmp_path).run(snap(flat), account=account(), portfolio=PortfolioState())
    assert not r.accepted
    assert r.decision.state is SystemState.REJECTED
    assert r.decision.rejection_stage is RejectionStage.QUANT
    assert r.decision.candidate is None
    assert r.stored_at.exists()


def test_outcome_4_risk_rejection_names_the_layer(tmp_path):
    positions = [OpenPosition(symbol=s, side="BUY", quantity=1000,
                              entry_price=100.0, stop_loss=98.0)
                 for s in ("A", "B", "C", "D")]     # 8% heat
    r = orch(tmp_path).run(snap(), account=account(),
                           portfolio=PortfolioState(positions=positions))
    assert not r.accepted
    assert r.decision.rejection_stage is RejectionStage.RISK_ENGINE
    assert "heat" in r.decision.rejection_reason
    assert r.decision.risk_gate is not None


def test_outcome_5_infrastructure_failure_propagates(tmp_path):
    """A decision that cannot be recorded must not be reported as complete."""
    o = orch(tmp_path)
    import shutil
    shutil.rmtree(tmp_path)
    tmp_path.write_text("now a file")
    with pytest.raises(StoreUnwritable):
        o.run(snap(), account=account(), portfolio=PortfolioState())


def test_quant_and_risk_rejections_are_distinguishable(tmp_path):
    o = orch(tmp_path)
    a = o.run(snap(bars(last=(100.0, 101.0, 99.0, 100.0))),
              account=account(), portfolio=PortfolioState()).decision
    heavy = PortfolioState(positions=[
        OpenPosition(symbol=s, side="BUY", quantity=1000, entry_price=100.0,
                     stop_loss=98.0) for s in ("A", "B", "C", "D")])
    b = o.run(snap(), account=account(), portfolio=heavy).decision
    assert a.rejection_stage is not b.rejection_stage


def test_risk_engine_failure_is_never_an_approval(tmp_path):
    o = orch(tmp_path)
    class Exploding:
        def evaluate(self, **kw): raise RuntimeError("engine down")
    o.risk = Exploding()
    r = o.run(snap(), account=account(), portfolio=PortfolioState())
    assert not r.accepted
    assert r.decision.state is SystemState.FAILED
    assert "risk engine failed" in r.decision.rejection_reason


def test_market_closed_is_rejected_not_ignored(tmp_path):
    r = orch(tmp_path).run(snap(market_open=False), account=account(),
                           portfolio=PortfolioState())
    assert not r.accepted
    assert r.decision.rejection_stage is RejectionStage.RISK_ENGINE


# ============================================ construction fails closed

def test_fail_open_risk_config_refused(tmp_path):
    with pytest.raises(RiskEngineError):
        DeterministicOrchestrator(
            strategies=[S07MomentumBreakout()], execution_config=EXEC_CFG,
            risk_config={**RISK_CFG, "fail_closed": False},
            store=DecisionStore(tmp_path))


# ============================================ thin + no authority

def test_orchestrator_contains_no_formulas_or_execution():
    src = (ROOT / "src" / "speedtrader" / "app" / "orchestrator.py").read_text()
    for token in ("1.5 *", "3.0 *", "portfolio_heat_pct", "size_position",
                  "submit_order", "ExecutionAuthorization", "api_key",
                  "MarketOrderRequest", "kelly"):
        assert token not in src, f"orchestrator contains {token}"


def test_orchestrator_imports_no_llm_or_broker_trading():
    src = (ROOT / "src" / "speedtrader" / "app" / "orchestrator.py").read_text()
    names = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
    for n in names:
        parts = set(n.split("."))
        assert "llm" not in parts, n
        assert "alpaca" not in parts, n
        assert "execution" not in parts, n


def test_account_and_portfolio_have_no_defaults():
    """Defaulting would fabricate the state the heat/exposure checks consume."""
    import inspect
    sig = inspect.signature(DeterministicOrchestrator.run)
    for p in ("account", "portfolio"):
        assert sig.parameters[p].default is inspect.Parameter.empty


def test_orchestrator_never_accepts_a_decisionlog_as_input():
    """Structural bar on replaying a stored decision through the pipeline."""
    import inspect
    from speedtrader.data.schemas import DecisionLog
    sig = inspect.signature(DeterministicOrchestrator.run)
    assert not any(p.annotation is DecisionLog for p in sig.parameters.values())


def test_reproducible_across_runs(tmp_path):
    o = orch(tmp_path)
    s = snap()
    a = o.run(s, account=account(), portfolio=PortfolioState(), now=T0).decision
    b = o.run(s, account=account(), portfolio=PortfolioState(), now=T0).decision
    assert a.decision_id != b.decision_id
    assert a.candidate.total_score == b.candidate.total_score
    assert a.candidate.expected_value == b.candidate.expected_value
    assert a.risk_gate.verdict == b.risk_gate.verdict
    assert a.risk_gate.approved_quantity == b.risk_gate.approved_quantity

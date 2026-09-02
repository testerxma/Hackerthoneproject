"""
End-to-end deterministic pipeline: fixture bars -> snapshot -> candidate ->
risk gate -> persisted DecisionLog. No network, no LLM, no broker.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import pytest  # noqa: E402
import yaml  # noqa: E402

from speedtrader.alpaca.market_data import FixtureMarketData  # noqa: E402
from speedtrader.app.orchestrator import DeterministicOrchestrator  # noqa: E402
from speedtrader.data.schemas import (  # noqa: E402
    Bar, DecisionLog, RejectionStage, RiskGateVerdict, SystemState,
)
from speedtrader.data.snapshot import SnapshotBuilder  # noqa: E402
from speedtrader.quant.cost_policy import EVCostNotConfigured  # noqa: E402
from speedtrader.quant.features import FeatureEngine  # noqa: E402
from speedtrader.quant.strategies.s07 import S07MomentumBreakout  # noqa: E402
from speedtrader.risk.state import AccountState, OpenPosition, PortfolioState  # noqa: E402
from speedtrader.storage.decision_store import DecisionStore  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 9, 2, 14, 0, tzinfo=timezone.utc)

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


EXEC_CFG = {**_cost_block(), "signal_ttl_seconds": 30}
RISK_CFG = yaml.safe_load((ROOT / "configs" / "risk_config.yaml").read_text())

N = FeatureEngine().recommended_bars()   # 891


def series(breakout: bool, vol_spike: bool = True) -> list[Bar]:
    """891 flat bars in a tight range, then one bar that does or does not break out."""
    bars = []
    for i in range(N - 1):
        bars.append(Bar(t=NOW - timedelta(hours=N - i), o=100.0, h=100.6,
                        l=99.4, c=100.0, v=1000.0))
    if breakout:
        last = Bar(t=NOW, o=101.0, h=106.0, l=100.9, c=105.5,
                   v=4000.0 if vol_spike else 1000.0)
    else:
        last = Bar(t=NOW, o=100.0, h=100.2, l=99.8, c=100.0, v=1000.0)
    bars.append(last)
    return bars


def provider(bars): return FixtureMarketData(bars={("TEST", "1Hour"): bars},
                                             quotes={"TEST": {"bid": 105.48,
                                                              "ask": 105.52,
                                                              "timestamp": NOW}})


def account(balance=100_000.0):
    return AccountState(balance=balance, equity=balance,
                        day_start_equity=balance, equity_high_water=balance)


def build(tmp_path, exec_cfg=None):
    return DeterministicOrchestrator(
        strategies=[S07MomentumBreakout()],
        execution_config=exec_cfg or EXEC_CFG,
        risk_config=RISK_CFG, store=DecisionStore(tmp_path))


def snapshot_of(bars):
    r = SnapshotBuilder(provider(bars)).build("TEST", now=NOW)
    assert r.ok, r.reason
    return r.snapshot


# ============================================ full path

def test_breakout_produces_a_persisted_decision(tmp_path):
    o = build(tmp_path)
    r = o.run(snapshot_of(series(True)), account=account(),
              portfolio=PortfolioState(), now=NOW)
    assert r.decision.candidate is not None
    assert r.decision.candidate.strategy_id == "S07"
    assert r.decision.risk_gate is not None
    assert r.stored_at.exists()
    assert DecisionStore(tmp_path).count(NOW) == 1


def test_accepted_decision_reaches_pass_or_reduce(tmp_path):
    r = build(tmp_path).run(snapshot_of(series(True)), account=account(),
                            portfolio=PortfolioState(), now=NOW)
    assert r.accepted
    assert r.decision.risk_gate.verdict in (RiskGateVerdict.PASS,
                                            RiskGateVerdict.REDUCE)
    assert r.decision.risk_gate.approved_quantity > 0


def test_no_breakout_is_recorded_not_discarded(tmp_path):
    """Section 75: rejected opportunities are evidence too."""
    r = build(tmp_path).run(snapshot_of(series(False)), account=account(),
                            portfolio=PortfolioState(), now=NOW)
    assert not r.accepted
    assert r.decision.rejection_stage is RejectionStage.QUANT
    assert DecisionStore(tmp_path).count(NOW) == 1


def test_heat_rejection_recorded_with_the_blocking_rule(tmp_path):
    heavy = PortfolioState(positions=[
        OpenPosition(symbol=s, side="BUY", quantity=1000, entry_price=100.0,
                     stop_loss=98.0) for s in ("A", "B", "C", "D")])
    r = build(tmp_path).run(snapshot_of(series(True)), account=account(),
                            portfolio=heavy, now=NOW)
    assert r.decision.rejection_stage is RejectionStage.RISK_ENGINE
    assert "heat" in r.decision.rejection_reason
    failed = [c.rule for c in r.decision.risk_gate.failed_checks]
    assert "portfolio_heat" in failed


# ============================================ trace reconstructible from disk

def test_full_trace_reconstructible_from_disk_alone(tmp_path):
    o = build(tmp_path)
    r = o.run(snapshot_of(series(True)), account=account(),
              portfolio=PortfolioState(), now=NOW)
    fresh = DecisionStore(tmp_path).read(NOW)[0]     # new instance, disk only
    assert fresh.decision_id == r.decision.decision_id
    assert fresh.snapshot_id == r.decision.snapshot_id
    assert fresh.candidate.entry == r.decision.candidate.entry
    assert fresh.candidate.total_score == r.decision.candidate.total_score
    assert fresh.risk_gate.verdict == r.decision.risk_gate.verdict
    assert len(fresh.risk_gate.checks) == len(r.decision.risk_gate.checks)
    assert fresh.snapshot.source.vendor == "replay"


def test_cost_provenance_persisted_on_disk(tmp_path):
    """Any stored decision records which fee schedule priced it."""
    o = build(tmp_path)
    o.run(snapshot_of(series(True)), account=account(),
          portfolio=PortfolioState(), now=NOW)
    line = json.loads((tmp_path / f"decisions-{NOW.date().isoformat()}.jsonl")
                      .read_text().splitlines()[0])
    assert line["candidate"]["expected_value"] is not None
    assert line["snapshot"]["source"]["vendor"] == "replay"
    assert "SIMULATED" in (line["snapshot"]["source"]["notes"] or "")


def test_simulated_data_never_labelled_alpaca(tmp_path):
    o = build(tmp_path)
    r = o.run(snapshot_of(series(True)), account=account(),
              portfolio=PortfolioState(), now=NOW)
    assert r.decision.snapshot.source.vendor == "replay"


# ============================================ reproducibility

def test_two_runs_identical_except_ids_and_timestamps(tmp_path):
    o = build(tmp_path)
    s = snapshot_of(series(True))
    a = o.run(s, account=account(), portfolio=PortfolioState(), now=NOW).decision
    b = o.run(s, account=account(), portfolio=PortfolioState(), now=NOW).decision
    assert a.decision_id != b.decision_id
    assert a.candidate.entry == b.candidate.entry
    assert a.candidate.total_score == b.candidate.total_score
    assert a.candidate.expected_value == b.candidate.expected_value
    assert a.candidate.combined_priority == b.candidate.combined_priority
    assert a.risk_gate.verdict == b.risk_gate.verdict
    assert a.risk_gate.approved_quantity == b.risk_gate.approved_quantity
    assert a.risk_gate.size_multiplier == b.risk_gate.size_multiplier


def test_both_decisions_persisted_separately(tmp_path):
    o = build(tmp_path)
    s = snapshot_of(series(True))
    o.run(s, account=account(), portfolio=PortfolioState(), now=NOW)
    o.run(s, account=account(), portfolio=PortfolioState(), now=NOW)
    assert DecisionStore(tmp_path).count(NOW) == 2


# ============================================ fail closed, end to end

def test_shipped_config_now_builds_the_pipeline(tmp_path):
    """Commission was resolved 2026-09-02, so the production config runs."""
    prod = yaml.safe_load((ROOT / "configs" / "execution_config.yaml").read_text())
    build(tmp_path, exec_cfg=prod)          # must not raise


def test_shipped_config_recloses_when_the_commission_decision_is_removed(tmp_path):
    """The fail-closed gate is satisfied, not removed. Deleting the recorded
    decision must stop the pipeline again and write nothing."""
    prod = yaml.safe_load((ROOT / "configs" / "execution_config.yaml").read_text())
    del prod["transaction_cost"]["default"]["commission"]
    with pytest.raises(EVCostNotConfigured):
        build(tmp_path, exec_cfg=prod)
    assert list(tmp_path.glob("*.jsonl")) == []


@pytest.mark.parametrize("block,key", [
    ("commission", "per_share"), ("commission", "rate_of_notional"),
    ("commission", "source"),
    ("regulatory", "sec_rate_of_notional"), ("regulatory", "taf_per_share"),
    ("regulatory", "cat_per_share"), ("regulatory", "sides_per_round_trip"),
    ("slippage", "per_share"), ("slippage", "source"),
])
def test_partial_cost_config_produces_zero_decisions(tmp_path, block, key):
    """Every individual component, missing, blocks the whole pipeline."""
    import copy
    cfg = copy.deepcopy(EXEC_CFG)
    del cfg["transaction_cost"]["default"][block][key]
    with pytest.raises(EVCostNotConfigured):
        build(tmp_path, exec_cfg=cfg)
    assert list(tmp_path.glob("*.jsonl")) == []


def test_production_config_carries_provenance_for_every_cost_component(tmp_path):
    """Every rate reaching EV must state where it came from."""
    prod = yaml.safe_load((ROOT / "configs" / "execution_config.yaml").read_text())
    default = prod["transaction_cost"]["default"]
    assert default["regulatory"]["source"] == "authoritative"
    # Commission and slippage are attestations about THIS account and THIS
    # execution history; no published document can supply either.
    assert default["commission"]["source"] == "operator_assumption"
    assert default["slippage"]["source"] == "operator_assumption"
    for name in ("commission", "slippage"):
        assert default[name]["assumption"].strip()
    build(tmp_path, exec_cfg=prod)


def test_stale_bars_produce_no_snapshot_and_no_decision(tmp_path):
    old = [Bar(t=b.t - timedelta(days=5), o=b.o, h=b.h, l=b.l, c=b.c, v=b.v)
           for b in series(True)]
    r = SnapshotBuilder(provider(old)).build("TEST", now=NOW)
    assert not r.ok and r.code == "stale_data"
    assert list(tmp_path.glob("*.jsonl")) == []


def test_insufficient_history_produces_no_snapshot(tmp_path):
    r = SnapshotBuilder(provider(series(True)[-300:])).build("TEST", now=NOW)
    assert not r.ok
    assert r.code in ("insufficient_history", "ema_not_converged")


# ============================================ CLI

def test_cli_fails_closed_when_market_data_is_unavailable(tmp_path):
    """With the cost policy resolved, the next fail-closed gate is data. An
    empty fixture directory must produce no snapshot, no decision and a
    non-zero exit — never a decision built on absent data."""
    p = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_deterministic.py"),
         "--fixture", str(tmp_path), "--symbol", "TEST", "--balance", "100000"],
        capture_output=True, text=True, cwd=str(ROOT))
    assert p.returncode != 0
    assert "NO SNAPSHOT" in p.stdout
    assert "data_unavailable" in p.stdout
    assert list(tmp_path.glob("*.jsonl")) == []


def test_cli_fails_closed_when_the_cost_policy_is_incomplete(tmp_path, monkeypatch):
    """The CLI's own cost fail-closed path, exercised against a broken config
    rather than relying on the shipped one being unresolved."""
    broken = yaml.safe_load((ROOT / "configs" / "execution_config.yaml").read_text())
    del broken["transaction_cost"]["default"]["commission"]
    cfg_dir = tmp_path / "configs"; cfg_dir.mkdir()
    for src in (ROOT / "configs").glob("*.yaml"):
        (cfg_dir / src.name).write_text(src.read_text())
    (cfg_dir / "execution_config.yaml").write_text(yaml.safe_dump(broken))
    env = {**os.environ, "SPEEDTRADER_CONFIG_DIR": str(cfg_dir)}
    p = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_deterministic.py"),
         "--fixture", str(tmp_path), "--symbol", "TEST", "--balance", "100000"],
        capture_output=True, text=True, cwd=str(ROOT), env=env)
    assert p.returncode == 2
    assert "FAIL CLOSED" in p.stderr
    assert "commission" in p.stderr


def test_cli_requires_balance_rather_than_defaulting(tmp_path):
    p = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_deterministic.py"),
         "--fixture", str(tmp_path), "--symbol", "TEST"],
        capture_output=True, text=True, cwd=str(ROOT))
    assert p.returncode != 0
    assert "balance" in (p.stderr + p.stdout)


def test_cli_submits_no_orders():
    src = (ROOT / "scripts" / "run_deterministic.py").read_text()
    for token in ("submit_order", "TradingClient", "MarketOrderRequest",
                  "api.alpaca.markets"):
        assert token not in src

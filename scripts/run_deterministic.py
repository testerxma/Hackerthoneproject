#!/usr/bin/env python3
"""
SpeedTrader AI — Deterministic pipeline runner.

    MarketSnapshot -> QuantCore -> CandidateSignal -> RiskEngine -> DecisionLog

No LLM. No agents. No broker order is ever submitted: this script imports nothing
that can reach a trading endpoint.

    python scripts/run_deterministic.py --fixture tests/fixtures/bars --symbol TEST

With the shipped configuration this exits non-zero with EVCostNotConfigured.
That is intentional: no transaction-cost policy is configured, so no decision may
be produced. See configs/execution_config.yaml.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from speedtrader.alpaca.market_data import FixtureMarketData  # noqa: E402
from speedtrader.app.orchestrator import DeterministicOrchestrator  # noqa: E402
from speedtrader.config import get_config  # noqa: E402
from speedtrader.data.snapshot import SnapshotBuilder  # noqa: E402
from speedtrader.quant.cost_policy import CostPolicyError  # noqa: E402
from speedtrader.quant.strategies.s07 import S07MomentumBreakout  # noqa: E402
from speedtrader.risk.state import AccountState, PortfolioState  # noqa: E402
from speedtrader.storage.decision_store import DecisionStore  # noqa: E402

C = {"g": "\033[92m", "r": "\033[91m", "y": "\033[93m", "d": "\033[2m", "0": "\033[0m"}


def main() -> int:
    ap = argparse.ArgumentParser(description="Run one deterministic decision cycle.")
    ap.add_argument("--fixture", required=True,
                    help="directory of <SYMBOL>_<timeframe>.json bar files")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--balance", type=float, required=True,
                    help="account balance; required, never defaulted — it drives "
                         "position sizing and the heat calculation")
    ap.add_argument("--store", default="data/decisions")
    args = ap.parse_args()

    cfg = get_config()
    try:
        orch = DeterministicOrchestrator(
            strategies=[S07MomentumBreakout()],
            execution_config=cfg.execution,
            risk_config=cfg.risk,
            store=DecisionStore(args.store),
        )
    except CostPolicyError as e:
        print(f"{C['r']}FAIL CLOSED{C['0']} — {e}", file=sys.stderr)
        return 2

    provider = FixtureMarketData.from_directory(args.fixture)
    built = SnapshotBuilder(provider).build(args.symbol)
    if not built.ok:
        print(f"{C['y']}NO SNAPSHOT{C['0']} — [{built.code}] {built.reason}")
        return 1

    snapshot = built.snapshot
    if snapshot.source.vendor != "alpaca":
        print(f"{C['y']}SIMULATED DATA — not live Alpaca{C['0']}")

    # Synthetic, explicitly labelled. Real account state arrives with the Alpaca
    # account module; nothing here pretends this is a real portfolio.
    account = AccountState(balance=args.balance, equity=args.balance,
                           day_start_equity=args.balance,
                           equity_high_water=args.balance)

    result = orch.run(snapshot, account=account, portfolio=PortfolioState())
    d = result.decision

    print(f"\n  decision   {d.decision_id}")
    print(f"  snapshot   {d.snapshot_id}")
    print(f"  state      {d.state.value}")
    if d.candidate:
        c = d.candidate
        print(f"  signal     {c.strategy_id} {c.direction.value} @ {c.entry}"
              f"  sl {c.stop_loss}  tp {c.take_profit}")
        print(f"  score      {c.total_score}  ({c.score_breakdown})")
        print(f"  EV         {c.expected_value:+.4f} R"
              f"{'  [bootstrap]' if c.ev_is_bootstrap else ''}")
    if d.risk_gate:
        g = d.risk_gate
        col = C["g"] if result.accepted else C["r"]
        print(f"  risk gate  {col}{g.verdict.value}{C['0']}"
              f"  heat {g.portfolio_heat_pct:.2f}%")
        if g.approved_quantity:
            print(f"  quantity   {int(g.approved_quantity)} shares (x{g.size_multiplier})")
        for chk in g.failed_checks:
            print(f"    {C['r']}x{C['0']} {chk.rule}: observed={chk.observed} "
                  f"limit={chk.limit}")
    if d.rejection_stage:
        print(f"  {C['r']}{d.rejection_stage.value}{C['0']} — {d.rejection_reason}")
    print(f"\n  {C['d']}recorded in {result.stored_at}{C['0']}")
    print(f"  {C['d']}no order was submitted; Step 4 has no execution path{C['0']}\n")
    return 0 if result.accepted else 1


if __name__ == "__main__":
    sys.exit(main())

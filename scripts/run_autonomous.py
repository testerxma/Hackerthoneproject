#!/usr/bin/env python3
"""
SpeedTrader AI — bounded autonomous runtime

Runs the full decision pipeline unattended, on a schedule, against the Alpaca
PAPER account, until a configured bound stops it.

    market data -> data quality -> canonical snapshot -> strategy scan
    -> candidates -> AI review/veto -> options validation -> deterministic risk
    -> portfolio gate -> authorization -> execution -> reconciliation -> journal

    # inspect what it would do, contacting no broker
    python scripts/run_autonomous.py --dry-run --max-cycles 1

    # trade, hard-bounded
    python scripts/run_autonomous.py --live --max-cycles 20 --max-orders 5 \
        --interval 300 --until 2026-09-04T15:00:00Z

Stopping it:
    touch data/STOP            # kill switch; stops after the current cycle
    Ctrl-C / SIGTERM           # graceful; also stops after the current cycle

Neither interrupts mid-order. The window between submitting and recording is
exactly what the write-ahead journal exists to protect, so there is no reason
to open it deliberately.

CREDENTIALS come from the environment or a local .env and are never printed,
logged, or included in an exception.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402

from speedtrader.agents.veto import AdversarialReviewer  # noqa: E402
from speedtrader.alpaca.client import (  # noqa: E402
    AlpacaClient, AlpacaConfigError, load_credentials,
)
from speedtrader.alpaca.market_data import AlpacaMarketData  # noqa: E402
from speedtrader.alpaca.options_data import (  # noqa: E402
    AlpacaOptionsData, ChainRequest,
)
from speedtrader.app.options_orchestrator import OptionsOrchestrator  # noqa: E402
from speedtrader.app.runtime import (  # noqa: E402
    AutonomousRuntime, RuntimeLimits, RuntimeState,
)
from speedtrader.data.snapshot import SnapshotBuilder  # noqa: E402
from speedtrader.execution.authorization import AuthorizationRegistry  # noqa: E402
from speedtrader.execution.intent_journal import IntentJournal  # noqa: E402
from speedtrader.execution.mcp_broker import AlpacaMCPBroker, MCPUnavailable  # noqa: E402
from speedtrader.execution.options_adapter import OptionsExecutionAdapter  # noqa: E402
from speedtrader.llm.providers.deterministic import DeterministicProvider  # noqa: E402
from speedtrader.quant.strategies.s07 import S07MomentumBreakout  # noqa: E402
from speedtrader.risk.state import AccountState, PortfolioState  # noqa: E402
from speedtrader.storage.decision_store import DecisionStore  # noqa: E402

C = {"b": "\033[1m", "d": "\033[2m", "g": "\033[92m", "r": "\033[91m",
     "y": "\033[93m", "c": "\033[96m", "0": "\033[0m"}

DEFAULT_WATCHLIST = ["SPY", "QQQ", "F", "SOFI", "PLTR", "INTC", "T"]


def say(msg: str = "") -> None:
    print(msg, flush=True)


def rule(title: str = "") -> None:
    say(f"{C['b']}{'─' * 74}{C['0']}")
    if title:
        say(f"{C['b']}  {title}{C['0']}")
        say(f"{C['b']}{'─' * 74}{C['0']}")


def load_dotenv(path: Path) -> None:
    """Read a local .env without overriding the real environment. Never echoed."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


class OrderLookup:
    """Read-only broker view. Reconciliation gets this and nothing else, so the
    component that decides what is TRUE cannot also change it."""

    def __init__(self, trading):
        self._trading = trading

    def get_order_by_client_id(self, client_order_id: str):
        try:
            order = self._trading.get_order_by_client_id(client_order_id)
        except Exception as e:
            if "404" in str(e) or "not found" in str(e).lower():
                return None
            raise
        if order is None:
            return None
        return {
            "id": str(order.id), "client_order_id": order.client_order_id,
            "symbol": order.symbol,
            "status": str(getattr(order.status, "value", order.status)),
            "qty": order.qty, "filled_qty": order.filled_qty,
            "filled_avg_price": order.filled_avg_price,
        }


def build_account(trading) -> tuple[AccountState, float]:
    acct = trading.get_account()
    equity = float(acct.equity)
    return AccountState(
        balance=float(acct.cash) if acct.cash else equity,
        equity=equity,
        # Without stored history the only honest choice is today's equity: an
        # invented high-water mark would silently loosen the drawdown gate.
        day_start_equity=equity, equity_high_water=equity,
    ), equity


def open_option_premium(trading) -> float:
    total = 0.0
    for pos in trading.get_all_positions():
        if str(getattr(pos, "asset_class", "")).endswith("option"):
            total += abs(float(pos.cost_basis or 0.0))
    return total


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", nargs="+", default=DEFAULT_WATCHLIST)
    ap.add_argument("--interval", type=float, default=300.0,
                    help="seconds between cycles (minimum 1)")
    ap.add_argument("--max-cycles", type=int, default=12)
    ap.add_argument("--max-orders", type=int, default=5,
                    help="hard cap on orders that reach the broker")
    ap.add_argument("--until", help="ISO-8601 UTC stop time, e.g. 2026-09-04T15:00:00Z")
    ap.add_argument("--live", action="store_true",
                    help="ACTUALLY place orders on the paper account")
    ap.add_argument("--dry-run", action="store_true",
                    help="run the pipeline, hold no broker reference (default)")
    ap.add_argument("--ignore-market-hours", action="store_true",
                    help="run cycles out of hours for inspection; refused with --live")
    ap.add_argument("--store", default=str(ROOT / "data" / "decisions"))
    ap.add_argument("--journal", default=str(ROOT / "data"))
    ap.add_argument("--kill-switch", default=str(ROOT / "data" / "STOP"))
    args = ap.parse_args()

    if args.ignore_market_hours and args.live:
        ap.error("--ignore-market-hours cannot be combined with --live: an "
                 "option order priced outside regular hours is exactly the "
                 "stale-quote failure the gate prevents")

    load_dotenv(ROOT / ".env")
    exec_cfg = yaml.safe_load((ROOT / "configs" / "execution_config.yaml").read_text())
    risk_cfg = yaml.safe_load((ROOT / "configs" / "risk_config.yaml").read_text())

    try:
        creds = load_credentials(exec_cfg)
    except AlpacaConfigError as e:
        say(f"{C['r']}{e}{C['0']}")
        return 2
    if not creds.paper:
        say(f"{C['r']}refusing to run against a live account{C['0']}")
        return 2

    client = AlpacaClient(creds)
    trading = client.trading
    market = AlpacaMarketData(client)
    options = AlpacaOptionsData(trading, client.option_data)
    journal = IntentJournal(args.journal)
    store = DecisionStore(Path(args.store))

    adapter = None
    if args.live:
        try:
            adapter = OptionsExecutionAdapter(
                AlpacaMCPBroker(), AuthorizationRegistry(), journal=journal)
        except MCPUnavailable as e:
            say(f"{C['r']}MCP unavailable{C['0']}: {e}")
            return 2

    orch = OptionsOrchestrator(
        strategies=[S07MomentumBreakout()],
        execution_config=exec_cfg, risk_config=risk_cfg, store=store,
        chain_provider=lambda snap, asof: options.fetch_chain(
            ChainRequest(snap.symbol), spot=snap.price, asof=asof),
        adapter=adapter,
        reviewer=AdversarialReviewer(DeterministicProvider(), run_debate=False),
    )

    builder = SnapshotBuilder(market)

    def cycle_fn(symbol: str, cycle_id: str):
        """One symbol, end to end. Raises nothing the runtime cannot isolate."""
        snap = builder.build(symbol)
        if not snap.ok:
            say(f"    {symbol:<6} {C['y']}no snapshot{C['0']}  {snap.reason}")
            return None
        account, _ = build_account(trading)
        result = orch.run(
            snap.snapshot, account=account, portfolio=PortfolioState(),
            open_premium=open_option_premium(trading), dry_run=not args.live)
        verdict = (f"{C['g']}ACCEPTED{C['0']}" if result.accepted
                   else f"{C['r']}no trade{C['0']}")
        say(f"    {symbol:<6} {verdict}  {C['d']}{result.reason[:70]}{C['0']}")
        return result

    limits = RuntimeLimits(
        interval_seconds=max(1.0, args.interval),
        max_cycles=args.max_cycles,
        max_orders=args.max_orders,
        until=(datetime.fromisoformat(args.until.replace("Z", "+00:00"))
               if args.until else None),
        kill_switch_path=Path(args.kill_switch),
        require_market_open=not args.ignore_market_hours,
    )

    def on_cycle(result) -> None:
        say(f"  {C['d']}cycle {result.index} [{result.cycle_id}] "
            f"{len(result.decisions)} decision(s), "
            f"{result.orders_submitted} order(s)"
            f"{', ' + str(len(result.errors)) + ' error(s)' if result.errors else ''}"
            f"{C['0']}")

    runtime = AutonomousRuntime(
        symbols=[s.upper() for s in args.symbols], cycle_fn=cycle_fn,
        limits=limits, journal=journal, lookup=OrderLookup(trading),
        market_open_fn=client.is_market_open, on_cycle=on_cycle,
    )

    rule("SPEEDTRADER AI — AUTONOMOUS RUNTIME")
    say(f"  account        {creds.redacted()}   {C['b']}PAPER{C['0']}")
    say(f"  mode           {C['b']}"
        f"{'LIVE — real paper orders will be placed' if args.live else 'DRY RUN — no broker reference held'}"
        f"{C['0']}")
    say(f"  symbols        {', '.join(runtime.symbols)}")
    say(f"  bounds         every {limits.interval_seconds:.0f}s · "
        f"max {limits.max_cycles} cycles · max {limits.max_orders} orders"
        + (f" · until {limits.until.isoformat()}" if limits.until else ""))
    say(f"  kill switch    touch {limits.kill_switch_path}")
    say()

    health = runtime.run()

    say()
    rule("RUNTIME SUMMARY")
    colour = C['r'] if health.state is RuntimeState.HALTED else C['g']
    say(f"  state          {colour}{health.state.value}{C['0']}")
    say(f"  stopped by     {health.stop_reason.value or '—'}")
    say(f"  cycles         {health.cycles_completed}")
    say(f"  orders sent    {health.orders_submitted}")
    if health.detail:
        say(f"  detail         {health.detail}")
    say(f"  {C['d']}decisions appended to {args.store}{C['0']}")
    say(f"  {C['d']}python scripts/build_dashboard.py   renders the command centre{C['0']}")
    return 1 if health.state is RuntimeState.HALTED else 0


if __name__ == "__main__":
    sys.exit(main())

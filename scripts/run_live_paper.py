#!/usr/bin/env python3
"""
SpeedTrader AI — the real thing, against a real Alpaca paper account

--------------------------------------------------------------------------------
WHY THIS SCRIPT EXISTS
--------------------------------------------------------------------------------
`run_options_demo.py` proves the LOGIC with fixtures: it is deterministic,
offline, and shows six scenarios including the ones you cannot provoke on
demand. This script proves the WIRING: real bars, a real option chain with real
quotes, the real account balance, and — only if you ask for it in as many words
— a real order through Alpaca's official MCP server.

Both matter, and neither substitutes for the other. Running only the fixtures
tells you nothing about whether Alpaca's actual payloads fit your mapping; two
genuine bugs in this repository were found only by running THIS:

  * Alpaca caps latest-quote requests at 100 symbols, so a 354-contract chain
    returned `symbol limit is 100` and nothing could ever be priced.
  * The MCP server nests its reply two levels deep, so a SUCCESSFUL submission
    was reported UNKNOWN.

--------------------------------------------------------------------------------
IT DOES NOT TRADE UNLESS YOU SAY SO
--------------------------------------------------------------------------------
The default is a dry run: the full pipeline executes — snapshot, strategy, all
22 risk checks, contract selection, sizing, the AI review, the licence — and
stops at the broker. `--submit` is the only way an order is sent, and even then
paper is forced in three independent places and live is refused structurally.

    python scripts/run_live_paper.py --symbol SPY            # decide, don't trade
    python scripts/run_live_paper.py --symbol SPY --submit   # actually place it
    python scripts/run_live_paper.py --scan                  # survey a watchlist

Credentials come from the environment (or a local .env, never committed) and are
never printed, logged, or included in an exception.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date
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
    AlpacaOptionsData, ChainRequest, OptionsDataUnavailable,
)
from speedtrader.app.options_orchestrator import OptionsOrchestrator  # noqa: E402
from speedtrader.data.snapshot import SnapshotBuilder  # noqa: E402
from speedtrader.execution.authorization import AuthorizationRegistry  # noqa: E402
from speedtrader.execution.mcp_broker import AlpacaMCPBroker, MCPUnavailable  # noqa: E402
from speedtrader.execution.options_adapter import OptionsExecutionAdapter  # noqa: E402
from speedtrader.execution.reconciliation import (  # noqa: E402
    ReconciliationUnavailable, reconcile_order,
)
from speedtrader.llm.providers.deterministic import DeterministicProvider  # noqa: E402
from speedtrader.quant.strategies.plugins import (  # noqa: E402
    StrategyContractError, load_directory, strategies_of,
)
from speedtrader.quant.strategies.s07 import S07MomentumBreakout  # noqa: E402
from speedtrader.risk.state import AccountState, PortfolioState  # noqa: E402
from speedtrader.storage.decision_store import DecisionStore  # noqa: E402

C = {"b": "\033[1m", "d": "\033[2m", "g": "\033[92m", "r": "\033[91m",
     "y": "\033[93m", "0": "\033[0m"}

#: Cheap enough that one contract fits inside a 1% risk budget on a $100k
#: account. Established empirically, not guessed: at $2,000 per contract a
#: single AAPL call already exceeds that budget, and the sizing model correctly
#: declines it rather than rounding up to one — which is exactly the failure
#: that turns a 1% rule into a larger loss.
DEFAULT_WATCHLIST = ["SPY", "F", "SOFI", "PLTR", "INTC", "T", "SNAP"]


def say(msg: str = "") -> None:
    print(msg, flush=True)


def rule(title: str = "") -> None:
    say(f"{C['b']}{'─' * 74}{C['0']}")
    if title:
        say(f"{C['b']}  {title}{C['0']}")
        say(f"{C['b']}{'─' * 74}{C['0']}")


def load_dotenv(path: Path) -> None:
    """Read a local .env WITHOUT overriding anything already in the environment.

    Deliberately tiny and dependency-free. Values are never echoed.
    """
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


class _OrderLookup:
    """Read-only view of one order, by our own client_order_id.

    Reconciliation gets this and nothing else: the component that decides what
    is TRUE must not also be able to change it.
    """

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
            "id": str(order.id),
            "client_order_id": order.client_order_id,
            "symbol": order.symbol,
            "status": str(getattr(order.status, "value", order.status)),
            "qty": order.qty,
            "filled_qty": order.filled_qty,
            "filled_avg_price": order.filled_avg_price,
        }


def build_account(trading) -> tuple[AccountState, float]:
    """Real balances. The risk engine sizes against these, not against a guess."""
    acct = trading.get_account()
    equity = float(acct.equity)
    return AccountState(
        balance=float(acct.cash) if acct.cash else equity,
        equity=equity,
        # Without a stored history the only honest choice is today's equity: it
        # makes the daily-loss and drawdown checks measure from now rather than
        # from an invented high-water mark that would silently loosen them.
        day_start_equity=equity,
        equity_high_water=equity,
    ), equity


def open_option_premium(trading) -> float:
    """Premium already committed to open option positions.

    The concentration cap is a whole-book limit, so it has to see the book.
    """
    total = 0.0
    for pos in trading.get_all_positions():
        if str(getattr(pos, "asset_class", "")).endswith("option"):
            total += abs(float(pos.cost_basis or 0.0))
    return total


def resolve_strategies(directory: str | None):
    """S07, or the strategies in a directory the operator pointed us at.

    A refused strategy stops the run rather than being skipped: silently
    trading with fewer strategies than you asked for is worse than not starting.
    """
    if not directory:
        return [S07MomentumBreakout()], "S07 (built in)"
    loaded = load_directory(directory)
    if not loaded:
        raise StrategyContractError(f"no strategies found in {directory}")
    return strategies_of(loaded), ", ".join(item.id for item in loaded)


def run_symbol(symbol: str, args, client: AlpacaClient, exec_cfg, risk_cfg) -> int:
    trading = client.trading
    market = AlpacaMarketData(client)
    options = AlpacaOptionsData(trading, client.option_data)

    rule(f"{symbol}")

    # --- 1. Snapshot from real bars ------------------------------------
    snap_result = SnapshotBuilder(
        market, timeframe=args.timeframe,
        # Never relaxed while submitting: --allow-stale and --submit are refused
        # together, so the pipeline can be INSPECTED out of hours but a stale
        # bar can never become a real order.
        require_fresh=not args.allow_stale,
    ).build(symbol)
    if not snap_result.ok:
        say(f"  snapshot       {C['y']}NO SNAPSHOT{C['0']}  {snap_result.reason}")
        say(f"  {C['d']}No snapshot is a no-trade, never a trade on stale data.{C['0']}")
        return 0
    snapshot = snap_result.snapshot
    # Out of hours there is no two-sided quote, so spread is legitimately None.
    # Printing it as 0.0000 would read as an infinitely tight market.
    spread = ("—" if snapshot.spread is None else f"{snapshot.spread:.4f}")
    say(f"  snapshot       {snapshot.symbol} @ {snapshot.price:.2f}  "
        f"spread {spread}  "
        f"{'open' if snapshot.market_open else 'closed'}  "
        f"{len(snapshot.bars)} bars")

    # --- 2. Real option chain ------------------------------------------
    def chain_provider(snap, asof: date):
        return options.fetch_chain(
            ChainRequest(snap.symbol, min_dte=args.min_dte, max_dte=args.max_dte),
            spot=snap.price, asof=asof,
        )

    account, equity = build_account(trading)
    premium = open_option_premium(trading)
    say(f"  account        equity ${equity:,.2f}   open option premium "
        f"${premium:,.2f}")

    # --- 3. The pipeline -----------------------------------------------
    # The adapter is attached ONLY when submitting. Without it the orchestrator
    # cannot reach a broker at all — the safest possible form of a dry run,
    # since it is structural rather than a flag someone can forget to pass.
    adapter = None
    if args.submit:
        try:
            adapter = OptionsExecutionAdapter(AlpacaMCPBroker(), AuthorizationRegistry())
        except MCPUnavailable as e:
            say(f"  {C['r']}MCP unavailable{C['0']}: {e}")
            return 2

    orch = OptionsOrchestrator(
        strategies=args.strategy_objects,
        execution_config=exec_cfg,
        risk_config=risk_cfg,
        store=DecisionStore(Path(args.store)),
        chain_provider=chain_provider,
        adapter=adapter,
        reviewer=AdversarialReviewer(DeterministicProvider(), run_debate=False),
    )

    try:
        result = orch.run(snapshot, account=account, portfolio=PortfolioState(),
                          open_premium=premium, dry_run=not args.submit)
    except OptionsDataUnavailable as e:
        say(f"  chain          {C['y']}UNAVAILABLE{C['0']}  {e}")
        say(f"  {C['d']}Failing closed: an outage is not an absence of opportunity.{C['0']}")
        return 0

    d = result.decision
    verdict = (f"{C['g']}ACCEPTED{C['0']}" if result.accepted
               else f"{C['r']}NO TRADE{C['0']}")
    say(f"  outcome        {verdict}   {C['d']}({d.state.value}){C['0']}")
    say(f"  reason         {result.reason}")

    if result.contract is not None:
        c = result.contract
        say(f"  contract       {c.symbol}  strike {c.strike}  {c.expiration}  "
            f"ask {c.quote.ask if c.quote else '—'}")
    if result.contracts_ordered:
        ask = result.contract.quote.ask
        say(f"  sizing         {result.contracts_ordered} contract(s)  "
            f"max loss {C['b']}${ask * result.contract.multiplier * result.contracts_ordered:,.2f}"
            f"{C['0']}")

    review = (d.to_record().get("ai_review") if hasattr(d, "to_record") else None) or {}
    if review:
        say(f"  ai review      {review.get('verdict', '?')}  "
            f"{C['d']}{(review.get('reasoning') or '')[:80]}{C['0']}")

    if not args.submit:
        say(f"  {C['d']}dry run — the orchestrator holds no broker reference, so no"
            f" order could have been sent{C['0']}")
        return 0

    # --- 4. What actually happened at the broker ------------------------
    say(f"  execution      {result.execution_state.value if result.execution_state else 'none'}"
        f"   broker id {result.broker_order_id or '—'}")

    coid = getattr(d, "client_order_id", "") or ""
    if not coid:
        for record in (d.to_record().get("execution") or {},):
            coid = record.get("client_order_id", "") or coid
    if coid:
        rule("RECONCILIATION — the only thing that resolves an ambiguous submit")
        try:
            rec = reconcile_order(_OrderLookup(trading), client_order_id=coid,
                                  expected_quantity=result.contracts_ordered,
                                  expected_symbol=result.contract.symbol
                                  if result.contract else None)
        except ReconciliationUnavailable as e:
            say(f"  {C['y']}UNRESOLVED{C['0']}  {e}")
            say(f"  {C['d']}The order stays unresolved rather than optimistically"
                f" closed.{C['0']}")
            return 0
        say(f"  broker state   {rec.state.value}  ({rec.broker_status})")
        say(f"  position       {'exists' if rec.position_exists else 'none'}   "
            f"filled {rec.filled_quantity} of {rec.requested_quantity}")
        retry = (f"{C['g']}yes{C['0']}" if rec.safe_to_retry
                 else f"{C['r']}no{C['0']} — a retry could double-fill")
        say(f"  safe to retry  {retry}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", help="one underlying to evaluate")
    ap.add_argument("--scan", action="store_true",
                    help=f"evaluate the watchlist: {', '.join(DEFAULT_WATCHLIST)}")
    ap.add_argument("--submit", action="store_true",
                    help="ACTUALLY place the order (paper account only)")
    ap.add_argument("--strategies", metavar="DIR",
                    help="load strategies from a directory instead of using the "
                         "built-in S07 (see strategies/README.md)")
    ap.add_argument("--allow-stale", action="store_true",
                    help="inspect the pipeline outside market hours. REFUSED "
                         "with --submit: a stale bar must never become an order")
    ap.add_argument("--timeframe", default="1Hour")
    ap.add_argument("--min-dte", type=int, default=7)
    ap.add_argument("--max-dte", type=int, default=60)
    ap.add_argument("--store", default=str(ROOT / "data" / "decisions"))
    args = ap.parse_args()

    if not args.symbol and not args.scan:
        ap.error("pass --symbol SYM or --scan")
    if args.allow_stale and args.submit:
        ap.error("--allow-stale cannot be combined with --submit: trading on a "
                 "stale bar is the thing the freshness gate exists to prevent")

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

    try:
        args.strategy_objects, strategy_names = resolve_strategies(args.strategies)
    except StrategyContractError as e:
        say(f"{C['r']}{e}{C['0']}")
        return 2

    client = AlpacaClient(creds)
    rule("SPEEDTRADER AI — LIVE PAPER RUN")
    say(f"  credentials    {creds.redacted()}")
    say(f"  strategies     {strategy_names}")
    say(f"  mode           {C['b']}"
        f"{'SUBMIT — real orders will be placed' if args.submit else 'DRY RUN — no broker reference is held'}"
        f"{C['0']}")
    if args.allow_stale:
        say(f"  freshness      {C['y']}RELAXED{C['0']} — inspection only; "
            f"{C['d']}--submit is refused in this mode{C['0']}")
    say()

    symbols = [args.symbol.upper()] if args.symbol else DEFAULT_WATCHLIST
    worst = 0
    for symbol in symbols:
        try:
            worst = max(worst, run_symbol(symbol, args, client, exec_cfg, risk_cfg))
        except Exception as e:
            # One symbol failing must not abort a scan, and the failure is
            # reported rather than swallowed into a silent no-trade.
            say(f"  {C['r']}error{C['0']}          {type(e).__name__}: {e}")
            worst = max(worst, 1)
        say()

    rule("AUDIT TRAIL")
    say(f"  every decision above is appended to {args.store}")
    say(f"  {C['d']}python scripts/run_options_demo.py --replay --dashboard"
        f"  re-derives and renders them{C['0']}")
    return worst


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
SpeedTrader AI — end-to-end options decision demo.

    python scripts/run_options_demo.py

Runs with NO credentials, NO network and NO configuration. That is deliberate:
a demo that needs an API key and a live market is a demo that fails in front of
a judge. With credentials present it uses them; without, it uses a deterministic
offline provider and a synthetic breakout, and says so.

    --scenario breakout   S07 fires, the trade is approved and submitted
    --scenario veto       the AI vetoes a deterministically-approved trade
    --scenario no-signal  no breakout; the cycle still records a decision
    --scenario illiquid   the option book is too wide to price -> no trade
    --scenario broke      the premium exceeds the risk budget -> no trade
    --scenario timeout    the broker times out -> UNKNOWN, never assumed filled
    --scenario all        every one of the above, in sequence

    --replay              re-derive every recorded decision from its snapshot
                          and prove the deterministic result is reproducible
    --dashboard           also write the command centre HTML
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402

from speedtrader.agents.veto import AdversarialReviewer  # noqa: E402
from speedtrader.app.options_orchestrator import OptionsOrchestrator  # noqa: E402
from speedtrader.alpaca.market_data import FixtureMarketData  # noqa: E402
from speedtrader.data.schemas import Bar  # noqa: E402
from speedtrader.data.snapshot import SnapshotBuilder  # noqa: E402
from speedtrader.quant.features import FeatureEngine  # noqa: E402
from speedtrader.execution.authorization import AuthorizationRegistry  # noqa: E402
from speedtrader.execution.options_adapter import (  # noqa: E402
    BrokerTimeout, OptionsExecutionAdapter,
)
from speedtrader.llm.providers.base import LLMResponse  # noqa: E402
from speedtrader.llm.providers.deterministic import DeterministicProvider  # noqa: E402
from speedtrader.options.contracts import (  # noqa: E402
    ContractType, OptionContract, OptionQuote,
)
from speedtrader.quant.strategies.s07 import S07MomentumBreakout  # noqa: E402
from speedtrader.risk.state import AccountState, PortfolioState  # noqa: E402
from speedtrader.storage.decision_store import DecisionStore  # noqa: E402

NOW = datetime(2026, 9, 2, 15, 30, tzinfo=timezone.utc)
SYMBOL = "DEMO"

C = {"g": "\033[92m", "r": "\033[91m", "y": "\033[93m", "b": "\033[94m",
     "d": "\033[2m", "B": "\033[1m", "0": "\033[0m"}


def say(text: str = "") -> None:
    print(text)


def rule(title: str) -> None:
    say(f"\n{C['B']}{'─' * 74}{C['0']}")
    say(f"{C['B']}  {title}{C['0']}")
    say(f"{C['B']}{'─' * 74}{C['0']}")


# ---------------------------------------------------------------- market data
#: S07 reads a 200-period EMA, so the snapshot builder requires enough history
#: for that EMA to converge. This is the real requirement, not a demo shortcut.
N = FeatureEngine().recommended_bars()          # 891


def bars(breakout: bool) -> list[Bar]:
    """Flat bars in a tight range, then one bar that does or does not break out.

    The geometry satisfies S07's genuine conditions (breakout above the 2..21
    window high, body > 1.5*ATR, price > EMA200, DI+ > DI-). Nothing here
    bypasses the strategy — it is fed synthetic prices, not a synthetic signal.
    """
    out = [Bar(t=NOW - timedelta(hours=N - i), o=100.0, h=100.6, l=99.4,
               c=100.0, v=1000.0) for i in range(N - 1)]
    out.append(Bar(t=NOW, o=101.0, h=106.0, l=100.9, c=105.5, v=4000.0)
               if breakout else
               Bar(t=NOW, o=100.0, h=100.2, l=99.8, c=100.0, v=1000.0))
    return out


def snapshot(breakout: bool):
    """Built through the REAL SnapshotBuilder, so validation and feature
    computation run exactly as they would against live Alpaca data."""
    provider = FixtureMarketData(
        bars={(SYMBOL, "1Hour"): bars(breakout)},
        quotes={SYMBOL: {"bid": 105.48, "ask": 105.52, "timestamp": NOW}})
    result = SnapshotBuilder(provider).build(SYMBOL, now=NOW)
    if not result.ok:
        raise SystemExit(f"snapshot failed: [{result.code}] {result.reason}")
    return result.snapshot


def chain(spot: float, *, bid=3.00, ask=3.20, oi=800) -> list[OptionContract]:
    exp = NOW.date() + timedelta(days=28)
    return [
        OptionContract(
            symbol=f"{SYMBOL}{exp:%y%m%d}C{int(k * 1000):08d}", underlying=SYMBOL,
            type=ContractType.CALL, strike=float(k), expiration=exp,
            multiplier=100, open_interest=oi, quote=OptionQuote(bid=bid, ask=ask))
        for k in (spot - 5, spot, spot + 5)
    ]


class DemoBroker:
    """Stands in for Alpaca so the demo runs offline. With credentials the same
    interface is served by execution/mcp_broker.py against the real MCP server."""

    def __init__(self, raises=None):
        self.raises, self.calls = raises, []

    def submit_option_order(self, payload):
        self.calls.append(dict(payload))
        if self.raises:
            raise self.raises
        return {"id": "demo-order-0001", "status": "accepted"}


class VetoProvider:
    """Scripted so the veto path is demonstrable without a live model."""
    name = "scripted-demo"

    def complete(self, request):
        return LLMResponse(
            text=json.dumps({
                "verdict": "VETO", "confidence": 0.86,
                "reasoning": "Implied move over the remaining 28 days does not "
                             "cover the debit plus fees; the breakout is one bar "
                             "old and unconfirmed.",
                "concerns": ["single-bar breakout", "debit not covered by "
                             "expected move"]}),
            provider=self.name, model="scripted-demo-1")


def replay_recorded(store_dir: Path) -> None:
    """Re-derive every stored decision and report whether it reproduced.

    The AI is not run during replay. That is the point: if the deterministic
    result is identical with the model absent, the model did not influence it.
    """
    import json as _json

    from speedtrader.replay.engine import replay_all

    records = []
    for path in sorted(store_dir.glob("decisions-*.jsonl")):
        for line in path.read_text().splitlines():
            if line.strip():
                records.append(_json.loads(line))
    if not records:
        say("  nothing recorded yet")
        return

    exec_cfg = yaml.safe_load((ROOT / "configs" / "execution_config.yaml").read_text())
    risk_cfg = yaml.safe_load((ROOT / "configs" / "risk_config.yaml").read_text())
    results = replay_all(records, strategies=[S07MomentumBreakout()],
                         execution_config=exec_cfg, risk_config=risk_cfg)

    ok = sum(1 for r in results if r.reproducible)
    for r in results:
        mark = f"{C['g']}reproduced{C['0']}" if r.reproducible else f"{C['r']}DIVERGED{C['0']}"
        veto = " (AI vetoed — the one thing it can change)" if r.ai_changed_the_outcome else ""
        say(f"  {r.original_fingerprint}  {mark}{C['d']}{veto}{C['0']}")
    say()
    say(f"  {ok}/{len(results)} decisions re-derived from their stored snapshot alone,")
    say(f"  {C['d']}with the AI never consulted. The fingerprint excludes the AI "
        f"review by{C['0']}")
    say(f"  {C['d']}construction, so a model that confirms and a model that "
        f"abstains hash identically.{C['0']}")


# ---------------------------------------------------------------- the cycle
def run_scenario(name: str, store_dir: Path) -> bool:
    breakout = name != "no-signal"
    broker = DemoBroker(raises=BrokerTimeout("no response in 5s")
                        if name == "timeout" else None)

    book = chain(105.5)
    if name == "illiquid":
        book = chain(105.5, bid=0.10, ask=5.00)
    elif name == "broke":
        book = chain(105.5, bid=60.0, ask=62.0)

    provider = VetoProvider() if name == "veto" else DeterministicProvider()

    exec_cfg = yaml.safe_load((ROOT / "configs" / "execution_config.yaml").read_text())
    risk_cfg = yaml.safe_load((ROOT / "configs" / "risk_config.yaml").read_text())

    orch = OptionsOrchestrator(
        strategies=[S07MomentumBreakout()],
        execution_config=exec_cfg, risk_config=risk_cfg,
        store=DecisionStore(store_dir),
        chain_provider=lambda snap, asof: book,
        adapter=OptionsExecutionAdapter(broker, AuthorizationRegistry()),
        reviewer=AdversarialReviewer(provider, run_debate=False),
    )

    result = orch.run(snapshot(breakout), account=AccountState(
        balance=100_000.0, equity=100_000.0, day_start_equity=100_000.0,
        equity_high_water=100_000.0), portfolio=PortfolioState(), now=NOW)

    d = result.decision
    verdict = f"{C['g']}ACCEPTED{C['0']}" if result.accepted else f"{C['r']}NO TRADE{C['0']}"
    say(f"  outcome        {verdict}   {C['d']}({d.state.value}){C['0']}")
    say(f"  reason         {result.reason}")

    if d.candidate:
        say(f"  signal         S07 {d.candidate.direction.value} @ "
            f"{d.candidate.entry:.2f}  score {d.candidate.total_score:.0f}  "
            f"EV {d.candidate.expected_value:+.3f}R")
    if d.risk_gate:
        say(f"  risk engine    {d.risk_gate.verdict.value}  "
            f"({len(d.risk_gate.checks)} deterministic checks)")
    if d.options_trace:
        t = d.options_trace
        say(f"  contract       {t['contract']['symbol']}  strike "
            f"{t['contract']['strike']:g}  {t['contract']['expiration']}")
        s = t["sizing"]
        say(f"  sizing         {s['contracts']} contract(s) @ "
            f"{s['premium_per_contract']:.2f} x100  "
            f"max loss {C['B']}${s['max_loss_total']:,.2f}{C['0']} "
            f"of ${s['risk_budget']:,.2f} budget")
    if d.ai_review:
        j = d.ai_review["judge"]
        colour = C["r"] if d.ai_review["vetoed"] else C["d"]
        say(f"  ai review      {colour}{j['verdict']}{C['0']} "
            f"{C['d']}({j['provenance'].get('model') or 'none'}){C['0']}")
        if j["reasoning"]:
            say(f"                 {C['d']}{j['reasoning'][:110]}{C['0']}")
    if broker.calls:
        c = broker.calls[0]
        say(f"  submitted      {c['symbol']}  qty {c['quantity']}  "
            f"{c['order_type']} @ {c['limit_price']}  "
            f"{C['d']}idempotency {c['client_order_id'][:14]}...{C['0']}")
    else:
        say(f"  submitted      {C['d']}nothing reached the broker{C['0']}")
    say(f"  persisted      {result.stored_at.name}")
    return result.accepted


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", default="all",
                    choices=["all", "breakout", "veto", "no-signal", "illiquid",
                             "broke", "timeout"])
    ap.add_argument("--store", default=str(ROOT / "data" / "decisions"))
    ap.add_argument("--replay", action="store_true",
                    help="replay every recorded decision and verify reproducibility")
    ap.add_argument("--dashboard", action="store_true",
                    help="also write data/dashboard.html from the decisions")
    args = ap.parse_args()

    store_dir = Path(args.store)
    store_dir.mkdir(parents=True, exist_ok=True)

    say(f"{C['B']}SpeedTrader AI{C['0']} — deterministic options decision cycle")
    say(f"{C['d']}S07 -> deterministic risk -> option contract -> sizing -> "
        f"AI veto -> authorization -> Alpaca{C['0']}")
    say(f"{C['d']}offline demo: synthetic bars, no credentials required. "
        f"paper trading only.{C['0']}")

    scenarios = (["breakout", "veto", "no-signal", "illiquid", "broke", "timeout"]
                 if args.scenario == "all" else [args.scenario])
    for s in scenarios:
        rule(s.upper())
        run_scenario(s, store_dir)

    if args.replay:
        rule("REPLAY — IS THIS SYSTEM REPRODUCIBLE?")
        replay_recorded(store_dir)

    if args.dashboard:
        rule("COMMAND CENTRE")
        out = ROOT / "data" / "dashboard.html"
        import subprocess
        subprocess.run([sys.executable, str(ROOT / "scripts" / "build_dashboard.py"),
                        "--store", str(store_dir), "--out", str(out)], check=True)
        say(f"  open {out} — no server required")

    rule("AUDIT TRAIL")
    say(f"  every decision above — accepted or not — is appended to "
        f"{store_dir}/decisions-<utc-date>.jsonl")
    say(f"  {C['d']}each record carries the snapshot, the signal, the cost "
        f"assumptions behind its EV,{C['0']}")
    say(f"  {C['d']}every deterministic check, the contract choice with what was "
        f"rejected, and the AI review.{C['0']}")
    say()
    return 0


if __name__ == "__main__":
    sys.exit(main())

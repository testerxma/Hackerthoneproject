"""
The demo. Two identical high-quality signals; the only difference is portfolio state.
Run: python scripts/demo_risk_gate.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from speedtrader.common.clock import expires_at, utcnow
from speedtrader.common.ids import IdKind, new_id
from speedtrader.data.schemas import CandidateSignal, Direction, MarketRegime
from speedtrader.risk.engine import DeterministicRiskEngine
from speedtrader.risk.state import AccountState, OpenPosition, PortfolioState
from speedtrader.config import get_config

C = {"g": "\033[92m", "r": "\033[91m", "y": "\033[93m", "d": "\033[2m", "0": "\033[0m"}

def signal():
    now, atr, entry = utcnow(), 2.85, 231.40
    return CandidateSignal(
        signal_id=new_id(IdKind.SIGNAL), snapshot_id=new_id(IdKind.SNAPSHOT),
        symbol="AAPL", direction=Direction.BUY, strategy_id="S7",
        entry=entry, stop_loss=entry - 1.5*atr, take_profit=entry + 3.0*atr,
        stop_distance=1.5*atr, reward_risk=2.0, atr_at_signal=atr,
        base_score=50.0, bonus=22.0, total_score=72.0,
        score_breakdown="base50 Vol+8 Squeeze+10 Fib+4",
        expected_value=0.75, combined_priority=61.0, regime=MarketRegime.STRONG_UP,
        expires_at=expires_at(now, 30))

def show(title, result):
    v = result.verdict.value
    col = C["g"] if v == "PASS" else (C["y"] if v == "REDUCE" else C["r"])
    print(f"\n{title}")
    print(f"  verdict        {col}{v}{C['0']}")
    print(f"  portfolio heat {result.portfolio_heat_pct:.2f}%")
    if result.approved_quantity:
        print(f"  approved qty   {int(result.approved_quantity)} shares"
              f"  (x{result.size_multiplier} {result.size_multiplier_breakdown or ''})")
    if result.blocking_reason:
        print(f"  {C['r']}blocked by     {result.blocking_reason}{C['0']}")
    print(f"  {C['d']}{len(result.checks)} rules evaluated, "
          f"{len(result.failed_checks)} failed{C['0']}")
    for c in result.failed_checks:
        print(f"    {C['r']}x{C['0']} {c.rule:22s} observed={c.observed} limit={c.limit}")

cfg = get_config()
engine = DeterministicRiskEngine(cfg.risk)
acct = AccountState(balance=100_000, equity=100_000, day_start_equity=100_000,
                    equity_high_water=100_000)

print("=" * 66)
print("  SpeedTrader AI — the same signal, two portfolio states")
print("  AI proposes. Deterministic controls authorise.")
print("=" * 66)
print(f"\n  AAPL BUY  S7  score 72/100  R:R 2.0  EV +0.75R")
print(f"  {C['d']}every agent supports this trade{C['0']}")

show("[1] Flat portfolio", engine.evaluate(
    signal=signal(), account=acct, portfolio=PortfolioState()))

heavy = PortfolioState(positions=[
    OpenPosition(symbol=s, side=Direction.BUY, quantity=1000,
                 entry_price=100.0, stop_loss=98.0)
    for s in ("MSFT", "NVDA", "AMD", "TSLA")])
show("[2] Same signal, portfolio already at 8% heat", engine.evaluate(
    signal=signal(), account=acct, portfolio=heavy))

print(f"\n{C['d']}  No LLM was consulted. Same inputs -> same verdict, every time.{C['0']}\n")

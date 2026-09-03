#!/usr/bin/env python3
"""
SpeedTrader AI — bring your own strategy

    python scripts/strategy_tool.py new my_edge   # scaffold a strategy
    python scripts/strategy_tool.py check         # validate strategies/
    python scripts/strategy_tool.py list          # what would load, and from where

S07 is a worked example, not the product. The product is everything below it —
22 deterministic risk checks, options sizing against an exactly known maximum
loss, a single-use execution licence, reconciliation that refuses an unsafe
retry, a reproducible decision journal, and an AI layer that can only subtract.
None of that is specific to S07, so your strategy should be able to inherit all
of it by writing one file.

`check` runs your strategy rather than inspecting it, because the failures that
matter are semantic and silent: a stop on the wrong side of entry, a
non-deterministic evaluate that cannot be replayed, or one that edits the
snapshot every other layer is reading.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from speedtrader.quant.strategies.plugins import (  # noqa: E402
    StrategyContractError, load_directory, load_file, probe_snapshot,
    validate_strategy,
)

C = {"b": "\033[1m", "d": "\033[2m", "g": "\033[92m", "r": "\033[91m",
     "y": "\033[93m", "0": "\033[0m"}
DEFAULT_DIR = ROOT / "strategies"

TEMPLATE = '''"""
{title}

Replace this docstring with what your strategy actually does and where the rule
came from. It is written onto every decision this strategy produces, and in six
months it is the only thing that will tell you why the numbers are what they are.
"""
from __future__ import annotations

from speedtrader.data.schemas import Direction, MarketSnapshot
from speedtrader.quant.strategies.base import (
    Code, StrategyOutput, StrategyResult,
)


class {classname}:
    #: Stable and unique. The decision journal and per-strategy statistics key
    #: off this, so changing it later orphans the history you have collected.
    id = "{sid}"

    #: Where the rule came from — a paper, a book, a repository, your own
    #: research note. Free text, carried onto every decision.
    source_reference = "TODO: cite where this rule comes from"

    #: Fewest bars you can be evaluated on. Be honest: asking for 50 and reading
    #: 200 is how a strategy silently produces different answers early in a run.
    min_bars = 50

    def evaluate(self, snapshot: MarketSnapshot) -> StrategyResult:
        """Decide whether to propose a trade.

        THREE RULES, all checked at load time by `strategy_tool.py check`:

          1. TREAT THE SNAPSHOT AS READ-ONLY. Every layer in one decision reads
             the same object, so editing it changes what the risk engine and the
             reviewer see.

          2. BE DETERMINISTIC. No clock, no randomness, no mutable state carried
             between calls. Replay re-derives every stored decision from its
             snapshot alone; a strategy that cannot be replayed is refused.

          3. DECLINING IS A RESULT, NOT AN EXCEPTION. Returning "no signal" with
             a reason is normal operation and is recorded. Raising is a bug.

        You do NOT size the position, choose the option contract, or decide
        whether the trade is allowed. Propose the trade; the deterministic layer
        judges it.
        """
        bars = snapshot.bars
        if len(bars) < self.min_bars:
            return StrategyResult(
                ok=False,
                reason=f"need {{self.min_bars}} bars, have {{len(bars)}}",
                code=Code.INSUFFICIENT_HISTORY,
            )

        price = snapshot.price
        if price is None or price <= 0:
            return StrategyResult(ok=False, reason="no usable price",
                                  code=Code.NO_SIGNAL)

        # ------------------------------------------------------------------
        # YOUR EDGE GOES HERE.
        #
        # The example below is a plain 20-bar high breakout. It is a PLACEHOLDER
        # to show the shape of a signal, not a recommendation, and it is not
        # claimed to be profitable. Delete it.
        # ------------------------------------------------------------------
        window = bars[-21:-1]
        highest = max(b.h for b in window)

        if price <= highest:
            return StrategyResult(
                ok=False,
                reason=f"no breakout (price {{price:.2f}} <= 20-bar high {{highest:.2f}})",
                code=Code.NO_SIGNAL,
            )

        # Distances, not fixed numbers: a stop that is a constant amount is a
        # different amount of risk on a $5 stock and a $500 one.
        risk = price * 0.02
        return StrategyResult(
            ok=True,
            code=Code.SIGNAL,
            reason="20-bar breakout",
            output=StrategyOutput(
                strategy_id=self.id,
                direction=Direction.BUY,
                entry=price,
                # For a BUY the stop is BELOW entry and the target ABOVE it.
                # Getting this backwards does not raise — it inverts the
                # risk/reward and the expected value is then computed from a
                # reward that is really a loss. `check` refuses it for you.
                stop_loss=price - risk,
                take_profit=price + risk * 2.0,
                # 0-100. Feeds ranking and expected value. It does NOT size the
                # position: a bigger score never buys more contracts.
                base_score=50.0,
                breakdown=f"price {{price:.2f}} > 20-bar high {{highest:.2f}}",
                source_reference=self.source_reference,
                inputs={{"price": price, "highest_20": highest}},
            ),
        )
'''


def say(msg: str = "") -> None:
    print(msg, flush=True)


def cmd_new(args) -> int:
    name = args.name.strip().lower().replace("-", "_").replace(" ", "_")
    if not name.isidentifier():
        say(f"{C['r']}{args.name!r} is not a usable module name{C['0']}")
        return 2

    directory = Path(args.dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.py"
    if path.exists() and not args.force:
        say(f"{C['r']}{path} already exists{C['0']} — pass --force to overwrite")
        return 2

    classname = "".join(part.capitalize() for part in name.split("_")) or "MyStrategy"
    path.write_text(TEMPLATE.format(
        title=f"{name} — a SpeedTrader AI strategy",
        classname=classname,
        sid=name,
    ))
    say(f"{C['g']}wrote{C['0']} {path}")
    say()
    say(f"  1. open it and replace the placeholder breakout with your edge")
    say(f"  2. {C['b']}python scripts/strategy_tool.py check{C['0']}")
    say(f"  3. {C['b']}python scripts/run_live_paper.py --scan --strategies {directory}{C['0']}")
    return 0


def _report(path: Path, sid: str, problems: list[str]) -> bool:
    if not problems:
        say(f"  {C['g']}ok{C['0']}      {sid:<24} {C['d']}{path.name}{C['0']}")
        return True
    say(f"  {C['r']}REFUSED{C['0']} {sid:<24} {C['d']}{path.name}{C['0']}")
    for problem in problems:
        say(f"          {C['y']}·{C['0']} {problem}")
    return False


def cmd_check(args) -> int:
    directory = Path(args.dir)
    if not directory.is_dir():
        say(f"{C['y']}{directory} does not exist yet{C['0']} — "
            f"create a strategy with: python scripts/strategy_tool.py new my_edge")
        return 0

    files = [p for p in sorted(directory.glob("*.py"))
             if not p.name.startswith("_")]
    if not files:
        say(f"{C['y']}no strategies in {directory}{C['0']}")
        return 0

    say(f"{C['b']}checking {len(files)} file(s) in {directory}{C['0']}")
    say(f"{C['d']}  each strategy is RUN on a synthetic snapshot: determinism, "
        f"snapshot mutation{C['0']}")
    say(f"{C['d']}  and stop/target geometry are only observable by running "
        f"it{C['0']}")
    say()

    snapshot = probe_snapshot()
    ok = True
    for path in files:
        try:
            loaded = load_file(path, validate=False)
        except StrategyContractError as e:
            say(f"  {C['r']}REFUSED{C['0']} {path.name}")
            say(f"          {C['y']}·{C['0']} {e}")
            ok = False
            continue
        for item in loaded:
            problems = validate_strategy(item.strategy, snapshot=snapshot)
            ok = _report(path, item.id, problems) and ok

    say()
    if ok:
        say(f"{C['g']}every strategy satisfies the contract{C['0']}")
        say(f"{C['d']}A strategy can propose a trade. It cannot size one, "
            f"authorize one, or place one.{C['0']}")
        return 0
    say(f"{C['r']}at least one strategy was refused{C['0']}")
    say(f"{C['d']}Refused at load, deliberately: a wrong-sided stop does not "
        f"fail loudly, it quietly{C['0']}")
    say(f"{C['d']}produces decisions whose expected value came from a reward "
        f"that is really a loss.{C['0']}")
    return 1


def cmd_list(args) -> int:
    try:
        loaded = load_directory(args.dir)
    except StrategyContractError as e:
        say(f"{C['r']}{e}{C['0']}")
        return 1
    if not loaded:
        say(f"{C['y']}no strategies in {args.dir}{C['0']}")
        return 0
    for item in loaded:
        say(f"  {item.id:<24} {item.path.name:<28} "
            f"{C['d']}sha256 {item.digest[:12]}…{C['0']}")
    say()
    say(f"{C['d']}The digest travels onto every decision: a backtest or a replay "
        f"is only meaningful{C['0']}")
    say(f"{C['d']}against a known version of the logic.{C['0']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    def with_dir(parser):
        # On each SUBCOMMAND rather than the top level, so `check --dir X` works
        # in the order anyone would type it.
        parser.add_argument("--dir", default=str(DEFAULT_DIR),
                            help=f"strategy directory (default: {DEFAULT_DIR})")
        return parser

    new = with_dir(sub.add_parser("new", help="scaffold a strategy from a template"))
    new.add_argument("name")
    new.add_argument("--force", action="store_true")
    new.set_defaults(func=cmd_new)

    with_dir(sub.add_parser("check", help="validate every strategy")
             ).set_defaults(func=cmd_check)
    with_dir(sub.add_parser("list", help="show what would load")
             ).set_defaults(func=cmd_list)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

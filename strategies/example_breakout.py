"""
example_breakout — a SpeedTrader AI strategy

Replace this docstring with what your strategy actually does and where the rule
came from. It is written onto every decision this strategy produces, and in six
months it is the only thing that will tell you why the numbers are what they are.
"""
from __future__ import annotations

from speedtrader.data.schemas import Direction, MarketSnapshot
from speedtrader.quant.strategies.base import (
    Code, StrategyOutput, StrategyResult,
)


class ExampleBreakout:
    #: Stable and unique. The decision journal and per-strategy statistics key
    #: off this, so changing it later orphans the history you have collected.
    id = "example_breakout"

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
                reason=f"need {self.min_bars} bars, have {len(bars)}",
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
                reason=f"no breakout (price {price:.2f} <= 20-bar high {highest:.2f})",
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
                breakdown=f"price {price:.2f} > 20-bar high {highest:.2f}",
                source_reference=self.source_reference,
                inputs={"price": price, "highest_20": highest},
            ),
        )

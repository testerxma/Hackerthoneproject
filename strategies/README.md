# Bring your own strategy

S07 is a worked example. The product is everything below it — 22 deterministic
risk checks, options sizing against an exactly known maximum loss, a single-use
execution licence, reconciliation that refuses an unsafe retry, a reproducible
decision journal, and an AI layer that can only subtract.

None of that is specific to S07. Drop one file in this directory and your
strategy inherits all of it.

```bash
python scripts/strategy_tool.py new my_edge     # scaffold a template
python scripts/strategy_tool.py check           # validate what you wrote
python scripts/strategy_tool.py list            # what would load, and its digest

# then run it against real Alpaca market data (sends no orders without --submit)

python scripts/run_live_paper.py --scan --strategies strategies/
```

Files here are **gitignored by default** — a private edge is not committed by
accident. `example_breakout.py` is tracked deliberately as a reference.

## The contract

Your strategy returns a direction, an entry, a stop, a target and a score. That
is the entire vocabulary.

> A strategy **proposes** a trade. It cannot size one, authorize one, or place
> one.

It has no reference to the broker, the risk engine, the authorization registry
or the account, and it is handed a frozen snapshot rather than a data client.
A score of 999 does not buy a bigger position — size comes from the risk budget
divided by what a contract actually costs.

## What `check` enforces, and why it runs your code

Checking that an object has an `evaluate` method proves almost nothing. The
failures that hurt are semantic and silent, and each is only visible by running
the strategy:

| Refused | Why it matters |
|---|---|
| Stop on the wrong side of entry | Risk/reward inverts, and expected value is then computed from a reward that is really a loss |
| Non-deterministic `evaluate` | Replay re-derives every decision from its snapshot alone; a strategy that reads the clock or a random number cannot be replayed |
| Mutating the snapshot | Every layer in one decision reads the same object, so editing it changes what the risk engine and the reviewer see |
| Raising instead of declining | Declining to trade is normal operation and is recorded; raising is a bug |
| Returning the wrong type | The journal and ranking both key off the result shape |
| Duplicate strategy id | Two strategies sharing an id would have their histories merged |

Each file's SHA-256 is recorded on every decision it produces — a backtest or a
replay is only meaningful against a known version of the logic.

## One thing this is not

Loading a strategy file **executes it**. Python has no meaningful in-process
sandbox and this project does not pretend otherwise. Validation enforces the
*contract*, not safety from hostile code: treat a third-party strategy the way
you would treat any dependency you install, and read it first.

What is guaranteed is narrower and true: no strategy, however written, can
execute a trade, enlarge a position, weaken a risk limit, or bypass the licence.
Those paths do not exist from here.

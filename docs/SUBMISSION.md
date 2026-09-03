# SpeedTrader AI — engineering log

**Alpaca AI Trading Agents Hackathon, Aug 28 – Sep 4, 2026.**

This is the detailed record of what was built, what was verified against a real
Alpaca paper account, what was deliberately rejected and why, and how to
reproduce every claim in the top-level [README](../README.md). The README is
the pitch; this is the audit trail behind it.

---

## 1. What this project is

An options trading system where a deterministic risk engine has sole authority
to trade, and an AI layer can only **veto** — never propose, size, or execute.
S07 (a momentum-breakout strategy ported line-for-line from a real MT5 bot) is
the built-in signal generator, but the system now accepts **any** strategy that
satisfies a narrow, mutation-tested contract (see §6).

## 2. Setup

```bash
git clone <this repo>
cd Hackerthoneproject
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

No API key is required for the offline demo. Live paper trading needs Alpaca
credentials:

```bash
cp .env.example .env      # fill in ALPACA_API_KEY / ALPACA_SECRET_KEY
pip install -e ".[live]"      # MCP broker transport
```

## 3. How to run everything

```bash
# offline, zero-setup, deterministic — six scenarios covering the whole pipeline
python scripts/run_options_demo.py --replay --dashboard

# the full test suite
python -m pytest -q                          # 1116 passed, 1 skipped

# mutation testing — proves every safety guard is load-bearing, not decorative
python scripts/mutation_test.py               # 78 mutants: 76 killed, 2 equivalent

# against the real Alpaca paper API (sends orders only with --submit)
python scripts/run_live_paper.py --scan
python scripts/run_live_paper.py --symbol SPY --submit

# bring your own strategy
python scripts/strategy_tool.py new my_edge
python scripts/strategy_tool.py check
python scripts/run_live_paper.py --scan --strategies strategies/
```

`data/dashboard.html` is the generated command centre — open it in a browser
after running the demo or a live scan. It is a static, offline-readable report
(three lanes per decision: AI advisory, deterministic authority, execution
outcome), not a server you start.

## 4. What was verified against the real Alpaca API, not just mocks

Two real option orders were placed on a paper account through the full
production stack (strategy → risk engine → sizing → licence → MCP broker):

```
SOFI261002C00017000    qty 1  limit 1.50  accepted   id ffeb8132-de46-4a15-8f26-fe476fcb6995
F261002C00013500       qty 1  limit 0.98  accepted   id ccbade60-0089-47f0-87ae-c58db27ed0af
```

Running against the live API — not mocks — found three real defects that no
fixture could have shown, because a fixture returns exactly the data you wrote
into it. All three are fixed and pinned by regression tests and mutants:

1. **Alpaca caps the option-quote endpoint at 100 symbols.** A 354-contract
   chain returned `symbol limit is 100`, so nothing could be priced. The
   adapter had correctly failed *closed* (no chain, not an empty one) — right
   behaviour, real bug. Fixed by batching quote requests
   (`alpaca/options_data.py`, mutant `quote-batch-uncapped`).
2. **The MCP server nests its reply two levels deep**
   (`{"_alpaca_mcp_security": ..., "data": {"result": <order>}}`). The parser
   originally unwrapped only `result`, so a genuinely *successful* submission
   was reported `UNKNOWN`. The order had actually been placed — verified
   directly against the broker. Fixed with a bounded, self-terminating
   envelope unwrapper (`execution/mcp_broker.py`, mutant `envelope-unbounded`).
   This is the exact failure mode the reconciliation layer exists for, and it
   worked as designed: no double order was ever placed.
3. **The historical-bars lookback window was computed in calendar time, not
   trading time.** A calendar week has 168 hours but only ~32.5 regular market
   hours, so a request for the 891 hourly bars an EMA200 needs to converge
   returned 660 — meaning the system could never trade its own default
   timeframe. It failed closed (no trade rather than a bad one), but it could
   never have failed any other way, and no fixture would reveal it. Fixed by
   deriving the calendar/trading-hours ratio explicitly
   (`alpaca/market_data.py`, mutant `lookback-calendar-units`).

## 5. What was rejected, and why

- **Options P&L backtesting.** No historical option-chain data exists to
  backtest against; simulating it would require a pricing model, and the
  backtest would then measure the pricing model, not the strategy. The
  underlying-signal backtest (walk-forward, cost-sensitivity) is real; nothing
  claims P&L anywhere in this repository.
- **Vertical spreads.** Declared (`VERTICAL_DEBIT`) but explicitly refused: a
  two-leg order can leg out mid-fill, so maximum loss is not exactly known at
  submission time — the one property long single-leg options were chosen for.
  (An earlier draft of this reasoning also cited options-approval level; the
  paper account is level 3, so that reason was false and has been removed
  rather than left standing.)
- **S01–S14 (the rest of the MT5 strategy suite).** Only S07 is ported.
  Inventing the others from scratch would violate the same-source-of-truth
  rule that every formula in this codebase is cited to a specific line in
  `docs/reference/SpeedTraderBot_v6.1.mq5`.
- **Greeks-based selection.** Contract selection uses moneyness and DTE, which
  are exact and require no pricing model. No delta target is claimed.
- **A flat (non-tiered) cost-policy schema**, an earlier owner preference. The
  repository's own cost-provenance code already used a nested,
  price/direction-aware contract; rewriting it to match the flat preference
  would have thrown away real information (commission charged on both legs,
  SEC fee priced off the actual sell leg) for no benefit. Documented and
  rejected on the evidence, with the owner's explicit sign-off to exercise
  that judgment.

## 6. Bring your own strategy — the most recent addition

S07 is a worked example, not the product. The product is everything **below**
a strategy: 22 deterministic risk checks, options sizing against an exactly
known maximum loss, a single-use execution licence, reconciliation that
refuses unsafe retries, a reproducible decision journal, and an AI layer that
can only subtract. None of that is S07-specific.

A strategy returns a direction, entry, stop, target, and score — nothing else.
It holds **no reference** to the broker, risk engine, authorization registry,
or account, and is handed a frozen, read-only snapshot. `strategy_tool.py
check` validates a candidate strategy by **running** it (not just inspecting
its shape), because the failures that matter are silent:

| Refused at load | Because |
|---|---|
| Stop on the wrong side of entry | Inverts risk/reward; EV would be computed from a reward that is really a loss |
| Non-deterministic `evaluate` | Breaks replay, which re-derives every decision from its snapshot alone |
| Mutates the snapshot | Every other layer in the decision reads that same object |
| Raises instead of declining | Declining is a normal, recorded result |
| Duplicate strategy id | Two decision histories would silently merge |

Writing the validator's own tests found a real gap: `id = None` satisfied
`hasattr()` and would have reached the decision journal as a null key. Fixed
before this shipped.

**Honestly stated limit:** loading a strategy file *executes* it. Python has no
real in-process sandbox, so this enforces the *contract*, not safety from
hostile code — a third-party strategy should be read like any other
dependency. What is structurally guaranteed, and enforced by the code path
itself rather than by convention, is narrower: no strategy can size a trade,
authorize one, or reach the broker. Those paths do not exist from a strategy
object.

## 7. Numbers, as of the final commit

- **1116 tests passing**, 1 skipped
- **49 mutation tests**: 76 killed, 0 survived, 2 declared equivalent (with the
  argument for why, and the harness fails if either is ever actually killed —
  that would mean the argument was wrong)
- **2 real orders placed** on the Alpaca paper API through the full stack
- **3 live-only defects found and fixed**, each pinned by a regression test
  and a mutation test
- CI runs on Python 3.11 and 3.12, plus a dedicated mutation-testing job and a
  safety-invariants job (paper-only enforcement, fail-closed config, no
  committed secrets)

## 8. Known limitations, stated plainly

- Only S07 of the original bot's S01–S14 strategy suite is ported.
- The offline backtest measures the underlying signal only, never options P&L.
- Strategy-plugin loading executes arbitrary Python; it is a contract
  enforcer, not a sandbox.
- No performance or profitability claim is made anywhere in this repository —
  every result shown is paper trading or synthetic demo data, clearly labelled
  as such.

## 9. Repository hygiene performed for submission

- Removed a vestigial `dashboard/` directory containing a directory literally
  named `{components,pages}` (a shell brace-expansion that never executed).
- Untracked `.coverage`, a build artifact that had been committed by mistake;
  gitignored `.coverage*`, `htmlcov/`, `*.egg-info/`, `build/`, `dist/`.
- Added `LICENSE` (MIT), required for lablab.ai submission.
- Verified no credential-shaped literal or the live paper-account key/secret
  appears in any tracked file (checked by the CI `safety-invariants` job and
  independently by a full-repository grep before every push).

- Removed ten empty scaffolding packages (`agents/{analysts,research,risk,
  trading}`, `bridge`, `evidence`, `llm/models`, `memory`, `monitoring`,
  `portfolio`) — every one 0 bytes and verified unreferenced before removal.
- Gitignored generated runtime artefacts (`data/execution_intents.jsonl`,
  `data/STOP`) and every `.env` variant, not just `.env` itself.
- Rewrote a test fixture that was shaped exactly like a real API key: it tripped
  the repository's own secret scanner, and a scanner people learn to ignore is
  worse than no scanner.

## 10. Still open — owner action required

These cannot be completed from inside this repository:

- **Pitch video** (≤5 min, MP4) and **slide presentation** — both required by
  the lablab.ai submission form. Neither can be produced by this agent.
- **Cover image** for the submission listing.
- **Team name and contact email** — a placeholder is left in the README.
- **Alpaca paper account ID** must be entered on the submission form. The
  dedicated account is `PA3FJIP2GIRB` (created for this submission, $100,000
  starting balance, options level 3, zero pre-existing orders — verified).
- **Rotate the API keys** that were shared during development once the
  hackathon is over. They are in `.env`, which is gitignored and appears in no
  tracked file or commit — but they passed through a chat transcript.
- **Up to 5 social posts** on X / LinkedIn tagging @lablabai and @AlpacaHQ, for
  the separate social-engagement prize.
- **Demo application URL**, if the form requires one distinct from the repo.
  `data/dashboard.html` is a single self-contained file, so any static host
  serves it as-is.

## 11. On the P&L judging criterion, stated plainly

P&L is the first judging criterion, and this account's P&L is currently
**$0.00** with zero fills. That is reported, not hidden, on the dashboard and
here.

The honest position: this system is built to decline trades it cannot justify,
and it spent the development window outside market hours, where it correctly
refuses to price options against stale quotes. Two earlier orders on the
development account were submitted pre-market and never became marketable —
root-caused above, and the market-hours gate now prevents exactly that.

No P&L figure is fabricated, annualised, back-filled or simulated anywhere in
this repository to improve that number. A zero is a factual state, and
manufacturing activity to disguise it would invalidate every other claim the
audit trail makes.

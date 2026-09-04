<img src="docs/assets/speedtrader-banner.png" alt="SpeedTrader AI" width="100%">

<sub><i>Concept banner. The dashboard figures in it are illustrative design
mock-ups, not results — this project makes no performance claim anywhere. The
real generated command centre is <code>data/dashboard.html</code>.</i></sub>

# SpeedTrader AI

**An options trading agent where AI can challenge a trade, but deterministic
controls alone can authorize execution.**

`Alpaca AI Trading Agents Hackathon` · `Paper trading only` · `Alpaca-native` ·
`Options-first` · `Replayable` · `Auditable`

```bash
pip install -e ".[dev]"
python scripts/run_options_demo.py --replay --dashboard
```

No API key. No network. No configuration. That runs all six demo scenarios,
re-derives every decision from its stored snapshot, and writes the command
centre to `data/dashboard.html`.

| | |
|---|---|
| **Team** | testerxma |
| **One-page write-up** | [`docs/ONE_PAGER.md`](docs/ONE_PAGER.md) |
| **Engineering log** | [`docs/SUBMISSION.md`](docs/SUBMISSION.md) |
| **Paper account** | `PA3FJIP2GIRB` · dedicated to this hackathon · $100,000 starting balance |
| **License** | [MIT](LICENSE) |

---

## Why SpeedTrader?

Most AI trading agents propose first and rely on guardrails to catch mistakes
afterward. SpeedTrader inverts that order:

> **The AI does not create a trade.** A deterministic strategy produces the
> candidate, a deterministic risk engine sizes and approves it, and only then
> is a model consulted — to **confirm, abstain, or veto**. Never to originate,
> enlarge, or authorize.

This is not a prompt-level convention. The AI's response is validated against a
JSON schema with `additionalProperties: false` and exactly four fields
(`verdict`, `confidence`, `reasoning`, `concerns`). A hostile model returning
`{"verdict":"CONFIRM","quantity":99999}` changes nothing, proven end-to-end by
test.

```
AI            MAY STOP a trade         (advisory)
Deterministic MAY AUTHORIZE a trade    (sole authority)
Broker        EXECUTION OBSERVATION    (never decides)
```

The practical consequence: model capability is a reasoning decision, not a
safety one. Swap in the strongest model available and its maximum authority is
still to cancel one trade.

---

## What makes it different

| | |
|---|---|
| **AI is subtractive** | Vocabulary is `CONFIRM · ABSTAIN · VETO`; it cannot resize, reprice, or authorize |
| **Exact maximum loss** | Long single-leg options only — max loss is the premium paid, known exactly at entry |
| **Deterministic risk authority** | 22 checks, all evaluated, never short-circuited; the sole source of execution authority |
| **Single-use authorization** | An HMAC licence bound by hash to the exact proposal and portfolio, non-serialisable |
| **Restart-safe execution** | Intent is fsynced to a write-ahead journal *before* the broker is contacted |
| **Reconciliation before retry** | `SUBMITTED` is never `FILLED`; `UNKNOWN` is resolved by querying the broker, never by resubmitting |
| **Deterministic replay** | A fingerprint that excludes the AI review by construction — the same market state hashes identically regardless of what the model said |

---

## Architecture

```
Market Data
   │
   ▼
S07 Quant Signal            deterministic
   │
   ▼
Options Selection + Sizing  deterministic
   │
   ▼
AI Review                   advisory — CONFIRM / ABSTAIN / VETO
   │
   ▼
Deterministic Risk          22 checks — the sole execution authority
   │
   ▼
Single-use Authorization    deterministic
   │
   ▼
Alpaca (paper)               broker — observed, not decided
   │
   ▼
Reconciliation               broker
   │
   ▼
Replay / Audit                deterministic
```

| Layer | Responsibility | Authority |
|---|---|---|
| Market / Data | Bars, quotes, account and portfolio state | data |
| Quant (S07) | Produces a candidate signal | deterministic |
| Options | Contract selection and sizing against risk budget | deterministic |
| AI Review | Bull / Bear / Judge — may only confirm, abstain, or veto | advisory |
| Risk | 22 checks evaluated in full; sole execution authority | deterministic |
| Authorization | Single-use, hash-bound execution licence | deterministic |
| Execution | Write-ahead intent, broker submission, reconciliation | broker (observed) |
| Replay / Audit | Fingerprint and replay of the deterministic decision | deterministic |

No research-manager, technical-analyst, or sentiment-analyst agents exist in
this system. The dashboard reports absent stages as `NOT BUILT` rather than
implying they ran.

---

## 60-second demo

```bash
pip install -e ".[dev]"
python scripts/run_options_demo.py --replay --dashboard
```

Six scenarios, run offline against synthetic bars:

| Scenario | Demonstrates |
|---|---|
| `breakout` | 3 contracts, $960 max loss inside a $1,000 budget |
| `veto` | AI cancels an approved trade — nothing reaches the broker |
| `no-signal` | S07 declines; the decision is still recorded |
| `illiquid` | spread too wide → no tradeable contract |
| `broke` | one contract costs $6,200 vs a $1,000 budget → rejected |
| `timeout` | broker times out → `UNKNOWN`, not retried, never "filled" |

Inspect one recorded decision:

```bash
cat data/decisions/decisions-*.jsonl | head -1 | python -m json.tool
```

## What the demo proves

| Proof | Evidence |
|---|---|
| The AI cannot originate a trade | schema has no field that could |
| A veto actually stops execution | `veto` scenario — nothing reaches the broker |
| Sizing respects the risk budget | `broke` scenario is rejected at $6,200 vs $1,000 |
| An ambiguous broker outcome is never claimed as a fill | `timeout` scenario ends `UNKNOWN`, not retried |
| Every decision — accepted or not — is recorded | one JSONL line per decision, `python -m json.tool` readable |
| The deterministic decision is reproducible | `--replay` re-derives it from the stored snapshot alone |

---

## Options intelligence

Long calls on a bullish signal, long puts on a bearish one — never a short
option, so risk stays defined in both directions.

```
equities:  shares    = risk_money / stop_distance     ← a price distance
options:   contracts = risk_budget / (ask × 100)       ← the entire amount at risk
```

Contracts are selected deterministically by moneyness and days-to-expiry, and
priced at the **ask**, never the mid. Max loss is shown all-in — premium plus
documented fees — and labelled `EXACT`. Max profit for a long option is
unbounded and is deliberately never printed as a number.

---

## AI governance

```
CONFIRM   the deterministic decision stands
ABSTAIN   no model was consulted, or it produced nothing usable — the
          deterministic decision stands
VETO      the trade is cancelled — the only effect the AI can have
```

**The AI cannot:** change quantity, change strike, loosen a risk limit,
authorize a trade, or hold a broker reference of its own.

**AI failure policy** — every failure mode subtracts, never substitutes a
guess:

| Failure | Result |
|---|---|
| Timeout | ABSTAIN |
| Malformed output | ABSTAIN |
| Refusal / no API key | ABSTAIN |

This is not "fail closed" in the usual sense — the deterministic decision was
already closed before the AI ran. A model outage cannot halt trading, because
it was never the layer trading depended on.

---

## Execution safety

```
Proposal → Authorization Licence → Write-Ahead Intent → Broker → Reconcile
```

The licence is single-use, HMAC-signed with a per-process secret, and bound by
hash to the exact proposal and portfolio — it cannot be replayed or stretched
to cover a different trade. The intent is written to an fsynced,
append-only journal **before** the broker is contacted, so a crash mid-submission
still leaves evidence an order may exist.

**`SUBMITTED` is never `FILLED`.** An ambiguous outcome is recorded as
`UNKNOWN`, and reconciliation — querying the broker directly — resolves it.
Nothing is ever retried before that resolution: a retry into an unresolved
`UNKNOWN` is exactly how a system double-fills.

---

## Reproducibility

```bash
python -m pytest -q                 # 1122 passed, 1 skipped
python scripts/mutation_test.py     # 78 mutants: 76 killed, 0 survived, 2 equivalent
python scripts/run_options_demo.py --replay   # 6/6 decisions reproduced
```

A 16-character fingerprint identifies the **deterministic** decision — market
snapshot, quant output, risk verdict, sizing — and excludes the AI review by
construction. The same market state hashes identically whether the model
confirmed, abstained, timed out, or never ran. `--replay` re-derives every
stored decision from its snapshot alone, with the model absent; a veto is
tracked beside the fingerprint rather than folded into it, since it is the one
thing that does change the outcome. Deterministic replay of an LLM response is
not claimed — a model is not reproducible, which is exactly why nothing it
emits is inside the hash.

---

## Dashboard

```bash
python scripts/build_dashboard.py --live      # real paper account + journal
open data/dashboard.html
```

A single self-contained HTML file — no server, no build step, no network
dependency. It renders:

- the decision funnel and strategy activity, from real stored decisions only
- **"why we did NOT trade"**, attributing each declined decision to the layer that stopped it
- the options opportunity inspector — full quote, sizing, and selection reason
- every deterministic risk check with its observed value
- the order lifecycle, with intent phase shown beside broker state
- replay evidence and the deterministic fingerprint

Every figure comes from a persisted decision record, the Alpaca paper account,
or the runtime's own health object — never estimated or carried over. A broker
that cannot be reached omits the account panel rather than showing stale
numbers.

---

## Validation & security

| Check | Status |
|---|---|
| Tests | **1122 passed**, 1 skipped (Python 3.11 and 3.12), in CI |
| Mutation testing | **78 mutants: 76 killed, 0 survived, 2 declared equivalent**, in CI |
| Safety invariants | paper-only enforced at 3 layers, fail-closed risk config, kill switch — checked in CI |
| Secret scanning | no credential-shaped literal in any tracked file or git history — checked in CI |
| Dashboard | no `<script>`, no external URL, no event-handler attribute — asserted by test and re-checked in CI on the generated page |
| Live verification | real option orders placed through Alpaca's official MCP server on a development paper account |

Paper trading is enforced structurally in three independent places — config,
adapter constructor, and MCP broker — not by a flag alone.

---

## Quantitative heritage

S07 is ported line-by-line, with citations, from a real MT5 strategy pinned at
`docs/reference/SpeedTraderBot_v6.1.mq5`; CI verifies its SHA-256 before the
test suite runs, so an edit to the source fails the build. This is
**specification parity, not runtime-verified parity** — the port has not been
compared against a live MT5 terminal.

---

## Bring your own strategy

A strategy **proposes** a trade. It cannot size one, authorize one, or place
one — it returns a direction, entry, stop, target and score, and holds no
reference to the broker, risk engine, or account.

```bash
python scripts/strategy_tool.py new my_edge     # scaffold a strategy
python scripts/strategy_tool.py check           # validate it
```

`check` runs the strategy rather than just inspecting it, refusing it at load
if it inverts risk/reward, is non-deterministic, mutates the input snapshot, or
raises instead of declining. See the generated template in `strategies/` for
the full contract.

---

## Limitations

- Only **S07** of the original strategy suite is ported; S01–S06 and S08–S14
  do not exist in this repository.
- **Vertical spreads** are not implemented for this submission — a two-leg
  order can leg out, which breaks the exactly-known max-loss guarantee long
  single-leg is chosen for.
- **Historical option-chain backtesting is not claimed.** No historical
  option-chain data exists to price against, so the backtest measures the
  underlying S07 signal only, never options P&L.
- **Greeks are not used as a selection target.** Contract selection is by
  moneyness and days-to-expiry, which are exact and need no pricing model.
- **Memory, reflection, and outcome-analysis layers are not implemented.**
  The decision journal provides the history such layers would read from.
- The dedicated hackathon paper account (`PA3FJIP2GIRB`) currently shows
  **$0.00 with zero fills** — reported on the dashboard, not disguised. Two
  earlier orders were placed on a separate development account during
  integration and never filled.
- Paper trading is a simulation. **No profitability, win-rate, Sharpe, or
  performance claim is made anywhere in this repository.**

---

## Repository layout

```
src/speedtrader/
  quant/        S07, scoring, expected value, cost policy
  options/      contract selection · sizing · fees
  risk/         deterministic engine · measures · state
  execution/    authorization · adapter · MCP · reconciliation
  agents/       veto.py — Bull / Bear / Judge
  llm/          provider abstraction
  app/          orchestrator, autonomous runtime
strategies/     bring-your-own-strategy plugins
scripts/        run_options_demo · run_live_paper · strategy_tool · mutation_test
docs/           one-pager, engineering log, decision records, MT5 reference
```

---

## Documentation

- [`docs/ONE_PAGER.md`](docs/ONE_PAGER.md) — AI logic, risk gates, Alpaca infrastructure
- [`docs/SUBMISSION.md`](docs/SUBMISSION.md) — full engineering log: setup, live paper trading, what was rejected and why
- [`docs/decisions/0001-transaction-cost-research.md`](docs/decisions/0001-transaction-cost-research.md) — cost-model research

---

## Verification

```bash
pip install -e ".[dev]"
python scripts/run_options_demo.py --replay --dashboard
python -m pytest -q
python scripts/mutation_test.py
```

## License

[MIT](LICENSE)

---

*Paper trading only. Nothing here is investment advice, and no claim is made
that any strategy is profitable.*

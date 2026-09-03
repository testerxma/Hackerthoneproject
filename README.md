# SpeedTrader AI

**An options trading agent where the AI can veto a trade but can never cause one.**

Alpaca AI Trading Agents Hackathon · Paper trading only · 732 tests

```bash
pip install -e ".[dev]"
python scripts/run_options_demo.py
```

No API key. No network. No configuration. That runs the complete decision cycle.

---

## The idea

Most AI trading agents work like this:

```
LLM proposes a trade  ──►  guardrails try to catch bad ones  ──►  broker
```

The guardrails are the last line of defence, and the LLM's mistakes are the
thing they have to catch. SpeedTrader inverts it:

```
S07 (ported quant strategy)
   ──►  deterministic risk engine        decides IF and HOW MUCH
   ──►  options sizing                   converts risk budget to contracts
   ──►  AI adversarial review            may only CONFIRM · ABSTAIN · VETO
   ──►  single-use execution licence
   ──►  Alpaca (paper) via its MCP server
```

By the time the model is consulted, the trade already exists and is already
approved. **The AI's entire vocabulary is three words**, and none of them can
enlarge a position, loosen a limit, change a strike, or authorize anything.

This is not enforced by telling the model to behave — a prompt is a request, and
prompt injection is real. It is enforced by the **schema**: the review object has
four fields (`verdict`, `confidence`, `reasoning`, `concerns`) and
`additionalProperties: false`. A model returning `{"verdict": "CONFIRM",
"quantity": 99999}` changes nothing, and there is a test that proves exactly
that against the real pipeline.

The practical consequence: **model capability is a reasoning decision, not a
safety one.** Swap in the strongest model available and its maximum authority is
still to cancel one trade.

---

## The failure mode is inverted, deliberately

Because this layer can only *subtract*, "fail closed" here means **change
nothing** — not "block everything".

| What happens | What the system does |
|---|---|
| Model times out | ABSTAIN · the deterministic decision stands |
| Model returns malformed JSON | ABSTAIN · never guessed at |
| Model refuses | ABSTAIN |
| No API key at all | ABSTAIN · honest "no model was consulted" |
| Model says VETO with a reason | **Trade cancelled** |

If an LLM outage vetoed instead, a vendor going down would silently halt all
trading. Mutation testing confirms it: making failures veto breaks 22 tests.

---

## Why long single-leg options

Chosen for correctness, not convenience.

> **Maximum loss = the premium paid, known exactly at entry, under every price path.**

An equity stop-loss is an *intention* — price can gap through it overnight. A
long option cannot lose more than its debit. So the risk engine sizes against an
**exact** maximum loss rather than an assumed one:

```
equities:  shares    = risk_money / stop_distance      ← a price distance
options:   contracts = risk_budget / (ask × 100)       ← the entire amount at risk
```

These are not the same formula with different names, and the options path never
reuses the equity one. Priced at the **ask**, never the mid — the mid understates
max loss by half the spread on every position.

A bearish signal buys a **long put**, never a short call: risk stays defined in
both directions.

---

## What a judge can verify in 60 seconds

```bash
python scripts/run_options_demo.py           # all six scenarios
python -m pytest -q                          # 732 passed
```

| Scenario | Demonstrates |
|---|---|
| `breakout` | 3 contracts, **$960 max loss** inside a $1,000 budget |
| `veto` | AI cancels an approved trade — nothing reaches the broker |
| `no-signal` | S07 declines; the decision is still recorded |
| `illiquid` | spread too wide → no tradeable contract |
| `broke` | one contract costs $6,200 vs a $1,000 budget → rejected |
| `timeout` | broker times out → **UNKNOWN**, not retried, never "filled" |

Then read one decision:

```bash
cat data/decisions/decisions-*.jsonl | head -1 | python -m json.tool
```

Every record carries the snapshot, the signal, **the cost assumptions behind its
EV**, all 22 deterministic checks, the contract chosen *and what was rejected and
why*, the AI review with the model that produced it, and the execution outcome.

---

## Safety properties, and how each is proven

| Property | Enforcement | Proof |
|---|---|---|
| AI cannot enlarge a trade | schema has no such field | hostile-model test, end to end |
| AI outage cannot halt trading | failures ABSTAIN | mutation: veto-on-failure breaks 22 tests |
| No order without a licence | required positional arg | mutation: skipping it breaks 29 tests |
| A licence works once | single-use nonce | 16-thread race admits exactly one winner |
| Approve small, submit large | proposal bound by hash | any economic field change blocks |
| Portfolio changed since approval | portfolio bound by hash | blocked before the broker |
| Licence never persisted | not serialisable; repr redacts | nonce asserted absent from JSONL |
| SUBMITTED ≠ FILLED | no FILLED state in the adapter | timeout → UNKNOWN, not retried |
| Retry cannot double-fill | idempotency key = the nonce | reconciliation refuses unsafe retries |
| Live trading | refused structurally in the constructor | CI asserts `environment: paper` |

Critical logic is **mutation-tested**: the guard is deliberately broken and the
tests must fail. 34 mutants across the risk engine, options domain, cost
provenance, authorization, execution adapter, reconciliation and the veto
layer — all killed.

---

## Quantitative heritage

S07 is ported from `docs/reference/SpeedTraderBot_v6.1.mq5` (a real MT5 strategy),
line-by-line with citations:

```python
HISTORY_GUARD_SHIFT = 22    # Tm(i,22)==0                  (L1037)
CANDLE_ATR_MULT     = 1.5   # candle > 1.5*st.atr          (L1042/L1044)
TARGET_ATR_MULT     = 3.0   # tp = price +/- 3.0*st.atr    (L1043/L1045)
```

The reference is **read-only and cryptographically pinned** — CI verifies its
SHA-256 before the test suite, so an edit fails the build rather than silently
invalidating every `L####` citation. Options were added as a *translation layer*;
S07's formula was not modified to suit them.

**Parity is stated honestly.** This is specification parity, not runtime-verified
parity: MT5's ADX/DI uses normalisation and smoothing that has not been compared
against a live terminal. That is documented rather than claimed.

---

## Live Alpaca

```bash
cp .env.example .env        # add ALPACA_API_KEY / ALPACA_SECRET_KEY
pip install alpaca-mcp-server mcp anthropic
```

Orders reach Alpaca through its **official MCP server** (`place_option_order`).
The MCP server is a *transport beneath the safety gate*, not an agent's hand —
nothing else in the system holds a broker reference, so no LLM can reach it
without passing every deterministic gate first.

Paper is forced in three independent places: config, the adapter constructor, and
the MCP broker. Credentials are never logged, stored on an object, or included in
an exception.

---

## What is NOT built

Stated plainly, because a README that implies more than exists is the first thing
a reviewer catches:

- **S01–S14** — only **S07** is ported. Inventing the others would violate the
  quant integrity rule that formulas come from the source, not from a model.
- **Vertical spreads** — the structure is pluggable and `VERTICAL_DEBIT` is
  declared, but it is *explicitly refused* rather than silently downgraded.
- **Greeks** — Alpaca exposes them; selection is by moneyness and DTE, which are
  exact and need no pricing model. No delta target is claimed.
- **Memory / reflection / outcome analysis** — the decision journal provides the
  history these would read from; the layers themselves are not built.
- **P&L** — the demo is synthetic and paper trading is a simulation. No
  performance claim is made anywhere in this repository.

---

## Layout

```
src/speedtrader/
  quant/        S07, scoring, expected value, cost policy   ← ported, cited
  options/      contract selection · sizing · fees          ← broker-agnostic, offline
  risk/         deterministic engine · measures · state     ← the authority
  execution/    authorization · adapter · MCP · reconciliation
  agents/       veto.py — Bull / Bear / Judge
  llm/          provider abstraction (no vendor SDK above providers/)
  app/          options_orchestrator.py — coordination only
docs/decisions/ research and decision records
```

Provider abstraction is real: `llm/providers/base.py` imports no vendor SDK, so
OpenAI / DeepSeek / OpenRouter / local Qwen are one adapter file away with no
change to agent or risk code.

---

*Paper trading only. Nothing here is investment advice, and no claim is made that
any strategy is profitable.*

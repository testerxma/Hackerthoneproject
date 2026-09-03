# SpeedTrader AI — one-page write-up

**Alpaca AI Trading Agents Hackathon** · options · **paper trading only** ·
account `PA3FJIP2GIRB` · 1122 tests · 78 mutants

> **AI can challenge the trade. Determinism authorizes it.**

---

## 1. AI logic

Most agents have an LLM **propose** trades with guardrails behind it.
SpeedTrader inverts it: **the AI can veto a trade but can never cause one.**

By the time a model is consulted the trade already exists and has already been
approved — produced by S07 (a quant strategy ported line-by-line from a real
MT5 bot), gated by the deterministic risk engine, sized by the options risk
model. Three agents then review it: **Bull** argues for, **Bear** hunts failure
modes, **Judge** answers one question — *is there a disqualifying reason not to
take a trade the deterministic system already approved?*

Their entire vocabulary is **CONFIRM · ABSTAIN · VETO**, enforced by JSON
schema (`additionalProperties: false`, four fields) rather than by prompt
instruction — a prompt is a request, and prompt injection is real. A hostile
model returning `{"verdict":"CONFIRM","quantity":99999}` changes nothing,
proven end-to-end by test.

Because the layer can only subtract, its failure mode inverts: a timeout,
malformed JSON, a refusal or a missing API key all **ABSTAIN**, and the
deterministic decision stands. Vetoing on error would turn a vendor outage into
a trading outage.

## 2. Risk gates

The deterministic engine is the sole execution authority. It evaluates **all 22
checks** rather than short-circuiting, so the audit trail is complete: account
halts, score, EV, duplicates, correlation, spread, gap, TTL, portfolio heat,
symbol and sector exposure.

**Options sizing never reuses equity mathematics:**

```
equities:  shares    = risk_money / stop_distance     ← a price distance
options:   contracts = risk_budget / (ask × 100)      ← the entire amount at risk
```

Long single-leg was chosen for *correctness*: maximum loss is the premium paid,
known exactly at entry under every price path. Priced at the **ask**, never the
mid — the mid is not a price anyone will sell to you at, and sizing against it
would make the max-loss figure untrue.

## 3. Alpaca infrastructure

Orders reach Alpaca through its **official MCP server** (`place_option_order`),
used as a *transport beneath the safety gate* rather than an agent's hand —
nothing else holds a broker reference, so no LLM can reach it without passing
every gate. Options discovery and quotes come from the Trading and Market Data
APIs; fees from the published schedule. **Paper is forced in three independent
places; live is refused structurally.**

Running against the real API found three defects no fixture could: Alpaca's
100-symbol quote cap, a two-level MCP envelope that made a *successful*
submission read `UNKNOWN`, and a bar window computed in calendar rather than
trading hours. All fixed and pinned by mutants.

## 4. Deterministic safety

An `ExecutionAuthorization` is a single-use HMAC licence, per-process secret,
bound by hash to the exact proposal and portfolio, non-serialisable. No licence
→ no order, with no code path around it. **SUBMITTED is never FILLED**; a
timeout is UNKNOWN and resolved by reconciliation, never by a retry that could
double-fill. Execution intent is written to an **fsynced write-ahead journal
before the broker is contacted**, so a crash mid-submission still leaves
evidence an order may exist; on restart the runtime refuses to trade while any
intent is unresolved.

## 5. Reproducibility and auditability

Every decision — accepted or not — is appended to JSONL with the snapshot,
signal, cost assumptions, all 22 checks, the contract chosen *and the
alternatives rejected*, the AI review, and the state it ended in with the
reason — `UNKNOWN` included, where an order may exist and reconciliation, not
a retry, resolves it.

A 16-character fingerprint identifies the deterministic decision and
**excludes the AI review by construction**, so the same market state hashes
identically whether the model confirmed, abstained or never ran. `--replay`
re-derives every stored decision from its snapshot with the model absent.
**Deterministic replay of an LLM response is not claimed** — a model is not
reproducible, which is exactly why nothing it emits is inside the hash.

Every guard is mutation-tested: **78 mutants, 76 killed, 0 survived, 2 declared
equivalent** with the argument for why.

## 6. Limitations, stated plainly

- **P&L is $0.00 with zero fills.** Reported on the dashboard, not disguised.
  No win rate, Sharpe, alpha or profitability is computed anywhere — they are
  undefined without resolved trades.
- Specification parity with the MT5 source, **not** runtime-verified parity.
  Only S07 of S01–S14 is ported.
- **Vertical spreads are declared and explicitly refused**: a two-leg order can
  leg out, so max loss is not exactly known at submission — the one property
  long single-leg was chosen for.
- No news, sentiment or fundamental analyst exists, and none is rendered.
- The backtest measures the underlying signal, never options P&L, because no
  historical option-chain data exists to price against.
- Paper trading is a simulation. **No performance claim is made anywhere.**

```bash
pip install -e ".[dev]" && python scripts/run_options_demo.py --replay --dashboard
```

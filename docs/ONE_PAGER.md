# SpeedTrader AI — one-page technical summary

**Alpaca AI Trading Agents Hackathon** · options · paper trading only · 1116 tests

---

## AI logic

Most agents have an **LLM propose trades** with guardrails behind it. SpeedTrader
inverts the relationship: **the AI can veto a trade but can never cause one.**

By the time a model is consulted, the trade already exists and has already been
approved — produced by S07 (a quant strategy ported from MT5), gated by the
deterministic risk engine, and sized by the options risk model. Three agents then
review it:

- **Bull** — argues the strongest defensible case *for* the trade
- **Bear** — hunts the failure conditions
- **Judge** — answers one question: *is there a disqualifying reason not to take a
  trade the deterministic system already approved?*

Bull exists to keep Bear honest; a lone critic always finds something, and a veto
that fires every time is the same as having no AI.

The Judge's entire vocabulary is **CONFIRM · ABSTAIN · VETO**. None of them can
enlarge a position, loosen a limit, change a strike, or authorize anything —
enforced by the *schema*, not by prompt instructions (a prompt is a request, and
prompt injection is real). The review object has four fields and
`additionalProperties: false`. A hostile model returning `{"verdict":"CONFIRM",
"quantity":99999}` changes nothing, proven end-to-end by test.

**Consequence:** model capability is a reasoning decision, not a safety one.

Because the layer can only subtract, its failure mode is inverted: a timeout,
malformed JSON, a refusal, or no API key all **ABSTAIN** — the deterministic
decision stands. Vetoing on error would turn an LLM vendor outage into a trading
outage. Market context is fenced as `<untrusted>`; since the schema cannot express
an escalation, a successful injection still cannot exceed a veto — a
denial-of-service risk, not a financial one.

Provider-abstracted: nothing above `llm/providers/base.py` imports a vendor SDK.

---

## Risk gates

The deterministic risk engine is the authority and is **ported from the original
MT5 bot**, evaluating all 22 checks rather than short-circuiting, so the audit
trail is complete: account halts, score, EV, duplicates, correlation, spread, gap,
TTL, portfolio heat, symbol and sector exposure.

**Options sizing never reuses equity mathematics:**

```
equities:  shares    = risk_money / stop_distance     ← a price distance
options:   contracts = risk_budget / (ask × 100)      ← the entire amount at risk
```

Long single-leg was chosen for *correctness*: maximum loss is the premium paid,
known exactly at entry under every price path. An equity stop is an intention that
price can gap through; a long option cannot lose more than its debit — so the
engine sizes against an **exact** maximum loss. Priced at the ask, never the mid.
A bearish signal buys a long put, never a short call.

The engine decides **whether to trade and at what fraction of risk**; the options
layer only converts that authorized budget into contracts and can never widen it.

**Execution boundary.** A `RiskGateResult` explains and is persisted; an
`ExecutionAuthorization` licenses and is not. The licence is single-use
(HMAC-signed, per-process secret, nonce), bound by hash to the exact proposal and
portfolio, non-serialisable, and redacted in `repr`. No licence → no order, with
no code path around it. `SUBMITTED` is never `FILLED`; a timeout is `UNKNOWN` and
resolved by reconciliation against the broker, never by a retry that could
double-fill — the idempotency key is the same single-use nonce.

Every guard is **mutation-tested by a harness that runs in CI**
(`scripts/mutation_test.py`, 78 mutants: 76 killed, 0 survived, 2 declared
equivalent with the argument for why). Each mutant is a reviewable edit to real
source. The harness fails if a declared equivalent is ever killed, or if a
mutant's anchor disappears in a refactor — so the score cannot be padded. It
found a real gap on its first run: a depth-bound test that was shallower than
Python's recursion limit.

---

## Alpaca infrastructure

- **Trading API via Alpaca's official MCP server** (`place_option_order`), used as
  a *transport beneath the safety gate* rather than an agent's hand. Nothing else
  holds a broker reference, so no LLM can reach it without passing every gate.
- **Options data** — `GetOptionContractsRequest` for discovery (strike, expiry,
  open interest) and latest quotes for the two-sided market. Alpaca returns several
  risk-critical numbers as *strings*; each is converted explicitly, and anything
  unconvertible **drops the contract** rather than defaulting it — a silent default
  would size a position against a maximum loss that does not exist.
- **Fees from the published schedule** (revised 2026-09-01, read from the PDF):
  per-contract TAF/CAT/ORF/OCC plus notional SEC. Equity per-share fees are never
  reused — ORF and OCC have no equity equivalent. Index options are **refused**,
  not silently mispriced, because the schedule adds $0.50/contract this model does
  not apply.
- **Paper is forced in three independent places**; live is refused structurally.
- **Verified live, not only against mocks** — `scripts/run_live_paper.py` runs the
  same pipeline on Alpaca's bars, chain, quotes and balances, and has placed real
  orders (`SOFI261002C00017000`, `F261002C00013500`). It sends nothing without
  `--submit`, and refuses `--submit` with `--allow-stale`. Doing this found three
  bugs mocks could not: the 100-symbol quote cap; a two-level MCP envelope that
  made a *successful* submission read `UNKNOWN`; and a bar window computed in
  calendar rather than trading hours, which returned 660 of the 891 bars an
  EMA200 needs and so refused **every** hourly symbol — failing closed, and
  unable to fail any other way. The safety chain held throughout: the adapter
  refused to claim a fill it could not see, reconciliation found the order
  `ACCEPTED`, and the retry was refused. An ambiguous success stayed one order.

---

## Bring your own strategy

S07 is a worked example; the product is everything below it. A trader drops one
file into `strategies/`, runs `strategy_tool.py check`, and inherits all 22 risk
checks, options sizing, the execution licence, reconciliation and the audit
trail — while the contract keeps the boundary absolute: **a strategy proposes a
trade; it cannot size, authorize or place one.**

Validation *runs* the strategy, because every failure that matters is silent: a
wrong-sided stop (EV computed from a reward that is really a loss), a
non-deterministic `evaluate` (unreplayable), a mutated snapshot, a duplicate id.
Each file's SHA-256 travels onto every decision. Seven mutants prove the guards.
Stated plainly: loading a file executes it, so this enforces the contract, not
safety from hostile code — but no strategy can reach the broker regardless.

---

## Auditability — the differentiator

Every decision — accepted or not — is appended to JSONL carrying the snapshot, the
signal, **the cost assumptions behind its EV**, all deterministic checks, the
contract chosen *with the alternatives that were rejected and why*, the AI review
with the model and prompt version, and the execution outcome.

**Decisions are reproducible.** A 16-char fingerprint identifies what the
deterministic system decided and *excludes the AI review by construction*, so the
same market state hashes identically whether the model confirmed, abstained,
timed out or never ran — the central claim reduced to comparing two strings, and
asserted end-to-end by test. `--replay` re-derives every stored decision from its
snapshot with the model absent; a divergence names the field that moved. A veto
is tracked *beside* the fingerprint, never inside it, because it is the one thing
the AI can change.

A generated command centre renders each decision as three lanes — AI advisory,
deterministic authority, execution outcome — plus **"why we did NOT trade"**,
attributing every declined decision to the layer that stopped it.

Walk-forward backtesting with cost sensitivity covers the underlying signal;
options P&L is deliberately not simulated, because it would require a pricing
model and would measure that model rather than the strategy.

The MT5 source is cryptographically pinned in CI so an edit fails the build
rather than silently invalidating the `L####` citations throughout the quant
modules.

## Stated honestly

Specification parity with MT5, **not** runtime-verified parity (ADX/DI
normalisation is undocumented and untested against a live terminal). Only S07 of
S01–S14 is ported. Vertical spreads are declared and explicitly refused: a
two-leg order can leg out, so max loss is not exactly known at submission — the
one property long single-leg was chosen for. Greeks and memory are not built. The backtest measures the
underlying signal, never options P&L. **No P&L claim is made anywhere** — paper
trading is a simulation.

```bash
pip install -e ".[dev]" && python scripts/run_options_demo.py --replay --dashboard
```

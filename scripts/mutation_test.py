#!/usr/bin/env python3
"""
SpeedTrader AI — mutation testing harness

--------------------------------------------------------------------------------
WHY THIS EXISTS
--------------------------------------------------------------------------------
A passing test suite proves the tests pass. It does not prove they would NOTICE
if a safety guard were removed. For a system whose entire claim is "the
deterministic layer holds", that distinction is the whole argument.

So every guard that matters is deliberately broken here, one at a time, and the
suite must go red. A mutant that SURVIVES is a guard nothing actually tests —
the most useful finding this repository can produce about itself.

Each mutant below is a precise, reviewable edit to real source: the exact string
removed and the exact string put in its place. Nothing is generated, so a
reviewer can read what was broken and judge whether breaking it should matter.

    python scripts/mutation_test.py              # run them all
    python scripts/mutation_test.py --list       # just show what would be broken
    python scripts/mutation_test.py -k licence   # a subset, matched on id or note

--------------------------------------------------------------------------------
SAFETY
--------------------------------------------------------------------------------
Source is restored in a `finally`, and again on SIGINT/SIGTERM, from an in-memory
copy taken before the first edit. The run refuses to start if the working tree
has uncommitted changes to a file it intends to mutate, so an interrupted run can
never be confused with your own work. Exit status is non-zero if any mutant
survives.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "speedtrader"
TESTS = ROOT / "tests"


@dataclass(frozen=True)
class Mutant:
    """One deliberate defect and the tests that must catch it."""
    id: str
    path: str                 # relative to src/speedtrader
    old: str
    new: str
    note: str                 # what safety property this breaks
    tests: tuple[str, ...] = field(default=())   # relative to tests/
    #: Set when the mutation provably cannot change behaviour, with the
    #: argument for why. Such a mutant is EXPECTED to survive, and is
    #: reported as an error if it gets killed — that would mean the
    #: reasoning below is wrong. Declaring these is how the score stays
    #: honest instead of being padded to 100%.
    equivalent: str = ""


# ==========================================================================
# The execution licence — no order without one, and it works exactly once
# ==========================================================================
AUTH = "execution/authorization.py"
ADAPTER = "execution/options_adapter.py"
_AUTH_TESTS = ("unit/execution", "integration/pipeline")

MUTANTS: list[Mutant] = [
    Mutant("auth-unsigned", AUTH,
           "if not hmac.compare_digest(expected, auth._signature):",
           "if False:",
           "a forged signature is accepted", _AUTH_TESTS),
    Mutant("auth-expiry", AUTH,
           "if (now or _utcnow()) >= auth.expires_at:",
           "if False:",
           "an expired licence still authorises", _AUTH_TESTS),
    Mutant("auth-proposal-binding", AUTH,
           "if canonical_hash(proposal) != auth.proposal_hash:",
           "if False:",
           "approve small, submit large", _AUTH_TESTS),
    Mutant("auth-portfolio-binding", AUTH,
           "if canonical_hash(portfolio) != auth.portfolio_hash:",
           "if False:",
           "the book may change between approval and submission", _AUTH_TESTS),
    Mutant("auth-replay", AUTH,
           "        registry.consume(auth.nonce)",
           "        pass",
           "one licence authorises unlimited orders", _AUTH_TESTS),
    Mutant("auth-nonce-reusable", AUTH,
           "            if nonce in self._used:",
           "            if False:",
           "the registry forgets a burned nonce", _AUTH_TESTS),
    Mutant("auth-any-object", AUTH,
           "if not isinstance(auth, ExecutionAuthorization):",
           "if False:",
           "a look-alike object passes as a licence", _AUTH_TESTS),
    Mutant("auth-none-accepted", AUTH,
           "if auth is None:",
           "if False:",
           "no licence at all is accepted", _AUTH_TESTS,
           equivalent="the isinstance check on the next line already refuses "
                      "None with the same exception type; this branch exists "
                      "only to give a clearer message, so removing it cannot "
                      "let an order through (auth-any-object covers the "
                      "property itself)"),
    Mutant("auth-serialisable", AUTH,
           '    def __reduce__(self):\n        raise TypeError(',
           '    def __reduce__(self):\n        return (dict, ({},)) or TypeError(',
           "the licence can be pickled out of the process", _AUTH_TESTS),

    # ---------------------------------------------------------------- adapter
    Mutant("adapter-skips-verify", ADAPTER,
           "            verify(\n                authorization,",
           "            _ = (\n                authorization,",
           "an order is submitted without checking the licence", _AUTH_TESTS),
    Mutant("adapter-quantity-drift", ADAPTER,
           "if request.quantity != authorization.approved_quantity:",
           "if False:",
           "submitted size may exceed the approved size", _AUTH_TESTS),
    Mutant("adapter-nonpositive-qty", ADAPTER,
           "if request.quantity <= 0:",
           "if False:",
           "a zero or negative order reaches the broker", _AUTH_TESTS,
           equivalent="unreachable through the only path that exists: "
                      "authorize() refuses to mint for a non-positive "
                      "quantity, and the line above requires the request "
                      "quantity to equal the approved one. Kept as defence in "
                      "depth against a future change to authorize()"),
    Mutant("adapter-idempotency-key", ADAPTER,
           'client_order_id = f"st-{authorization.nonce}"',
           'client_order_id = "st-fixed"',
           "a retry no longer collides at the broker, so it double-fills",
           _AUTH_TESTS),

    # ==================================================================
    # Ambiguity handling — SUBMITTED is not FILLED, UNKNOWN is not FAILED
    # ==================================================================
    Mutant("retry-anything", "execution/reconciliation.py",
           "return self.state in (ReconciledState.NOT_FOUND, ReconciledState.REJECTED)",
           "return True",
           "an order that may exist is retried anyway", _AUTH_TESTS),
    Mutant("retry-on-open", "execution/reconciliation.py",
           "TERMINAL = frozenset({",
           "TERMINAL = frozenset({\n    ReconciledState.OPEN, ReconciledState.NEEDS_HUMAN,",
           "a live or ambiguous order is treated as finished", _AUTH_TESTS),
    Mutant("partial-fill-invisible", "execution/reconciliation.py",
           "POSITION_EXISTS = frozenset({\n    ReconciledState.FILLED, ReconciledState.PARTIALLY_FILLED,",
           "POSITION_EXISTS = frozenset({\n    ReconciledState.FILLED,",
           "a partial fill reads as no position", _AUTH_TESTS),
    Mutant("ambiguity-becomes-rejection", "execution/mcp_broker.py",
           'return BrokerTimeout(f"ambiguous broker failure, outcome unknown: {message}")',
           "return BrokerRejected(message)",
           "an ambiguous failure is called a definite refusal — the double-fill path",
           ("unit/execution",)),
    Mutant("envelope-unbounded", "execution/mcp_broker.py",
           "if depth > 5 or not isinstance(payload, Mapping):",
           "if not isinstance(payload, Mapping):",
           "a malformed envelope can spin instead of terminating",
           ("unit/execution",)),
    Mutant("envelope-overshoots-order", "execution/mcp_broker.py",
           '    if payload.get("id") or payload.get("order_id"):\n        return payload',
           "    if False:\n        return payload",
           "an order carrying its own 'data' field is mistaken for an envelope",
           ("unit/execution",)),
    Mutant("live-trading-allowed", "execution/mcp_broker.py",
           'if not paper:\n            raise ValueError("AlpacaMCPBroker is paper-only")',
           "if False:\n            raise ValueError(\"AlpacaMCPBroker is paper-only\")",
           "live trading is no longer refused structurally",
           ("unit/execution",)),

    # ==================================================================
    # Options sizing — max loss is the premium, and it is exact
    # ==================================================================
    Mutant("size-rounds-up", "options/risk.py",
           "qty = math.floor(risk_budget / max_loss_per_contract)",
           "qty = math.ceil(risk_budget / max_loss_per_contract)",
           "rounding up turns a 1% risk rule into a larger loss",
           ("unit/options", "integration/pipeline")),
    Mutant("size-forces-one", "options/risk.py",
           "    if qty < 1:\n        # One contract already exceeds",
           "    if False:\n        # One contract already exceeds",
           "an unaffordable contract is bought anyway",
           ("unit/options", "integration/pipeline")),
    Mutant("size-prices-at-mid", "options/risk.py",
           "max_loss_per_contract = ask * contract.multiplier",
           "max_loss_per_contract = contract.quote.mid * contract.multiplier",
           "sizing against the mid understates the cost of actually buying",
           ("unit/options", "integration/pipeline")),
    Mutant("size-ignores-concentration", "options/risk.py",
           "    if affordable < qty:",
           "    if False:",
           "the whole-book premium cap stops applying",
           ("unit/options", "integration/pipeline")),
    Mutant("size-no-quote", "options/risk.py",
           "    if contract.quote is None:",
           "    if False:",
           "a contract with no quote is sized against an unknown max loss",
           ("unit/options", "integration/pipeline")),
    Mutant("select-below-min-ask", "options/contracts.py",
           "if c.quote.ask < policy.min_ask:",
           "if False:",
           "a near-worthless contract becomes selectable",
           ("unit/options",)),
    Mutant("select-crossed-book", "options/contracts.py",
           "if c.quote.ask < c.quote.bid:",
           "if False:",
           "a crossed book is treated as a real market",
           ("unit/options",)),

    # ==================================================================
    # Options data mapping — a wrong multiplier is a wrong max loss
    # ==================================================================
    Mutant("quote-one-sided", "alpaca/options_data.py",
           "    if bid <= 0 or ask <= 0:",
           "    if False:",
           "a one-sided book prices a trade",
           ("unit/test_options_data_mapping.py",)),
    Mutant("size-defaults-to-100", "alpaca/options_data.py",
           "    if size is None or size <= 0:\n        return None",
           "    if size is None or size <= 0:\n        size = STANDARD_CONTRACT_MULTIPLIER",
           "an adjusted contract is assumed to deliver 100 shares",
           ("unit/test_options_data_mapping.py",)),
    Mutant("lookback-calendar-units", "alpaca/market_data.py",
           "INTRADAY_CALENDAR_FACTOR = 6.0",
           "INTRADAY_CALENDAR_FACTOR = 3.0",
           "the bar window is priced in calendar time, so an EMA200 never "
           "converges and every hourly symbol is refused",
           ("unit/test_alpaca_layer.py",)),
    Mutant("quote-batch-uncapped", "alpaca/options_data.py",
           "QUOTE_BATCH_LIMIT = 100",
           "QUOTE_BATCH_LIMIT = 500",
           "the request exceeds Alpaca's documented symbol cap",
           ("unit/test_options_data_mapping.py",)),
    Mutant("partial-chain-tolerated", "alpaca/options_data.py",
           "                except Exception as e:\n                    # One failed batch",
           "                except Exception:  # noqa: BLE001\n                    continue\n                except BaseException as e:\n                    # One failed batch",
           "a half-priced chain silently narrows selection",
           ("unit/test_options_data_mapping.py",)),

    # ==================================================================
    # Bring your own strategy: the contract is the whole safety story
    # ==================================================================
    Mutant("plugin-wrong-sided-stop", "quant/strategies/plugins.py",
           "        if output.stop_loss >= output.entry:",
           "        if False:",
           "a BUY stop above entry is admitted, inverting risk/reward so EV is "
           "computed from a reward that is really a loss",
           ("unit/quant",)),
    Mutant("plugin-nondeterminism-allowed", "quant/strategies/plugins.py",
           "    if len(identities) > 1:",
           "    if False:",
           "a strategy that cannot be replayed is admitted",
           ("unit/quant",)),
    Mutant("plugin-determinism-single-trial", "quant/strategies/plugins.py",
           "DETERMINISM_TRIALS = 3",
           "DETERMINISM_TRIALS = 1",
           "one trial cannot observe non-determinism at all",
           ("unit/quant",)),
    Mutant("plugin-snapshot-mutation-allowed", "quant/strategies/plugins.py",
           "    if before != after:",
           "    if False:",
           "a strategy may edit the snapshot every other layer reads",
           ("unit/quant",)),
    Mutant("plugin-duplicate-ids-allowed", "quant/strategies/plugins.py",
           "        if item.id in ids:",
           "        if False:",
           "two strategies share an id and their journals merge silently",
           ("unit/quant",)),
    Mutant("plugin-unstable-order", "quant/strategies/plugins.py",
           "    for path in sorted(directory.glob(\"*.py\")):",
           "    for path in directory.glob(\"*.py\"):",
           "load order becomes filesystem-dependent, so one market state can "
           "resolve differently on two machines",
           ("unit/quant",)),
    Mutant("plugin-validation-skipped", "quant/strategies/plugins.py",
           "        if validate:\n            problems = validate_strategy(strategy)",
           "        if False:\n            problems = validate_strategy(strategy)",
           "strategies load without their contract being checked at all",
           ("unit/quant",)),

    # ==================================================================
    # Options presentation: the max-loss figure must stay true
    # ==================================================================
    Mutant("option-maxloss-excludes-fees", "replay/inspector.py",
           "    max_loss_all_in = (max_loss + fee_total",
           "    max_loss_all_in = (max_loss + 0 * fee_total",
           "the headline max loss understates what the position can actually lose",
           ("unit/replay",)),
    Mutant("option-priced-at-mid", "replay/inspector.py",
           '        "priced_at": "ask",',
           '        "priced_at": "mid",',
           "the page claims sizing used the mid, which would make max loss untrue",
           ("unit/replay",)),
    Mutant("option-quantifies-max-profit", "replay/inspector.py",
           '        "max_profit": ("unbounded for a long call — deliberately not quantified, "',
           '        "max_profit": ("999999 "',
           "a large profit figure is printed beside an exactly known max loss",
           ("unit/replay",)),
    Mutant("option-dte-from-today", "replay/inspector.py",
           '    dte = _days_to_expiry(contract.get("expiration"),\n                          _get(record, "snapshot", "timestamp"))',
           '    dte = _days_to_expiry(contract.get("expiration"), None)',
           "DTE is measured from now, not from the snapshot the decision faced",
           ("unit/replay",)),

    # ==================================================================
    # The decision inspector: authority separation is the whole argument
    # ==================================================================
    Mutant("inspector-ai-is-deterministic", "replay/inspector.py",
           '        add("ai_review", "AI review (veto layer)", Authority.ADVISORY,\n            StageState.BLOCKED if vetoed else StageState.PASSED,',
           '        add("ai_review", "AI review (veto layer)", Authority.DETERMINISTIC,\n            StageState.BLOCKED if vetoed else StageState.PASSED,',
           "an AI opinion is presented to a judge as deterministic authority",
           ("unit/replay",)),
    Mutant("inspector-risk-is-advisory", "replay/inspector.py",
           '        add("risk", "Deterministic risk engine", Authority.DETERMINISTIC,\n            StageState.BLOCKED if blocked else StageState.PASSED,',
           '        add("risk", "Deterministic risk engine", Authority.ADVISORY,\n            StageState.BLOCKED if blocked else StageState.PASSED,',
           "the only source of execution authority is shown as merely advisory",
           ("unit/replay",)),
    Mutant("inspector-unreached-shown-as-passed", "replay/inspector.py",
           "        if stopped and state is not StageState.NOT_BUILT:\n            state = StageState.NOT_REACHED",
           "        if False:\n            state = StageState.NOT_REACHED",
           "stages that never ran are rendered as having passed",
           ("unit/replay",)),
    Mutant("inspector-hides-the-blocking-stage", "replay/inspector.py",
           "        if state is StageState.BLOCKED:\n            stopped = True",
           "        if False:\n            stopped = True",
           "the pipeline no longer records where it actually stopped",
           ("unit/replay",)),
    Mutant("inspector-generic-reason-code", "replay/inspector.py",
           '            reason_code=(str(failed[0].get("rule", "")).upper() if failed',
           '            reason_code=("REJECTED" if failed',
           "the exact failing rule is replaced by a vague paraphrase",
           ("unit/replay",)),
    Mutant("inspector-trusts-a-claimed-fingerprint", "replay/inspector.py",
           "        fingerprint = decision_fingerprint(record)",
           '        fingerprint = str(record.get("fingerprint") or decision_fingerprint(record))',
           "a record can assert its own fingerprint instead of it being derived",
           ("unit/replay",)),
    Mutant("inspector-malformed-section-crashes", "replay/inspector.py",
           "    return dict(value) if isinstance(value, Mapping) else {}",
           "    return dict(value)",
           "one malformed field takes down the whole inspection",
           ("unit/replay",)),
    Mutant("evidence-invents-support", "replay/inspector.py",
           '            "supports": bool(check.get("passed")),',
           '            "supports": True,',
           "a failed check is presented as evidence supporting the trade",
           ("unit/replay",)),
    Mutant("evidence-drops-observed-values", "replay/inspector.py",
           '            "observed": check.get("observed"),',
           '            "observed": None,',
           "evidence states a verdict without the value it was based on",
           ("unit/replay",)),
    Mutant("evidence-stale-data-supports", "replay/inspector.py",
           '            "supports": _get(snapshot, "source", "freshness",\n                             default="") == "fresh",',
           '            "supports": True,',
           "stale market data is presented as supporting evidence",
           ("unit/replay",)),

    # ==================================================================
    # Restart safety: the write-ahead journal and crash recovery
    # ==================================================================
    Mutant("wal-not-durable", "execution/intent_journal.py",
           "                os.fsync(fh.fileno())",
           "                pass",
           "the attempt is only in the page cache, so a power loss erases it",
           ("unit/execution",)),
    Mutant("wal-duplicate-guard", "execution/options_adapter.py",
           "if self.journal is not None and self.journal.has_attempt(client_order_id):",
           "if False:",
           "a licence already sent to the broker can be sent again after a restart",
           ("unit/execution", "integration/pipeline")),
    Mutant("wal-attempt-phase-renamed", "execution/intent_journal.py",
           '    ATTEMPTED = "attempted"      # written pre-submission; outcome unknown',
           '    ATTEMPTED = "attempted_after"',
           "the pre-submission phase name changes, breaking recovery's contract",
           ("unit/execution",)),
    Mutant("recovery-assumes-absent", "execution/recovery.py",
           "        return not self.unresolved",
           "        return True",
           "an intent nobody could resolve no longer blocks the next run",
           ("unit/execution", "unit/app")),
    Mutant("recovery-submitted-is-resolved", "execution/intent_journal.py",
           "    IntentPhase.REJECTED, IntentPhase.RECONCILED, IntentPhase.ABANDONED,",
           "    IntentPhase.REJECTED, IntentPhase.RECONCILED, IntentPhase.ABANDONED,\n    IntentPhase.SUBMITTED, IntentPhase.UNKNOWN,",
           "an order that exists but is unfinished counts as settled",
           ("unit/execution", "unit/app")),
    Mutant("wal-corrupt-line-fatal", "execution/intent_journal.py",
           "                    # leaves its intent unresolved — the safe reading.\n                    continue",
           "                    # leaves its intent unresolved — the safe reading.\n                    break",
           "a torn line hides every intent written before it",
           ("unit/execution",)),

    # ==================================================================
    # The autonomous runtime is only as safe as its bounds
    # ==================================================================
    Mutant("runtime-unbounded-orders", "app/runtime.py",
           "        if limits.max_orders is not None and self.health.orders_submitted >= limits.max_orders:",
           "        if False:",
           "the order cap stops applying to an unattended loop",
           ("unit/app",)),
    Mutant("runtime-unbounded-cycles", "app/runtime.py",
           "        if limits.max_cycles is not None and self.health.cycles_completed >= limits.max_cycles:",
           "        if False:",
           "the loop never ends",
           ("unit/app",)),
    Mutant("runtime-ignores-kill-switch", "app/runtime.py",
           "        if self._kill_switch_engaged():",
           "        if False:",
           "an operator cannot stop it without finding the process",
           ("unit/app",)),
    Mutant("runtime-spins-on-errors", "app/runtime.py",
           "        if self.health.consecutive_errors >= limits.max_consecutive_errors:",
           "        if False:",
           "a broken dependency is retried forever at full speed",
           ("unit/app",)),
    Mutant("runtime-unknown-is-free", "app/runtime.py",
           '    return str(getattr(state, "value", state) or "") in {"submitted", "unknown"}',
           '    return str(getattr(state, "value", state) or "") in {"submitted"}',
           "ambiguous submissions stop consuming the order budget, so a string of timeouts can place unlimited real orders",
           ("unit/app",)),
    Mutant("runtime-trades-on-stale-quotes", "app/runtime.py",
           "            if not is_open:",
           "            if False:",
           "options are priced from a quote that will be stale at the open",
           ("unit/app",)),
    Mutant("runtime-starts-unsafe", "app/runtime.py",
           "            if report is not None and not report.safe_to_trade:",
           "            if False:",
           "it trades while a previous run's order is unaccounted for",
           ("unit/app",)),
    Mutant("runtime-observer-can-halt-trading", "app/runtime.py",
           "                        # An observer must never be able to stop trading logic.\n                        pass",
           "                        # An observer must never be able to stop trading logic.\n                        raise",
           "a dashboard error takes the trading loop down with it",
           ("unit/app",)),
    Mutant("runtime-one-symbol-kills-the-cycle", "app/runtime.py",
           '                result.errors.append(f"{symbol}: {type(e).__name__}: {e}")\n                continue',
           "                raise",
           "one bad symbol ends the whole run",
           ("unit/app",)),

    # ==================================================================
    # The AI layer can only subtract, and its failures change nothing
    # ==================================================================
    Mutant("veto-on-failure", "agents/veto.py",
           "    return Review(verdict=Verdict.ABSTAIN, confidence=0.0, reasoning=reason,",
           "    return Review(verdict=Verdict.VETO, confidence=0.0, reasoning=reason,",
           "an LLM outage becomes a trading outage",
           ("unit/agents", "integration/pipeline")),
    Mutant("veto-inverted", "agents/veto.py",
           "        vetoed = judge.verdict is Verdict.VETO",
           "        vetoed = judge.verdict is not Verdict.VETO",
           "the one mechanical effect of the AI layer is reversed",
           ("unit/agents", "integration/pipeline")),
    Mutant("schema-open", "agents/veto.py",
           '"additionalProperties": False',
           '"additionalProperties": True',
           "a model reply may carry fields the schema never declared",
           ("unit/agents", "integration/pipeline")),

    # ==================================================================
    # Reproducibility — the fingerprint must not contain the AI
    # ==================================================================
    Mutant("fingerprint-includes-ai", "replay/fingerprint.py",
           '        "version": FINGERPRINT_VERSION,',
           '        "version": FINGERPRINT_VERSION,\n        "ai": decision.get("ai_review"),',
           "the AI review enters the hash, so the non-interference claim dies",
           ("unit/replay", "integration/pipeline")),

    # ==================================================================
    # Cost provenance — an unexplained number is worse than no number
    # ==================================================================
    Mutant("cost-defaults-silently", "quant/cost_policy.py",
           "    missing = [k for k in keys if k not in mapping or mapping[k] is None]",
           "    missing = []",
           "a missing rate is silently treated as configured",
           ("unit/quant",)),

    # ==================================================================
    # Backtest integrity — the future must not be visible
    # ==================================================================
    Mutant("backtest-look-ahead", "evaluation/backtest.py",
           "        window = bars[:i]                      # strictly the past",
           "        window = bars[:i + 1]                  # strictly the past",
           "the strategy sees the bar it is about to trade",
           ("unit/evaluation",)),
    Mutant("backtest-entry-bar-resolves", "evaluation/backtest.py",
           "    for offset, bar in enumerate(bars[start + 1:], start=1):",
           "    for offset, bar in enumerate(bars[start:], start=1):",
           "the entry bar resolves the trade it created",
           ("unit/evaluation",)),
    Mutant("backtest-optimistic-bar", "evaluation/backtest.py",
           "        if hit_stop:\n            # Pessimistic on an ambiguous bar",
           "        if hit_target:\n            return Exit.TARGET, offset\n        if hit_stop:\n            # Pessimistic on an ambiguous bar",
           "a bar holding both stop and target is scored a win",
           ("unit/evaluation",)),
    Mutant("backtest-unresolved-as-win", "evaluation/backtest.py",
           "        return [t for t in self.resolved if t.exit is Exit.TARGET]",
           "        return [t for t in self.trades if t.exit is not Exit.STOP]",
           "trades that never resolved are counted as wins",
           ("unit/evaluation",)),
    Mutant("backtest-zero-win-rate", "evaluation/backtest.py",
           "        return len(self.wins) / len(self.resolved) if self.resolved else None",
           "        return len(self.wins) / len(self.resolved) if self.resolved else 0.0",
           "no evidence is reported as a measured 0% win rate",
           ("unit/evaluation",)),
]


# ==========================================================================

#: Wall-clock ceiling for one mutant's test run.
#:
#: Some mutants REMOVE A TERMINATION CONDITION — disabling the runtime's
#: max_cycles bound makes its loop run forever, so the suite never finishes
#: rather than failing. Without this ceiling the harness inherits that hang,
#: and in CI it burns a runner until the platform's job limit. A hang is
#: still a KILL (the tests did not pass), but it is reported distinctly:
#: "the guard is load-bearing" and "removing it wedges the process" are
#: different findings and the second one deserves to be visible.
#:
#: 60s is roughly two orders of magnitude above the slowest legitimate
#: mutant run, so it cannot mistake a slow CI runner for a hang, while
#: keeping the cost of the one genuinely non-terminating mutant bounded.
MUTANT_TIMEOUT_SECONDS = 60


def _run_tests(paths: tuple[str, ...]) -> tuple[bool, str]:
    """True if the suite PASSED (i.e. the mutant survived)."""
    targets = [str(TESTS / p) for p in paths] or [str(TESTS)]
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-x", "-q", "--no-header", "-p",
             "no:cacheprovider", *targets],
            cwd=ROOT, capture_output=True, text=True,
            timeout=MUTANT_TIMEOUT_SECONDS,
            # CPython invalidates a .pyc on (mtime, size). Most mutants here
            # are the same LENGTH as the code they replace and are written
            # inside one filesystem timestamp tick, so bytecode compiled from
            # mutated source can outlive the restore and be served to a LATER
            # run. That bit this harness during development: a full suite run
            # after a mutation run failed against source already correct.
            # Not writing bytecode at all removes the whole class of problem.
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
    except subprocess.TimeoutExpired:
        return False, (f"TIMED OUT after {MUTANT_TIMEOUT_SECONDS}s — the mutant "
                       f"removed a termination condition, so the suite never "
                       f"finished. Counted as killed: the tests did not pass.")
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def _purge_bytecode(rel_paths) -> None:
    """Drop any cached bytecode for the files being mutated.

    Belt and braces alongside PYTHONDONTWRITEBYTECODE: a .pyc written before
    this run started is just as capable of hiding a restore.
    """
    for rel in rel_paths:
        cache = (SRC / rel).parent / "__pycache__"
        if not cache.is_dir():
            continue
        stem = Path(rel).stem
        for pyc in cache.glob(f"{stem}.*.pyc"):
            pyc.unlink(missing_ok=True)


def _summary_line(output: str) -> str:
    # A hang is a different finding from a failure — "the guard is
    # load-bearing" versus "removing it wedges the process" — so it must not
    # be flattened into a missing-summary message.
    if output.startswith("TIMED OUT"):
        return output.split(" — ")[0] + " (removed a termination condition)"
    for line in reversed(output.strip().splitlines()):
        if "passed" in line or "failed" in line or "error" in line:
            return line.strip().strip("=").strip()
    return "no pytest summary"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="show mutants, run nothing")
    ap.add_argument("-k", dest="filter", default="",
                    help="only mutants whose id or note contains this")
    args = ap.parse_args()

    selected = [m for m in MUTANTS
                if not args.filter
                or args.filter.lower() in m.id.lower()
                or args.filter.lower() in m.note.lower()]

    if args.list:
        for m in selected:
            print(f"{m.id:<28} {m.path:<32} {m.note}")
        print(f"\n{len(selected)} mutant(s)")
        return 0

    # A mutant whose anchor no longer exists is a FAILURE, not a skip: it means
    # the guard was refactored and this harness is quietly no longer testing it.
    files = sorted({m.path for m in selected})
    originals: dict[str, str] = {}
    for rel in files:
        p = SRC / rel
        if not p.exists():
            print(f"FATAL: {rel} does not exist")
            return 2
        originals[rel] = p.read_text()

    dirty = subprocess.run(["git", "diff", "--name-only", "--",
                            *[f"src/speedtrader/{f}" for f in files]],
                           cwd=ROOT, capture_output=True, text=True).stdout.strip()
    if dirty:
        print("REFUSING TO RUN: uncommitted changes in files this harness mutates.")
        print("Commit or stash first, so an interrupted run cannot be mistaken")
        print("for your own edits:\n  " + "\n  ".join(dirty.splitlines()))
        return 2

    def restore(*_a) -> None:
        for rel, text in originals.items():
            (SRC / rel).write_text(text)
        _purge_bytecode(originals)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *a: (restore(), sys.exit(130)))

    killed, survived, broken = [], [], []
    equivalent, mislabelled = [], []
    print(f"applying {len(selected)} mutant(s)\n")
    try:
        for i, m in enumerate(selected, 1):
            source = originals[m.path]
            if source.count(m.old) != 1:
                broken.append(m)
                print(f"  {i:>2}/{len(selected)}  {m.id:<28} ANCHOR LOST "
                      f"({source.count(m.old)} matches) — guard refactored?")
                continue
            (SRC / m.path).write_text(source.replace(m.old, m.new, 1))
            _purge_bytecode([m.path])
            passed, output = _run_tests(m.tests)
            restore()
            if passed and m.equivalent:
                equivalent.append(m)
                print(f"  {i:>2}/{len(selected)}  {m.id:<28} \033[93mequivalent\033[0m"
                      f" — survives as declared")
            elif passed:
                survived.append(m)
                print(f"  {i:>2}/{len(selected)}  {m.id:<28} \033[91mSURVIVED\033[0m  "
                      f"— nothing catches: {m.note}")
            elif m.equivalent:
                mislabelled.append(m)
                print(f"  {i:>2}/{len(selected)}  {m.id:<28} \033[91mMISLABELLED\033[0m"
                      f" — declared equivalent but the tests caught it")
            else:
                killed.append(m)
                print(f"  {i:>2}/{len(selected)}  {m.id:<28} \033[92mkilled\033[0m    "
                      f"{_summary_line(output)}")
    finally:
        restore()

    print(f"\n{len(killed)} killed, {len(survived)} survived, "
          f"{len(equivalent)} equivalent by construction, "
          f"{len(broken)} anchor(s) lost, of {len(selected)}")
    if equivalent:
        print("\nEQUIVALENT MUTANTS — declared unkillable, and why:")
        for m in equivalent:
            print(f"  {m.id}: {m.equivalent}")
    if mislabelled:
        print("\nMISLABELLED — declared equivalent but the suite killed them, so")
        print("the argument for equivalence is wrong and should be removed:")
        for m in mislabelled:
            print(f"  {m.id}: {m.equivalent}")
    if survived:
        print("\nSURVIVING MUTANTS — each is a safety property nothing tests:")
        for m in survived:
            print(f"  {m.id}: {m.note}")
    if broken:
        print("\nLOST ANCHORS — the harness is no longer testing these:")
        for m in broken:
            print(f"  {m.id}  ({m.path})")
    return 0 if not (survived or broken or mislabelled) else 1


if __name__ == "__main__":
    sys.exit(main())

"""
SpeedTrader AI — Deterministic Decision Fingerprint

    A 16-character hash that identifies WHAT THE DETERMINISTIC SYSTEM DECIDED,
    independently of when it ran, what it called things, or what the AI said.

--------------------------------------------------------------------------------
WHY THIS IS THE SHARPEST THING IN THE SYSTEM
--------------------------------------------------------------------------------
Every LLM-driven trading agent has the same documented weakness, stated by its
own authors: model output is not reproducible, so a decision cannot be audited
after the fact. You cannot re-run it and get the same answer.

SpeedTrader can, because the part that decides is deterministic. The fingerprint
makes that checkable rather than merely claimed:

  1. REPRODUCIBILITY — replay a stored decision from its snapshot and the
     fingerprint must be identical. If it is not, the CODE changed, and the
     replay says so instead of quietly producing a different answer.

  2. AI NON-INTERFERENCE — the fingerprint deliberately EXCLUDES the AI review.
     So the same market state produces the same fingerprint whether the model
     confirmed, abstained, timed out, or was never configured. That is the
     project's central claim, reduced to comparing two strings.

--------------------------------------------------------------------------------
WHAT IS DELIBERATELY EXCLUDED, AND WHY
--------------------------------------------------------------------------------
    decision_id / signal_id / snapshot_id   random per run; including them would
                                            make every fingerprint unique and
                                            the whole mechanism worthless
    timestamps                              same reason
    ai_review                               THE POINT: excluded so that AI
                                            presence cannot move the hash
    broker order ids                        assigned by Alpaca, not decided here

--------------------------------------------------------------------------------
A KNOWN LIMIT, STATED RATHER THAN HIDDEN
--------------------------------------------------------------------------------
The option chain is NOT part of the stored snapshot, so two decisions that differ
only in the chain they were offered can share a fingerprint. In practice this
happens when both were rejected before a contract was selected — an illiquid book
and an unaffordable premium both stop at "no contract sized", and from the
deterministic payload's point of view those are the same decision reached from
the same market state.

That is correct for what the fingerprint claims to identify (the deterministic
decision) and wrong for what someone might assume it identifies (the whole
scenario). Persisting the chain would fix it and would also bloat every record
with hundreds of contracts; the trade was made deliberately and is recorded here
rather than left for someone to discover.

--------------------------------------------------------------------------------
THE ONE THING THE AI CAN CHANGE, TRACKED SEPARATELY
--------------------------------------------------------------------------------
A VETO does change the outcome — that is the AI's single power. So the veto is
NOT folded into the fingerprint; it is recorded beside it. The fingerprint
answers "what did determinism decide?", the veto flag answers "was that decision
then cancelled?". Merging them would destroy the ability to prove property 2.

This means a vetoed and a non-vetoed run of the same market state share a
fingerprint — which is correct and is exactly what makes the veto visible as a
separate, auditable act rather than an invisible influence on the decision.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

FINGERPRINT_VERSION = "fp-v1"
LENGTH = 16


def _round(value: Any, places: int = 6) -> Any:
    """Round floats so that IEEE noise in the last bits cannot change a hash.

    Two runs of identical arithmetic on the same machine agree exactly, but a
    fingerprint that is fragile to the 15th decimal is not usable as an identity
    across machines or Python versions.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return str(value)
        return round(value, places)
    if isinstance(value, Mapping):
        return {k: _round(v, places) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_round(v, places) for v in value]
    return value


def deterministic_payload(decision: Mapping[str, Any]) -> dict[str, Any]:
    """Extract exactly the deterministic decision, in a stable shape.

    Built explicitly field by field rather than by deleting keys from the record.
    A denylist silently absorbs any new field into the hash, which would break
    fingerprint stability the moment the schema grows; an allowlist fails safe by
    ignoring what it does not know about.
    """
    snapshot = decision.get("snapshot") or {}
    candidate = decision.get("candidate") or {}
    gate = decision.get("risk_gate") or {}
    options = decision.get("options_trace") or {}
    contract = options.get("contract") or {}
    sizing = options.get("sizing") or {}

    bars = snapshot.get("bars") or []
    # Hash the bars rather than embedding them: identity of the input series
    # without a fingerprint payload that grows with history length.
    bars_digest = hashlib.sha256(
        json.dumps(_round(bars), sort_keys=True, separators=(",", ":"),
                   default=str).encode()
    ).hexdigest()[:16]

    return {
        "version": FINGERPRINT_VERSION,
        "market": {
            "symbol": snapshot.get("symbol"),
            "price": _round(snapshot.get("price")),
            "spread": _round(snapshot.get("spread")),
            "regime": snapshot.get("regime"),
            "market_open": snapshot.get("market_open"),
            "bars_digest": bars_digest,
            "bar_count": len(bars),
            "features": _round(snapshot.get("features") or {}),
        },
        "quant": {
            "strategy_id": candidate.get("strategy_id"),
            "direction": candidate.get("direction"),
            "entry": _round(candidate.get("entry")),
            "stop_loss": _round(candidate.get("stop_loss")),
            "take_profit": _round(candidate.get("take_profit")),
            "base_score": _round(candidate.get("base_score")),
            "total_score": _round(candidate.get("total_score")),
            "expected_value": _round(candidate.get("expected_value")),
            "ev_is_bootstrap": candidate.get("ev_is_bootstrap"),
        },
        # Every rule and its outcome, in evaluation order. Two runs that reach
        # the same verdict by different checks are NOT the same decision.
        "risk": {
            "verdict": gate.get("verdict"),
            "engine_version": gate.get("engine_version"),
            "approved_quantity": _round(gate.get("approved_quantity")),
            "size_multiplier": _round(gate.get("size_multiplier")),
            "blocking_reason": gate.get("blocking_reason"),
            "checks": [
                {"rule": c.get("rule"), "passed": c.get("passed")}
                for c in (gate.get("checks") or [])
            ],
        },
        "options": {
            "structure": options.get("structure"),
            "contract": contract.get("symbol"),
            "strike": _round(contract.get("strike")),
            "expiration": contract.get("expiration"),
            "contracts": sizing.get("contracts"),
            "max_loss_total": _round(sizing.get("max_loss_total")),
            "risk_budget": _round(sizing.get("risk_budget")),
        },
        "outcome": {
            "state": decision.get("state"),
            "rejection_stage": decision.get("rejection_stage"),
        },
    }


def decision_fingerprint(decision: Mapping[str, Any]) -> str:
    """The deterministic identity of one decision."""
    payload = deterministic_payload(decision)
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:LENGTH]


def ai_influence(decision: Mapping[str, Any]) -> dict[str, Any]:
    """What the AI did, kept strictly outside the fingerprint.

    `changed_outcome` is true only for a veto, because a veto is the only thing
    the AI is capable of changing. Everything else it can emit is, by
    construction, an opinion with no mechanical effect.
    """
    review = decision.get("ai_review") or {}
    judge = review.get("judge") or {}
    return {
        "consulted": bool(review),
        "verdict": judge.get("verdict"),
        "vetoed": bool(review.get("vetoed")),
        "changed_outcome": bool(review.get("vetoed")),
        "model": (judge.get("provenance") or {}).get("model"),
        "provider": (judge.get("provenance") or {}).get("provider"),
        "degraded": bool(review.get("degraded")),
    }

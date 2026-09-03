"""
SpeedTrader AI — Decision Replay

Re-runs a stored decision from its own persisted snapshot and compares the
result to what was recorded. Answers three questions a judge or an auditor
actually asks:

    1. Is this system reproducible?          replay -> same fingerprint
    2. Did the AI change the outcome?        replay with the AI OFF -> same
                                             fingerprint, unless it vetoed
    3. Has the code drifted since?           replay -> different fingerprint,
                                             and the divergence is reported
                                             rather than silently accepted

--------------------------------------------------------------------------------
REPLAY MUST NEVER TRADE
--------------------------------------------------------------------------------
It is handed no execution adapter and no authorization registry, so it
structurally cannot submit an order — replaying an old decision must never place
a new one. It also writes to its own store, never to the production decision
journal: an append-only audit trail that a replay tool can append to is no longer
an audit trail.

--------------------------------------------------------------------------------
NO LOOK-AHEAD
--------------------------------------------------------------------------------
The replay uses the snapshot exactly as it was stored, including its original
`now`. Re-deriving anything from the current clock would let information that
did not exist at decision time leak into the reconstruction — the same class of
error as look-ahead bias in a backtest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..data.schemas import MarketSnapshot
from ..quant.engine import QuantCore
from ..risk.engine import DeterministicRiskEngine
from ..risk.state import AccountState, PortfolioState
from .fingerprint import ai_influence, decision_fingerprint, deterministic_payload


class ReplayError(RuntimeError):
    """The stored decision could not be replayed. Never silently skipped."""


@dataclass(frozen=True)
class ReplayResult:
    decision_id: str
    reproducible: bool
    original_fingerprint: str
    replay_fingerprint: str
    ai: dict[str, Any]
    divergences: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def ai_changed_the_outcome(self) -> bool:
        return bool(self.ai.get("changed_outcome"))

    def to_record(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "reproducible": self.reproducible,
            "original_fingerprint": self.original_fingerprint,
            "replay_fingerprint": self.replay_fingerprint,
            "ai": dict(self.ai),
            "divergences": list(self.divergences),
            "note": self.note,
        }


def _diff(original: Mapping[str, Any], replayed: Mapping[str, Any],
          path: str = "") -> list[str]:
    """Human-readable differences between two deterministic payloads.

    "Not reproducible" is useless on its own; an auditor needs to know WHICH
    field moved, because that is what points at the code change responsible.
    """
    out: list[str] = []
    for key in sorted(set(original) | set(replayed)):
        here = f"{path}.{key}" if path else key
        a, b = original.get(key), replayed.get(key)
        if isinstance(a, Mapping) and isinstance(b, Mapping):
            out.extend(_diff(a, b, here))
        elif a != b:
            out.append(f"{here}: stored={a!r} replayed={b!r}")
    return out


def replay_decision(
    record: Mapping[str, Any],
    *,
    strategies: Sequence[Any],
    execution_config: Mapping[str, Any],
    risk_config: Mapping[str, Any],
    account: AccountState | None = None,
    portfolio: PortfolioState | None = None,
) -> ReplayResult:
    """Re-derive one decision from its stored snapshot.

    The AI is not run at all. That is deliberate rather than a simplification:
    if the deterministic result is identical with the model entirely absent,
    the model demonstrably did not influence it.
    """
    decision_id = str(record.get("decision_id") or "")
    original_fp = decision_fingerprint(record)
    ai = ai_influence(record)

    raw_snapshot = record.get("snapshot")
    if not raw_snapshot:
        raise ReplayError(
            f"decision {decision_id} has no stored snapshot; it cannot be "
            "replayed. A decision that cannot be reconstructed is not auditable."
        )
    try:
        snapshot = MarketSnapshot.model_validate(raw_snapshot)
    except Exception as e:
        raise ReplayError(
            f"decision {decision_id} snapshot failed validation: {e}"
        ) from e

    # The ORIGINAL decision time, never the current clock — using "now" would
    # leak information that did not exist when the decision was made.
    now = snapshot.timestamp

    quant = QuantCore(list(strategies), execution_config)
    risk = DeterministicRiskEngine(risk_config)

    rebuilt: dict[str, Any] = {
        "snapshot": raw_snapshot,
        "state": record.get("state"),
        "rejection_stage": record.get("rejection_stage"),
        # Carried across unchanged: replay re-derives the quant and risk layers,
        # not the contract chain, which depends on an option book that is not
        # part of the stored snapshot.
        "options_trace": record.get("options_trace"),
    }

    result = quant.run(snapshot, now=now)
    if result.ok and result.candidate is not None:
        rebuilt["candidate"] = result.candidate.model_dump(mode="json")
        gate = risk.evaluate(
            signal=result.candidate,
            account=account or _account_from(record),
            portfolio=portfolio or PortfolioState(),
            spread_pct=snapshot.spread_pct, gap_pct=snapshot.gap_pct,
            market_open=snapshot.market_open, now=now,
        )
        rebuilt["risk_gate"] = gate.model_dump(mode="json")

    replay_fp = decision_fingerprint(rebuilt)
    divergences = _diff(deterministic_payload(record), deterministic_payload(rebuilt))

    reproducible = replay_fp == original_fp
    note = (
        "deterministic result reproduced exactly from the stored snapshot"
        if reproducible else
        "REPRODUCTION FAILED — the code or configuration has changed since this "
        "decision was recorded. The differing fields are listed."
    )
    return ReplayResult(
        decision_id=decision_id, reproducible=reproducible,
        original_fingerprint=original_fp, replay_fingerprint=replay_fp,
        ai=ai, divergences=divergences, note=note,
    )


def _account_from(record: Mapping[str, Any]) -> AccountState:
    """Reconstruct the account the decision was sized against.

    The balance is recoverable from the risk budget the options layer recorded
    (budget = balance x risk_pct), so a replay does not have to invent one. If it
    is absent the replay still runs, but the risk layer is re-derived against a
    stated default rather than a guessed-at real balance.
    """
    sizing = ((record.get("options_trace") or {}).get("sizing") or {})
    budget = sizing.get("risk_budget")
    balance = float(budget) * 100.0 if budget else 100_000.0
    return AccountState(balance=balance, equity=balance,
                        day_start_equity=balance, equity_high_water=balance)


def replay_all(
    records: Sequence[Mapping[str, Any]],
    *,
    strategies: Sequence[Any],
    execution_config: Mapping[str, Any],
    risk_config: Mapping[str, Any],
) -> list[ReplayResult]:
    """Replay a batch. One unreplayable decision does not abandon the rest, and
    is reported as a failure rather than dropped from the count."""
    out: list[ReplayResult] = []
    for record in records:
        try:
            out.append(replay_decision(
                record, strategies=strategies, execution_config=execution_config,
                risk_config=risk_config))
        except ReplayError as e:
            out.append(ReplayResult(
                decision_id=str(record.get("decision_id") or ""),
                reproducible=False,
                original_fingerprint=decision_fingerprint(record),
                replay_fingerprint="", ai=ai_influence(record),
                divergences=[str(e)], note="could not be replayed"))
    return out

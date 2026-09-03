"""
SpeedTrader AI — restart recovery

--------------------------------------------------------------------------------
WHAT RUNS BEFORE ANYTHING ELSE IS ALLOWED TO TRADE
--------------------------------------------------------------------------------
The intent journal records an execution attempt before the broker is contacted.
After a crash it therefore contains entries with no outcome, each meaning:

    "an order MAY exist at the broker under this client_order_id, and nobody
     has established what happened to it."

Recovery settles each one against the broker and refuses to let the runtime
start trading until it has. Starting a new cycle with unresolved intents is how
a restart turns one intended position into two.

--------------------------------------------------------------------------------
IT ASKS; IT NEVER ASSUMES
--------------------------------------------------------------------------------
Every resolution here comes from the broker's own answer about the
client_order_id. Nothing is inferred from elapsed time, from the absence of a
position, or from what the strategy expected. Two rules follow:

  * NOT_FOUND resolves to ABANDONED — the broker has no record, so the
    submission never landed and no position exists.
  * Anything the broker cannot answer stays UNRESOLVED and the runtime does not
    start. An outage is not evidence that an order is absent.

Recovery NEVER cancels, replaces or submits. It is read-only against the broker
by construction: it is handed an OrderLookup and nothing else, so there is no
code path from here to an order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .intent_journal import IntentJournal, IntentPhase, PendingIntent
from .reconciliation import (
    OrderLookup, Reconciliation, ReconciliationUnavailable, ReconciledState,
    reconcile_order,
)

#: Broker truth -> what the intent log should say about it.
_STATE_TO_PHASE = {
    ReconciledState.NOT_FOUND: IntentPhase.ABANDONED,
    ReconciledState.REJECTED: IntentPhase.REJECTED,
}


@dataclass(frozen=True)
class RecoveredIntent:
    intent: PendingIntent
    reconciliation: Reconciliation | None
    phase: IntentPhase | None
    error: str = ""

    @property
    def resolved(self) -> bool:
        return self.reconciliation is not None

    @property
    def position_exists(self) -> bool:
        return bool(self.reconciliation and self.reconciliation.position_exists)


@dataclass(frozen=True)
class RecoveryReport:
    """What a restart found. `safe_to_trade` gates the runtime."""
    recovered: list[RecoveredIntent] = field(default_factory=list)

    @property
    def unresolved(self) -> list[RecoveredIntent]:
        return [r for r in self.recovered if not r.resolved]

    @property
    def positions_found(self) -> list[RecoveredIntent]:
        return [r for r in self.recovered if r.position_exists]

    @property
    def safe_to_trade(self) -> bool:
        """False while ANY intent is unresolved.

        Deliberately not "false only if a position was found": an intent we
        could not resolve is exactly the case where a new order might duplicate
        an existing one, so it is the case that must block.
        """
        return not self.unresolved

    def summary(self) -> str:
        if not self.recovered:
            return "no pending execution intents; clean start"
        parts = [f"{len(self.recovered)} pending intent(s) from a previous run"]
        if self.positions_found:
            parts.append(f"{len(self.positions_found)} already have a position")
        if self.unresolved:
            parts.append(
                f"{len(self.unresolved)} UNRESOLVED — refusing to trade until "
                f"the broker can be reached"
            )
        return "; ".join(parts)


def recover(
    journal: IntentJournal, lookup: OrderLookup, *, now: datetime | None = None,
) -> RecoveryReport:
    """Settle every pending intent against the broker.

    Resolutions are written back to the journal, so a second restart does not
    re-ask about intents already settled.
    """
    recovered: list[RecoveredIntent] = []

    for pending in journal.pending():
        try:
            rec = reconcile_order(
                lookup,
                client_order_id=pending.client_order_id,
                expected_quantity=pending.quantity,
                expected_symbol=pending.symbol or None,
                now=now,
            )
        except ReconciliationUnavailable as e:
            # The broker could not be asked. The intent stays pending on disk
            # so the next restart tries again; it is never downgraded to
            # "probably fine".
            recovered.append(RecoveredIntent(
                intent=pending, reconciliation=None, phase=None,
                error=f"{type(e).__name__}: {e}",
            ))
            continue

        phase = _STATE_TO_PHASE.get(rec.state, IntentPhase.RECONCILED)
        journal.record_outcome(
            client_order_id=pending.client_order_id,
            phase=phase,
            broker_order_id=rec.broker_order_id,
            broker_status=rec.broker_status,
            detail=f"recovered on restart: {rec.reason or rec.state.value}",
            now=now,
            recovered=True,
            filled_quantity=rec.filled_quantity,
            position_exists=rec.position_exists,
        )
        recovered.append(RecoveredIntent(
            intent=pending, reconciliation=rec, phase=phase))

    return RecoveryReport(recovered=recovered)

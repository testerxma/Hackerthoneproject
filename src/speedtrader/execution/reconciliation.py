"""
SpeedTrader AI — Order Reconciliation

The adapter can only ever say SUBMITTED or UNKNOWN. Neither means a position
exists. This module is the only component permitted to resolve either into a
terminal truth, and it does so by asking the broker rather than by inferring.

--------------------------------------------------------------------------------
WHY THIS EXISTS
--------------------------------------------------------------------------------
An UNKNOWN submission is the single most dangerous state in the system: an order
may or may not be live. Two wrong instincts both lose money.

    "assume it failed"    -> retry -> two positions, double the intended risk
    "assume it worked"    -> a phantom position the risk engine now sizes around

So UNKNOWN is never resolved locally. It is resolved by looking the order up at
the broker by the client_order_id the adapter already sent — which is derived
from the single-use authorization nonce and is therefore a stable, unique handle
for exactly one intended order.

--------------------------------------------------------------------------------
WHAT RECONCILIATION MAY AND MAY NOT DO
--------------------------------------------------------------------------------
It may:  observe broker state, classify it, and report a discrepancy.
It may NOT: submit, amend, cancel or retry anything. It has no authorization and
cannot mint one, so it structurally cannot trade. A component that both decides
what is true and acts on it can hide its own mistakes.

An unresolvable order is escalated as NEEDS_HUMAN rather than guessed at. That is
the correct terminal state for genuine ambiguity, and pretending otherwise is how
an audit trail starts lying.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping, Protocol, Sequence


class ReconciledState(StrEnum):
    """Terminal truth about one submitted order."""
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    OPEN = "open"                    # live at the broker, not yet filled
    CANCELED = "canceled"
    EXPIRED = "expired"
    REJECTED = "rejected"            # the broker refused; no position
    NOT_FOUND = "not_found"          # no such order — the submission never landed
    NEEDS_HUMAN = "needs_human"      # genuinely ambiguous; never guessed


#: Broker status -> our terminal state. Anything absent from this map is NOT
#: assumed benign; it escalates. A new broker status must be classified
#: deliberately, not absorbed by a default.
_BROKER_STATUS = {
    "filled": ReconciledState.FILLED,
    "partially_filled": ReconciledState.PARTIALLY_FILLED,
    "new": ReconciledState.OPEN,
    "accepted": ReconciledState.OPEN,
    "pending_new": ReconciledState.OPEN,
    "accepted_for_bidding": ReconciledState.OPEN,
    "held": ReconciledState.OPEN,
    "canceled": ReconciledState.CANCELED,
    "pending_cancel": ReconciledState.OPEN,
    "expired": ReconciledState.EXPIRED,
    "rejected": ReconciledState.REJECTED,
    "suspended": ReconciledState.NEEDS_HUMAN,
    "stopped": ReconciledState.NEEDS_HUMAN,
    "calculated": ReconciledState.NEEDS_HUMAN,
    "done_for_day": ReconciledState.NEEDS_HUMAN,
    "replaced": ReconciledState.NEEDS_HUMAN,
    "pending_replace": ReconciledState.NEEDS_HUMAN,
}

#: States after which nothing further will happen on its own.
TERMINAL = frozenset({
    ReconciledState.FILLED, ReconciledState.CANCELED, ReconciledState.EXPIRED,
    ReconciledState.REJECTED, ReconciledState.NOT_FOUND,
})

#: States where money is actually at risk right now.
POSITION_EXISTS = frozenset({
    ReconciledState.FILLED, ReconciledState.PARTIALLY_FILLED,
})


class OrderLookup(Protocol):
    """The only capability reconciliation needs: read one order by our own id.

    Deliberately read-only. Handing this component a submit or cancel method
    would let the thing that decides what is true also change it.
    """

    def get_order_by_client_id(self, client_order_id: str) -> Mapping[str, Any] | None:
        ...


class ReconciliationUnavailable(RuntimeError):
    """The broker could not be queried. The order stays unresolved — it is never
    downgraded to 'probably fine'."""


@dataclass(frozen=True)
class Reconciliation:
    client_order_id: str
    state: ReconciledState
    filled_quantity: float = 0.0
    requested_quantity: float = 0.0
    average_fill_price: float | None = None
    broker_order_id: str | None = None
    broker_status: str | None = None
    reason: str = ""
    observed_at: datetime | None = None
    discrepancies: list[str] = field(default_factory=list)

    @property
    def position_exists(self) -> bool:
        return self.state in POSITION_EXISTS

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL

    @property
    def safe_to_retry(self) -> bool:
        """Only when we have positively established that no order exists.

        NOT_FOUND and REJECTED are the sole cases. Every other state either has
        a position or might still acquire one, and retrying against either is
        how a book ends up with double the intended risk.
        """
        return self.state in (ReconciledState.NOT_FOUND, ReconciledState.REJECTED)

    def to_record(self) -> dict[str, Any]:
        """Persisted onto the decision. Plain types only — this reaches JSONL."""
        return {
            "client_order_id": self.client_order_id,
            "state": self.state.value,
            "broker_status": self.broker_status,
            "broker_order_id": self.broker_order_id,
            "filled_quantity": self.filled_quantity,
            "requested_quantity": self.requested_quantity,
            "average_fill_price": self.average_fill_price,
            "position_exists": self.position_exists,
            "is_terminal": self.is_terminal,
            "safe_to_retry": self.safe_to_retry,
            "discrepancies": list(self.discrepancies),
            "reason": self.reason,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
        }


def _num(value: Any, default: float = 0.0) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return default if f != f else f


def reconcile_order(
    lookup: OrderLookup,
    *,
    client_order_id: str,
    expected_quantity: float,
    expected_symbol: str | None = None,
    now: datetime | None = None,
) -> Reconciliation:
    """Establish what actually happened to one submitted order.

    Raises ReconciliationUnavailable if the broker cannot be reached: an order
    whose state could not be observed stays unresolved rather than being
    optimistically closed.
    """
    now = now or datetime.now(timezone.utc)
    if not client_order_id:
        raise ValueError("client_order_id is required to reconcile an order")

    try:
        raw = lookup.get_order_by_client_id(client_order_id)
    except Exception as e:
        raise ReconciliationUnavailable(
            f"could not query order {client_order_id}: {type(e).__name__}: {e}"
        ) from e

    if raw is None:
        # The broker has no record. The submission never landed, so no position
        # exists and a retry is genuinely safe.
        return Reconciliation(
            client_order_id=client_order_id,
            state=ReconciledState.NOT_FOUND,
            requested_quantity=expected_quantity,
            reason="broker has no order with this client_order_id; "
                   "the submission did not land",
            observed_at=now,
        )
    if not isinstance(raw, Mapping):
        return Reconciliation(
            client_order_id=client_order_id, state=ReconciledState.NEEDS_HUMAN,
            requested_quantity=expected_quantity,
            reason=f"unreadable order payload of type {type(raw).__name__}",
            observed_at=now,
        )

    status = str(raw.get("status") or "").strip().lower()
    state = _BROKER_STATUS.get(status)
    if state is None:
        # An unrecognised status is escalated, never defaulted. A silent default
        # here would let an unknown broker state masquerade as a benign one.
        return Reconciliation(
            client_order_id=client_order_id, state=ReconciledState.NEEDS_HUMAN,
            broker_status=status or None,
            broker_order_id=str(raw.get("id") or "") or None,
            requested_quantity=expected_quantity,
            filled_quantity=_num(raw.get("filled_qty")),
            reason=f"unrecognised broker status {status!r}; not classified",
            observed_at=now,
        )

    filled = _num(raw.get("filled_qty"))
    requested = _num(raw.get("qty"), expected_quantity)
    avg = raw.get("filled_avg_price")
    avg_price = _num(avg, 0.0) if avg is not None else None

    discrepancies: list[str] = []
    if expected_symbol and str(raw.get("symbol") or "") != expected_symbol:
        # The id we generated came back attached to a different instrument.
        # That is never a benign difference.
        discrepancies.append(
            f"symbol mismatch: expected {expected_symbol}, broker says "
            f"{raw.get('symbol')!r}"
        )
    if requested and expected_quantity and requested != expected_quantity:
        discrepancies.append(
            f"quantity mismatch: authorized {expected_quantity}, broker has {requested}"
        )
    if filled > requested > 0:
        discrepancies.append(
            f"overfill: {filled} filled against {requested} requested"
        )
    if state is ReconciledState.FILLED and filled <= 0:
        discrepancies.append("broker reports filled with zero filled quantity")

    if discrepancies:
        # A discrepancy means our model of the order and the broker's disagree.
        # That is exactly the situation a human must look at.
        state = ReconciledState.NEEDS_HUMAN

    return Reconciliation(
        client_order_id=client_order_id,
        state=state,
        filled_quantity=filled,
        requested_quantity=requested,
        average_fill_price=avg_price,
        broker_order_id=str(raw.get("id") or "") or None,
        broker_status=status,
        reason=("discrepancy between authorized order and broker state"
                if discrepancies else f"broker reports {status}"),
        observed_at=now,
        discrepancies=discrepancies,
    )


def reconcile_all(
    lookup: OrderLookup,
    pending: Sequence[Mapping[str, Any]],
    *,
    now: datetime | None = None,
) -> list[Reconciliation]:
    """Reconcile a batch. One unreachable order does not abandon the rest.

    A failure to query is recorded as NEEDS_HUMAN for that order specifically,
    so a transient outage cannot quietly drop an order from the audit trail.
    """
    out: list[Reconciliation] = []
    for item in pending:
        coid = str(item.get("client_order_id") or "")
        try:
            out.append(reconcile_order(
                lookup, client_order_id=coid,
                expected_quantity=_num(item.get("quantity")),
                expected_symbol=item.get("symbol"), now=now,
            ))
        except (ReconciliationUnavailable, ValueError) as e:
            out.append(Reconciliation(
                client_order_id=coid, state=ReconciledState.NEEDS_HUMAN,
                requested_quantity=_num(item.get("quantity")),
                reason=f"could not reconcile: {e}", observed_at=now,
            ))
    return out

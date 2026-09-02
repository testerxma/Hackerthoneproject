"""
SpeedTrader AI — Options Execution Adapter

The single place where an order reaches a broker. Everything upstream reasons,
scores, sizes and authorizes; this submits. It is deliberately small, because the
smaller it is the easier it is to be sure it cannot be bypassed.

--------------------------------------------------------------------------------
TWO RULES THAT ARE NEVER RELAXED
--------------------------------------------------------------------------------
1. NO VALID AUTHORIZATION -> NO ORDER.
   The authorization is a required positional argument, not an optional keyword
   with a permissive default. There is no code path that submits without one.

2. SUBMITTED IS NOT FILLED, AND UNKNOWN IS NOT FAILED.
   A broker that times out may still have accepted the order. Treating that as a
   failure invites a retry that double-fills; treating it as success invents a
   position that may not exist. It is recorded as UNKNOWN and handed to
   reconciliation, which is the only component allowed to resolve it.

--------------------------------------------------------------------------------
IDEMPOTENCY
--------------------------------------------------------------------------------
Every submission carries a client_order_id derived from the authorization nonce,
which is unique per licence and single-use. If a submission is retried after an
ambiguous failure, the broker rejects the duplicate id rather than opening a
second position. The nonce is the idempotency key precisely because the
authorization system already guarantees it is used once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping, Protocol

from ..options.contracts import OptionContract
from .authorization import (
    AuthorizationError,
    AuthorizationRegistry,
    ExecutionAuthorization,
    verify,
)


class SubmissionState(StrEnum):
    """What we actually know. Note there is no FILLED: this adapter never
    observes a fill, it only submits. Fills arrive through reconciliation."""
    SUBMITTED = "submitted"
    REJECTED = "rejected"          # the broker refused; no order exists
    UNKNOWN = "unknown"            # ambiguous; an order MAY exist
    BLOCKED = "blocked"            # we refused before contacting the broker


class PositionIntent(StrEnum):
    BUY_TO_OPEN = "buy_to_open"
    SELL_TO_CLOSE = "sell_to_close"


@dataclass(frozen=True)
class OptionOrderRequest:
    """Exactly what will be sent. This object is what gets hashed into the
    authorization, so it cannot drift between approval and submission."""
    symbol: str                      # OCC contract symbol
    quantity: int
    intent: PositionIntent = PositionIntent.BUY_TO_OPEN
    order_type: str = "limit"
    limit_price: float | None = None
    time_in_force: str = "day"

    def to_proposal(self) -> dict[str, Any]:
        """The canonical form hashed by the authorization. Every field that
        changes the economics of the order must appear here, or a change to it
        would not invalidate the licence."""
        return {
            "symbol": self.symbol,
            "quantity": self.quantity,
            "intent": self.intent.value,
            "order_type": self.order_type,
            "limit_price": self.limit_price,
            "time_in_force": self.time_in_force,
        }


@dataclass(frozen=True)
class ExecutionResult:
    state: SubmissionState
    request: OptionOrderRequest
    client_order_id: str
    broker_order_id: str | None = None
    reason: str = ""
    submitted_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def needs_reconciliation(self) -> bool:
        return self.state in (SubmissionState.SUBMITTED, SubmissionState.UNKNOWN)

    @property
    def is_filled(self) -> bool:
        """Always False. Kept explicit so no caller can mistake submission for a
        fill by checking a truthy result."""
        return False


class BrokerPort(Protocol):
    """The narrow surface this adapter needs. Keeping it this small is what makes
    the adapter testable without a network and impossible to accidentally widen."""

    def submit_option_order(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        ...


class BrokerTimeout(Exception):
    """Ambiguous outcome. The order may or may not exist."""


class BrokerRejected(Exception):
    """Definite outcome: the broker refused and no order exists."""


class OptionsExecutionAdapter:
    """Submits one authorized option order. Holds no strategy or risk logic."""

    def __init__(self, broker: BrokerPort, registry: AuthorizationRegistry,
                 *, paper: bool = True):
        if not paper:
            # Structural, not advisory. This project has never validated a
            # strategy on live options and must not be one flag from doing so.
            raise ValueError(
                "OptionsExecutionAdapter refuses to run against a live account. "
                "Paper trading only."
            )
        self.broker = broker
        self.registry = registry

    def submit(
        self,
        request: OptionOrderRequest,
        authorization: ExecutionAuthorization,
        *,
        portfolio_snapshot: Mapping[str, Any],
        now: datetime | None = None,
    ) -> ExecutionResult:
        """Verify the licence against THIS request, then submit exactly once."""
        client_order_id = ""
        try:
            # Verify against the request actually being submitted — not against
            # whatever was approved earlier. Any drift invalidates the licence.
            verify(
                authorization,
                proposal=request.to_proposal(),
                portfolio=portfolio_snapshot,
                registry=self.registry,
                now=now,
            )
        except AuthorizationError as e:
            return ExecutionResult(
                state=SubmissionState.BLOCKED,
                request=request,
                client_order_id="",
                reason=f"{type(e).__name__}: {e}",
            )

        # Cross-check the quantity as well as the hash. The hash already covers
        # it; this makes the invariant explicit and survives any future change to
        # what to_proposal() includes.
        if request.quantity != authorization.approved_quantity:
            return ExecutionResult(
                state=SubmissionState.BLOCKED, request=request, client_order_id="",
                reason=(f"quantity {request.quantity} does not match the "
                        f"authorized {authorization.approved_quantity}"),
            )
        if request.quantity <= 0:
            return ExecutionResult(
                state=SubmissionState.BLOCKED, request=request, client_order_id="",
                reason=f"non-positive quantity {request.quantity}",
            )

        # Derived from the single-use nonce, so a retry after an ambiguous
        # failure collides at the broker instead of opening a second position.
        client_order_id = f"st-{authorization.nonce}"
        payload = {**request.to_proposal(), "client_order_id": client_order_id}

        try:
            raw = self.broker.submit_option_order(payload)
        except BrokerRejected as e:
            return ExecutionResult(
                state=SubmissionState.REJECTED, request=request,
                client_order_id=client_order_id, reason=str(e),
            )
        except BrokerTimeout as e:
            # The order MAY exist. Not a failure, not a success.
            return ExecutionResult(
                state=SubmissionState.UNKNOWN, request=request,
                client_order_id=client_order_id,
                reason=f"timeout, outcome unknown, reconcile before retrying: {e}",
            )
        except Exception as e:
            # Any unexpected error is also ambiguous: the request may have been
            # transmitted. Fail to UNKNOWN, never to a clean failure.
            return ExecutionResult(
                state=SubmissionState.UNKNOWN, request=request,
                client_order_id=client_order_id,
                reason=(f"unexpected {type(e).__name__} during submission, "
                        f"outcome unknown: {e}"),
            )

        if not isinstance(raw, Mapping):
            return ExecutionResult(
                state=SubmissionState.UNKNOWN, request=request,
                client_order_id=client_order_id,
                reason=f"unreadable broker response of type {type(raw).__name__}",
            )
        broker_id = raw.get("id") or raw.get("order_id")
        if not broker_id:
            # A response we cannot interpret is not a confirmation.
            return ExecutionResult(
                state=SubmissionState.UNKNOWN, request=request,
                client_order_id=client_order_id,
                reason="broker response carried no order id",
                raw=dict(raw),
            )

        return ExecutionResult(
            state=SubmissionState.SUBMITTED,
            request=request,
            client_order_id=client_order_id,
            broker_order_id=str(broker_id),
            reason="submitted; not filled until reconciliation confirms it",
            submitted_at=now or datetime.now(timezone.utc),
            raw=dict(raw),
        )


def limit_price_for(contract: OptionContract, *, cross_spread: bool = True) -> float:
    """Marketable limit at the ask.

    A market order on an option is dangerous: option books are thin and a market
    order can fill far from the quote. A limit at the ask is marketable — it
    fills like a market order in a normal book — but bounds the price paid, which
    is what keeps the max-loss figure the risk engine sized on actually true.
    """
    if contract.quote is None:
        raise ValueError("cannot price an order for a contract with no quote")
    return contract.quote.ask if cross_spread else contract.quote.mid

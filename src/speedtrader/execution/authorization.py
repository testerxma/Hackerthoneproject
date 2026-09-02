"""
SpeedTrader AI — Execution Authorization (§18)

    RiskGateResult          an AUDIT artifact. Explains a decision. Serialisable,
                            persisted, safe to log.
    ExecutionAuthorization  a short-lived execution LICENCE. Not an explanation,
                            not persistable, not loggable, single use.

Keeping these separate is the whole point. A RiskGateResult is written to the
decision store and read back later; if it *were* the permission to trade, then
anything that could replay a stored decision could trade, and an audit record
would double as a bearer token.

--------------------------------------------------------------------------------
WHAT THIS DEFENDS AGAINST
--------------------------------------------------------------------------------
    forged        constructing an authorization outside the risk engine
    expired       trading on a decision whose market context has aged out
    replayed      submitting the same authorization twice (duplicate orders)
    substituted   approving proposal A and submitting proposal B
    stale book    approving against a portfolio that has since changed
    leaked        the signature appearing in a log, repr, traceback or JSONL

--------------------------------------------------------------------------------
THE BINDING
--------------------------------------------------------------------------------
An authorization is bound to the exact thing that was approved by hashing it:

    proposal_hash   what is to be submitted (symbol, side, quantity, limit, ...)
    portfolio_hash  the book the approval was computed against
    snapshot_id     the market state that produced the signal

The execution adapter recomputes both hashes from the objects it is ACTUALLY
about to send and compares. Any drift between what was approved and what is being
submitted invalidates the licence, so a Portfolio Manager cannot approve a small
order and then submit a large one.

The signature is HMAC-SHA256 over those bindings using a per-process secret
generated at import. The secret never leaves the process and is never written
anywhere, so an authorization minted in one process is meaningless in another —
which is the intended blast radius for a single-process trading loop. This is
deliberately NOT a distributed-trust design: if this ever runs multi-process, the
secret must move to a real KMS and that is a design change, not a config change.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

#: Per-process signing key. Regenerated every start: authorizations do not and
#: must not survive a restart, because the market context that justified them
#: did not either.
_PROCESS_SECRET = os.urandom(32)

#: Only code holding this sentinel may construct an ExecutionAuthorization. It is
#: module-private and handed to the minter alone, so `ExecutionAuthorization(...)`
#: from anywhere else raises rather than producing a valid-looking licence.
_MINT_TOKEN = object()

DEFAULT_TTL_SECONDS = 30.0


class AuthorizationError(Exception):
    """Base. Every failure here means NO EXECUTION."""


class AuthorizationForged(AuthorizationError):
    """Signature did not verify, or the object was not minted by the engine."""


class AuthorizationExpired(AuthorizationError):
    """The licence aged out. Revalidate; never extend."""


class AuthorizationReplayed(AuthorizationError):
    """Already consumed. A second use is a duplicate order attempt."""


class AuthorizationMismatch(AuthorizationError):
    """The proposal or portfolio is not the one that was approved."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def canonical_hash(payload: Mapping[str, Any]) -> str:
    """Stable hash of a mapping.

    sort_keys makes it independent of dict ordering, and default=str keeps it
    total for dates/enums/Decimals so hashing can never itself raise and be
    mistaken for a verification failure.
    """
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ExecutionAuthorization:
    """A single-use licence to submit one specific order.

    Deliberately NOT a pydantic model and deliberately not serialisable: the
    surrounding system persists pydantic models to JSONL, and inheriting that
    behaviour is exactly how a bearer token ends up in an audit file.
    """
    decision_id: str
    snapshot_id: str
    proposal_hash: str
    portfolio_hash: str
    approved_quantity: int
    expires_at: datetime
    nonce: str
    _signature: str
    _token: Any = None

    def __post_init__(self) -> None:
        if self._token is not _MINT_TOKEN:
            raise AuthorizationForged(
                "ExecutionAuthorization may only be minted by the deterministic "
                "risk engine via authorize(). Direct construction is refused."
            )

    # -- leak protection ------------------------------------------------
    # repr appears in logs, tracebacks, pytest output and debugger frames.
    def __repr__(self) -> str:
        return (f"<ExecutionAuthorization decision={self.decision_id} "
                f"qty={self.approved_quantity} expires={self.expires_at:%H:%M:%S} "
                f"signature=REDACTED>")

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError(
            "ExecutionAuthorization is not serialisable. It is a short-lived "
            "in-process licence, not a record; persist the RiskGateResult instead."
        )

    def __getstate__(self):
        raise TypeError("ExecutionAuthorization is not serialisable")

    def model_dump(self, *a, **k):          # guards accidental pydantic-style use
        raise TypeError("ExecutionAuthorization is not serialisable")

    def dict(self, *a, **k):
        raise TypeError("ExecutionAuthorization is not serialisable")

    @property
    def expired(self) -> bool:
        return _utcnow() >= self.expires_at


def _sign(decision_id: str, snapshot_id: str, proposal_hash: str,
          portfolio_hash: str, approved_quantity: int, expires_at: datetime,
          nonce: str) -> str:
    msg = "|".join([
        decision_id, snapshot_id, proposal_hash, portfolio_hash,
        str(approved_quantity), expires_at.astimezone(timezone.utc).isoformat(),
        nonce,
    ]).encode("utf-8")
    return hmac.new(_PROCESS_SECRET, msg, hashlib.sha256).hexdigest()


class AuthorizationRegistry:
    """Tracks nonces so a licence is usable exactly once.

    Thread-safe: consumption must be atomic, or two threads racing the same
    authorization both see "unused" and both submit an order.
    """

    def __init__(self) -> None:
        self._used: set[str] = set()
        self._lock = threading.Lock()

    def consume(self, nonce: str) -> None:
        with self._lock:
            if nonce in self._used:
                raise AuthorizationReplayed(
                    f"authorization {nonce[:8]}... was already used. A second "
                    "submission would duplicate the order."
                )
            self._used.add(nonce)

    def __contains__(self, nonce: str) -> bool:
        with self._lock:
            return nonce in self._used


def authorize(
    *,
    decision_id: str,
    snapshot_id: str,
    proposal: Mapping[str, Any],
    portfolio: Mapping[str, Any],
    approved_quantity: int,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    now: datetime | None = None,
) -> ExecutionAuthorization:
    """Mint a licence. ONLY the deterministic risk engine should call this.

    Refuses to mint for a non-positive quantity: 'approved for zero' is not an
    approval, and letting it exist invites a caller to treat the presence of an
    authorization as permission and supply its own size.
    """
    if approved_quantity <= 0:
        raise AuthorizationError(
            f"refusing to authorize a non-positive quantity ({approved_quantity})"
        )
    if ttl_seconds <= 0:
        raise AuthorizationError("ttl_seconds must be positive")

    now = now or _utcnow()
    expires = now + timedelta(seconds=ttl_seconds)
    nonce = secrets.token_hex(16)
    p_hash = canonical_hash(proposal)
    b_hash = canonical_hash(portfolio)
    return ExecutionAuthorization(
        decision_id=decision_id,
        snapshot_id=snapshot_id,
        proposal_hash=p_hash,
        portfolio_hash=b_hash,
        approved_quantity=int(approved_quantity),
        expires_at=expires,
        nonce=nonce,
        _signature=_sign(decision_id, snapshot_id, p_hash, b_hash,
                         int(approved_quantity), expires, nonce),
        _token=_MINT_TOKEN,
    )


def verify(
    auth: Any,
    *,
    proposal: Mapping[str, Any],
    portfolio: Mapping[str, Any],
    registry: AuthorizationRegistry,
    now: datetime | None = None,
    consume: bool = True,
) -> None:
    """Raise unless `auth` licenses exactly this proposal against this portfolio.

    Order matters: identity, then signature, then expiry, then bindings, then
    replay. The nonce is burned LAST so a request rejected for another reason
    does not consume a licence that was otherwise still valid.
    """
    if auth is None:
        raise AuthorizationForged("no authorization supplied; execution refused")
    if not isinstance(auth, ExecutionAuthorization):
        raise AuthorizationForged(
            f"expected ExecutionAuthorization, got {type(auth).__name__}"
        )

    expected = _sign(auth.decision_id, auth.snapshot_id, auth.proposal_hash,
                     auth.portfolio_hash, auth.approved_quantity,
                     auth.expires_at, auth.nonce)
    # compare_digest: constant time, so a forged signature cannot be discovered
    # byte-by-byte from timing.
    if not hmac.compare_digest(expected, auth._signature):
        raise AuthorizationForged("authorization signature did not verify")

    if (now or _utcnow()) >= auth.expires_at:
        raise AuthorizationExpired(
            f"authorization expired at {auth.expires_at.isoformat()}; "
            "revalidate through the risk engine rather than extending it"
        )

    if canonical_hash(proposal) != auth.proposal_hash:
        raise AuthorizationMismatch(
            "the proposal being submitted is not the one that was authorized"
        )
    if canonical_hash(portfolio) != auth.portfolio_hash:
        raise AuthorizationMismatch(
            "the portfolio has changed since this proposal was authorized; "
            "revalidate against the current book"
        )

    if consume:
        registry.consume(auth.nonce)

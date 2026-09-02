"""
SpeedTrader AI — Identity & Idempotency
Spec: §60 No Duplicate Execution, §83 Decision Trace

The chain snapshot_id -> signal_id -> decision_id -> order_id -> position_id -> trade_id
is what makes a decision auditable (§83) and replayable (§89).
"""

from __future__ import annotations

import uuid
from enum import StrEnum

from .clock import utcnow


class IdKind(StrEnum):
    SNAPSHOT = "snap"
    SIGNAL = "sig"
    EVIDENCE = "ev"
    DECISION = "dec"
    ORDER = "ord"
    POSITION = "pos"
    TRADE = "trd"
    AGENT_RUN = "run"


def new_id(kind: IdKind) -> str:
    """Format: <kind>_<utc-compact>_<8 hex>  e.g. dec_20260901T143005_9f2a1c4e"""
    return f"{kind.value}_{utcnow().strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"


def client_order_id(decision_id: str) -> str:
    """§60. Alpaca rejects duplicate client_order_id broker-side.

    Deriving it deterministically from decision_id means a retry can never open a
    second position even if our own duplicate check fails. Two independent layers.
    Alpaca's limit is 128 chars; ours are ~35.
    """
    return f"st-{decision_id}"

"""
SpeedTrader AI — execution intent journal (write-ahead log)

--------------------------------------------------------------------------------
THE FAILURE THIS EXISTS TO PREVENT
--------------------------------------------------------------------------------
Without a write-ahead record, the order of events is:

    mint licence -> submit to broker -> write the decision to the journal

A crash, SIGKILL, container eviction or power loss in the window between the
second and third step leaves an order live at the broker that NOTHING on disk
records. On restart the system has no idea it ever tried. It will happily
evaluate the same signal again and submit a SECOND order.

That is the single most expensive bug an execution system can have, and no
amount of care inside one process prevents it: the process is what dies.

So the intent is written and FSYNCED BEFORE the broker is contacted. After a
crash the journal contains an entry with no outcome, which is precisely the
statement "an order may exist at the broker under this client_order_id, and
nobody has established what happened to it." Recovery reconciles it against the
broker rather than guessing.

--------------------------------------------------------------------------------
WHY client_order_id IS THE KEY
--------------------------------------------------------------------------------
It is derived from the authorization nonce, so it is unique per licence and is
the same string the broker records. It is therefore the only identifier that
survives a process restart AND identifies the order broker-side. Everything here
is keyed on it.

An entry is never rewritten in place. Outcomes are APPENDED, and the latest
outcome for a client_order_id wins. An append-only log cannot be corrupted by a
crash mid-write into a state that silently changes an earlier record — a torn
final line is detected and skipped, and the entry simply stays unresolved, which
is the safe reading.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterator, Mapping


class IntentPhase(StrEnum):
    """How far one execution attempt got. Ordered from least to most resolved.

    ATTEMPTED is written BEFORE the broker is contacted. Every other phase is
    written after something has been established.
    """
    ATTEMPTED = "attempted"      # written pre-submission; outcome unknown
    SUBMITTED = "submitted"      # broker acknowledged, order id known
    UNKNOWN = "unknown"          # broker call ambiguous; an order MAY exist
    REJECTED = "rejected"        # broker positively refused; no order exists
    RECONCILED = "reconciled"    # broker state observed after the fact
    ABANDONED = "abandoned"      # established that no order was ever created


#: Phases after which no further broker action is pending. ATTEMPTED and UNKNOWN
#: are deliberately absent: both mean an order may exist and must be reconciled.
RESOLVED_PHASES = frozenset({
    IntentPhase.REJECTED, IntentPhase.RECONCILED, IntentPhase.ABANDONED,
})


@dataclass(frozen=True)
class IntentRecord:
    """One line of the log."""
    client_order_id: str
    phase: IntentPhase
    at: datetime
    decision_id: str = ""
    cycle_id: str = ""
    symbol: str = ""
    quantity: int = 0
    limit_price: float | None = None
    broker_order_id: str | None = None
    broker_status: str = ""
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps({
            "client_order_id": self.client_order_id,
            "phase": str(self.phase),
            "at": self.at.isoformat(),
            "decision_id": self.decision_id,
            "cycle_id": self.cycle_id,
            "symbol": self.symbol,
            "quantity": self.quantity,
            "limit_price": self.limit_price,
            "broker_order_id": self.broker_order_id,
            "broker_status": self.broker_status,
            "detail": self.detail,
            "extra": self.extra,
        }, separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "IntentRecord | None":
        """None for anything unreadable. A corrupt line must not abort recovery:
        skipping it leaves its intent unresolved, which is the safe reading."""
        try:
            coid = str(raw["client_order_id"])
            phase = IntentPhase(str(raw["phase"]))
            at = datetime.fromisoformat(str(raw["at"]))
        except (KeyError, ValueError, TypeError):
            return None
        if not coid:
            return None
        return cls(
            client_order_id=coid, phase=phase, at=at,
            decision_id=str(raw.get("decision_id") or ""),
            cycle_id=str(raw.get("cycle_id") or ""),
            symbol=str(raw.get("symbol") or ""),
            quantity=int(raw.get("quantity") or 0),
            limit_price=raw.get("limit_price"),
            broker_order_id=raw.get("broker_order_id"),
            broker_status=str(raw.get("broker_status") or ""),
            detail=str(raw.get("detail") or ""),
            extra=dict(raw.get("extra") or {}),
        )


@dataclass(frozen=True)
class PendingIntent:
    """An attempt whose outcome was never established. Recovery's unit of work."""
    client_order_id: str
    latest: IntentRecord
    first_attempt_at: datetime

    @property
    def symbol(self) -> str:
        return self.latest.symbol

    @property
    def quantity(self) -> int:
        return self.latest.quantity


class IntentJournal:
    """Append-only, fsynced write-ahead log of execution attempts.

    Deliberately not the DecisionStore: a decision record is written once the
    cycle is over and is the analytical record. This is an operational log
    written mid-flight, and its whole value is that an entry exists BEFORE the
    thing it describes has happened.
    """

    FILENAME = "execution_intents.jsonl"

    def __init__(self, root: str | Path, *, verify_writable: bool = True):
        self.root = Path(root)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            # Same failure class as an unwritable file, so callers have one
            # error to handle: the journal cannot be prepared, so we must not
            # trade.
            raise RuntimeError(
                f"execution intent journal directory is not writable at "
                f"{self.root}: {e}. Refusing to start: an order that cannot be "
                f"recorded before submission cannot be recovered after a crash."
            ) from e
        self.path = self.root / self.FILENAME
        # One process must not interleave two partial lines.
        self._lock = threading.Lock()
        if verify_writable:
            self._verify_writable()

    def _verify_writable(self) -> None:
        """Fail at construction, not at the moment an order is about to be sent.

        If the journal cannot be written we must not trade at all: an
        unrecorded attempt is exactly what this class exists to prevent.
        """
        try:
            with self.path.open("a", encoding="utf-8"):
                pass
        except OSError as e:
            raise RuntimeError(
                f"execution intent journal is not writable at {self.path}: {e}. "
                "Refusing to start: an order that cannot be recorded before "
                "submission cannot be recovered after a crash."
            ) from e

    # ------------------------------------------------------------------ #
    def _append(self, record: IntentRecord) -> IntentRecord:
        line = record.to_json() + "\n"
        with self._lock:
            # Durability, not just buffering: flush() alone leaves the line in
            # the OS page cache, which a power loss discards. The whole
            # guarantee is that this line survives the process dying.
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
        return record

    def record_attempt(
        self, *, client_order_id: str, decision_id: str, cycle_id: str,
        symbol: str, quantity: int, limit_price: float | None,
        now: datetime | None = None, **extra: Any,
    ) -> IntentRecord:
        """Write BEFORE contacting the broker. Returns once durable on disk."""
        if not client_order_id:
            raise ValueError("client_order_id is required to record an intent")
        return self._append(IntentRecord(
            client_order_id=client_order_id, phase=IntentPhase.ATTEMPTED,
            at=now or _utcnow(), decision_id=decision_id, cycle_id=cycle_id,
            symbol=symbol, quantity=int(quantity), limit_price=limit_price,
            extra=dict(extra),
        ))

    def record_outcome(
        self, *, client_order_id: str, phase: IntentPhase,
        broker_order_id: str | None = None, broker_status: str = "",
        detail: str = "", now: datetime | None = None, **extra: Any,
    ) -> IntentRecord:
        """Append what was established. Never edits the attempt line."""
        return self._append(IntentRecord(
            client_order_id=client_order_id, phase=phase, at=now or _utcnow(),
            broker_order_id=broker_order_id, broker_status=broker_status,
            detail=detail, extra=dict(extra),
        ))

    # ------------------------------------------------------------------ #
    def iter_records(self) -> Iterator[IntentRecord]:
        """Every readable line, in write order. Unreadable lines are skipped."""
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except ValueError:
                    # A torn final line from a crash mid-write. Skipping it
                    # leaves its intent unresolved — the safe reading.
                    continue
                record = IntentRecord.from_mapping(raw)
                if record is not None:
                    yield record

    def latest_phase(self, client_order_id: str) -> IntentPhase | None:
        latest: IntentRecord | None = None
        for record in self.iter_records():
            if record.client_order_id == client_order_id:
                latest = record
        return latest.phase if latest else None

    def pending(self) -> list[PendingIntent]:
        """Attempts with no resolving outcome — what recovery must settle.

        Returned oldest-first so recovery resolves in the order the orders were
        actually sent.
        """
        first: dict[str, IntentRecord] = {}
        latest: dict[str, IntentRecord] = {}
        for record in self.iter_records():
            first.setdefault(record.client_order_id, record)
            latest[record.client_order_id] = record

        out = [
            PendingIntent(client_order_id=coid, latest=rec,
                          first_attempt_at=first[coid].at)
            for coid, rec in latest.items()
            if rec.phase not in RESOLVED_PHASES
        ]
        out.sort(key=lambda p: p.first_attempt_at)
        return out

    def has_attempt(self, client_order_id: str) -> bool:
        """Was this licence ever used to contact the broker?

        The duplicate-submission guard: a client_order_id already in the journal
        must never be sent again, whatever the in-memory state believes.
        """
        return any(r.client_order_id == client_order_id
                   for r in self.iter_records())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

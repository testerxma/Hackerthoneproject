"""
SpeedTrader AI — Strategy Contract

Scope note: Step 2 authorised "S07 only". This file exists because S07's signature
needs a return type, and putting that type inside s07.py would force S08..S14 to
import from a sibling strategy — an import graph that would later have to be
refactored, touching already-audited code. Nothing here implements a strategy.

WHAT A STRATEGY RETURNS, AND WHAT IT DOES NOT.

A strategy emits a StrategyOutput: direction, entry, stop, target, base score. That is
exactly the set MQL5's InitSignal() populates from a strategy's arguments (source
L885-894). It is NOT a CandidateSignal — that object additionally carries signal_id,
snapshot_id, bonuses, total score, expected value and TTL, all of which are produced
by the scoring and candidate layers, not by a strategy. Keeping them out of this type
is what stops a strategy from becoming a candidate producer by accident.

Result-object convention follows data/snapshot.py::SnapshotResult: expected non-signal
outcomes are results with a reason, not exceptions. A strategy that declines to fire is
normal operation, and the reason is what §75 no-trade memory records.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ...data.schemas import Direction, MarketSnapshot


class Code:
    """Stable reason codes. The decision log and dashboard key off these strings."""
    SIGNAL = "signal"
    NO_SIGNAL = "no_signal"
    ATR_UNAVAILABLE = "atr_unavailable"
    INSUFFICIENT_HISTORY = "insufficient_history"
    INDICATOR_UNAVAILABLE = "indicator_unavailable"


@dataclass(frozen=True)
class StrategyOutput:
    """One strategy's raw proposal. Mirrors InitSignal's inputs (source L885)."""
    strategy_id: str
    direction: Direction
    entry: float
    stop_loss: float
    take_profit: float
    base_score: float
    breakdown: str = ""
    source_reference: str = ""
    #: Values the strategy actually read, for the decision trace.
    inputs: dict = field(default_factory=dict)


@dataclass(frozen=True)
class StrategyResult:
    ok: bool
    output: StrategyOutput | None = None
    reason: str = "ok"
    code: str = Code.NO_SIGNAL
    detail: dict | None = None

    def __bool__(self) -> bool:
        return self.ok


@runtime_checkable
class Strategy(Protocol):
    id: str
    source_reference: str
    min_bars: int

    def evaluate(self, snapshot: MarketSnapshot) -> StrategyResult: ...

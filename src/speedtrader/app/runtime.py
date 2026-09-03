"""
SpeedTrader AI — bounded autonomous runtime

--------------------------------------------------------------------------------
WHAT "AUTONOMOUS" MEANS HERE, AND WHAT IT DELIBERATELY DOES NOT
--------------------------------------------------------------------------------
The loop decides and executes without a human in the cycle. It does NOT get to
decide how often it runs, how many orders it may place, or whether to keep going
after something breaks. Those are bounds set before it starts, and it cannot
raise them — an agent that can widen its own limits has no limits.

Every bound below exists because of a specific way an unattended trading loop
goes wrong:

  max_cycles / until          a run that never ends is a run nobody is watching
  interval_seconds            a tight loop is a rate-limit ban and a bill
  max_orders                  one bad signal repeated is not one bad trade
  max_consecutive_errors      a broken dependency should stop the loop, not be
                              retried forever at full speed
  kill switch (a FILE)        stopping it must not require finding the process
  market-hours gate           an option order queued overnight prices against a
                              quote that is stale by the open

--------------------------------------------------------------------------------
RESTART SAFETY COMES FIRST, BEFORE ANY CYCLE
--------------------------------------------------------------------------------
`start()` runs crash recovery before the first cycle and REFUSES TO TRADE while
any execution intent from a previous run is unresolved. That is the case where a
new order might duplicate a live one, so it is exactly the case that must block.

--------------------------------------------------------------------------------
FAILURE ISOLATION
--------------------------------------------------------------------------------
One symbol raising must not kill the loop, and must not be silently swallowed
either: it is recorded on the cycle, counted toward the consecutive-error bound,
and surfaced in the runtime health state. A cycle that fails entirely is a
NO-TRADE cycle, never an assumed-fine one.
"""

from __future__ import annotations

import signal
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Sequence


class RuntimeState(StrEnum):
    IDLE = "idle"
    RECOVERING = "recovering"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    #: Refused to start, or stopped itself. Never resumes on its own.
    HALTED = "halted"


class StopReason(StrEnum):
    NOT_STOPPED = ""
    MAX_CYCLES = "max_cycles_reached"
    DEADLINE = "deadline_reached"
    MAX_ORDERS = "max_orders_reached"
    KILL_SWITCH = "kill_switch_engaged"
    SIGNAL = "operating_system_signal"
    TOO_MANY_ERRORS = "too_many_consecutive_errors"
    UNSAFE_START = "unresolved_execution_intents"


@dataclass(frozen=True)
class RuntimeLimits:
    """Hard bounds. Validated at construction: a nonsensical bound is a
    configuration error, and discovering it mid-run means it was never a bound."""
    interval_seconds: float = 300.0
    max_cycles: int | None = None
    max_orders: int | None = 10
    max_consecutive_errors: int = 3
    until: datetime | None = None
    #: Touch this file to stop the runtime after the current cycle. A file,
    #: not a signal, so an operator can stop it without finding the process.
    kill_switch_path: Path | None = None
    #: Options do not trade outside regular hours, and a day order queued
    #: overnight is priced from a quote that will be stale at the open.
    require_market_open: bool = True

    def validate(self) -> None:
        if self.interval_seconds < 1.0:
            raise ValueError(
                f"interval_seconds must be >= 1, got {self.interval_seconds}; "
                "a tighter loop is a rate-limit ban, not a faster strategy")
        if self.max_cycles is not None and self.max_cycles < 1:
            raise ValueError(f"max_cycles must be >= 1, got {self.max_cycles}")
        if self.max_orders is not None and self.max_orders < 0:
            raise ValueError(f"max_orders must be >= 0, got {self.max_orders}")
        if self.max_consecutive_errors < 1:
            raise ValueError("max_consecutive_errors must be >= 1")


@dataclass
class CycleResult:
    cycle_id: str
    index: int
    started_at: datetime
    finished_at: datetime | None = None
    symbols: list[str] = field(default_factory=list)
    decisions: list[Any] = field(default_factory=list)
    orders_submitted: int = 0
    errors: list[str] = field(default_factory=list)
    skipped_reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_record(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "index": self.index,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "symbols": list(self.symbols),
            "decisions": len(self.decisions),
            "orders_submitted": self.orders_submitted,
            "errors": list(self.errors),
            "skipped_reason": self.skipped_reason,
            "ok": self.ok,
        }


@dataclass
class RuntimeHealth:
    """Explicit runtime state, for the dashboard and for an operator."""
    state: RuntimeState = RuntimeState.IDLE
    stop_reason: StopReason = StopReason.NOT_STOPPED
    cycles_completed: int = 0
    orders_submitted: int = 0
    consecutive_errors: int = 0
    started_at: datetime | None = None
    last_cycle_at: datetime | None = None
    detail: str = ""

    def to_record(self) -> dict[str, Any]:
        return {
            "state": str(self.state),
            "stop_reason": str(self.stop_reason),
            "cycles_completed": self.cycles_completed,
            "orders_submitted": self.orders_submitted,
            "consecutive_errors": self.consecutive_errors,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "last_cycle_at": self.last_cycle_at.isoformat() if self.last_cycle_at else None,
            "detail": self.detail,
        }


class AutonomousRuntime:
    """Runs decision cycles until a bound is reached. Never unbounded.

    `cycle_fn(symbol, cycle_id)` runs ONE symbol through the full pipeline and
    returns an object exposing `.accepted` and optionally `.execution_state`.
    The runtime does not know what a strategy, a risk gate or an order is — it
    owns pacing, bounds, recovery and health, and nothing else.
    """

    def __init__(
        self,
        *,
        symbols: Sequence[str],
        cycle_fn: Callable[[str, str], Any],
        limits: RuntimeLimits | None = None,
        journal: Any = None,
        lookup: Any = None,
        market_open_fn: Callable[[], bool] | None = None,
        on_cycle: Callable[[CycleResult], None] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        if not symbols:
            raise ValueError("a runtime with no symbols would do nothing")
        self.symbols = list(symbols)
        self.cycle_fn = cycle_fn
        self.limits = limits or RuntimeLimits()
        self.limits.validate()
        self.journal = journal
        self.lookup = lookup
        self.market_open_fn = market_open_fn
        self.on_cycle = on_cycle
        self._sleep = sleep_fn
        self._clock = clock

        self.health = RuntimeHealth()
        self.cycles: list[CycleResult] = []
        self._stop_requested = False
        self._previous_handlers: dict[int, Any] = {}

    # ------------------------------------------------------------------ #
    def request_stop(self, reason: StopReason = StopReason.SIGNAL) -> None:
        """Stop AFTER the current cycle. Never mid-order.

        Interrupting between submission and journalling is precisely the
        window the write-ahead log exists to protect; there is no reason to
        create it deliberately.
        """
        self._stop_requested = True
        if self.health.stop_reason is StopReason.NOT_STOPPED:
            self.health.stop_reason = reason
        self.health.state = RuntimeState.STOPPING

    def _install_signal_handlers(self) -> None:
        def handler(signum, _frame):
            self.request_stop(StopReason.SIGNAL)
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._previous_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, handler)
            except (ValueError, OSError):
                # Not the main thread. Graceful shutdown then relies on
                # request_stop() being called directly, which is fine.
                pass

    def _restore_signal_handlers(self) -> None:
        for sig, previous in self._previous_handlers.items():
            try:
                signal.signal(sig, previous)
            except (ValueError, OSError):
                pass
        self._previous_handlers.clear()

    def _kill_switch_engaged(self) -> bool:
        path = self.limits.kill_switch_path
        return bool(path and Path(path).exists())

    def _stop_now(self) -> StopReason:
        """The bound that has been hit, if any. Checked before every cycle."""
        limits = self.limits
        if self._stop_requested:
            return self.health.stop_reason or StopReason.SIGNAL
        if self._kill_switch_engaged():
            return StopReason.KILL_SWITCH
        if limits.max_cycles is not None and self.health.cycles_completed >= limits.max_cycles:
            return StopReason.MAX_CYCLES
        if limits.max_orders is not None and self.health.orders_submitted >= limits.max_orders:
            return StopReason.MAX_ORDERS
        if limits.until is not None and self._clock() >= limits.until:
            return StopReason.DEADLINE
        if self.health.consecutive_errors >= limits.max_consecutive_errors:
            return StopReason.TOO_MANY_ERRORS
        return StopReason.NOT_STOPPED

    # ------------------------------------------------------------------ #
    def recover(self) -> Any:
        """Settle unresolved intents from a previous run. Runs before cycle 1."""
        if self.journal is None or self.lookup is None:
            return None
        from ..execution.recovery import recover as run_recovery
        self.health.state = RuntimeState.RECOVERING
        return run_recovery(self.journal, self.lookup, now=self._clock())

    def run(self) -> RuntimeHealth:
        """Recover, then cycle until a bound stops it. Always returns health."""
        self.health.started_at = self._clock()
        self._install_signal_handlers()
        try:
            report = self.recover()
            if report is not None and not report.safe_to_trade:
                # An intent we could not resolve is exactly the case where a
                # new order might duplicate a live one.
                self.health.state = RuntimeState.HALTED
                self.health.stop_reason = StopReason.UNSAFE_START
                self.health.detail = report.summary()
                return self.health

            self.health.state = RuntimeState.RUNNING
            index = 0
            while True:
                reason = self._stop_now()
                if reason is not StopReason.NOT_STOPPED:
                    self.health.stop_reason = reason
                    break

                index += 1
                result = self._run_cycle(index)
                self.cycles.append(result)
                self.health.cycles_completed += 1
                self.health.orders_submitted += result.orders_submitted
                self.health.last_cycle_at = result.finished_at
                self.health.consecutive_errors = (
                    self.health.consecutive_errors + 1 if not result.ok else 0)
                if self.on_cycle is not None:
                    try:
                        self.on_cycle(result)
                    except Exception:
                        # An observer must never be able to stop trading logic.
                        pass

                if self._stop_now() is not StopReason.NOT_STOPPED:
                    continue
                self._sleep(self.limits.interval_seconds)

            self.health.state = RuntimeState.STOPPED
            return self.health
        finally:
            self._restore_signal_handlers()

    def _run_cycle(self, index: int) -> CycleResult:
        cycle_id = f"cyc-{uuid.uuid4().hex[:12]}"
        result = CycleResult(cycle_id=cycle_id, index=index,
                             started_at=self._clock())

        if self.limits.require_market_open and self.market_open_fn is not None:
            try:
                is_open = bool(self.market_open_fn())
            except Exception as e:
                # Cannot establish market state -> do not trade. An option
                # order priced from an unknown session is exactly the stale-quote
                # failure this gate exists to prevent.
                result.errors.append(f"market clock unavailable: {type(e).__name__}: {e}")
                result.finished_at = self._clock()
                return result
            if not is_open:
                result.skipped_reason = (
                    "market closed — options do not trade outside regular hours, "
                    "and a day order queued overnight would price against a quote "
                    "that is stale by the open")
                result.finished_at = self._clock()
                return result

        for symbol in self.symbols:
            if self._stop_requested:
                break
            result.symbols.append(symbol)
            try:
                outcome = self.cycle_fn(symbol, cycle_id)
            except Exception as e:
                # Failure isolation: one symbol must not end the run, and must
                # not be silently swallowed either.
                result.errors.append(f"{symbol}: {type(e).__name__}: {e}")
                continue
            if outcome is not None:
                result.decisions.append(outcome)
                if _submitted_an_order(outcome):
                    result.orders_submitted += 1

        result.finished_at = self._clock()
        return result


def _submitted_an_order(outcome: Any) -> bool:
    """Count only outcomes that actually reached the broker.

    Counted toward max_orders on UNKNOWN as well as SUBMITTED: an ambiguous
    submission may have created an order, so it must consume the budget. Doing
    otherwise would let a string of timeouts place unlimited real orders.
    """
    state = getattr(outcome, "execution_state", None)
    return str(getattr(state, "value", state) or "") in {"submitted", "unknown"}

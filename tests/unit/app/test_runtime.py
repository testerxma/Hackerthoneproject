"""
The autonomous runtime.

An unattended loop that can place real orders is only as safe as its bounds, so
every bound is tested by trying to exceed it. The loop is driven with an
injected clock and sleep, so these are deterministic and instant.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import pytest  # noqa: E402

from speedtrader.app.runtime import (  # noqa: E402
    AutonomousRuntime, RuntimeLimits, RuntimeState, StopReason,
)
from speedtrader.execution.intent_journal import IntentJournal  # noqa: E402

NOW = datetime(2026, 9, 3, 15, 0, tzinfo=timezone.utc)


class Outcome:
    def __init__(self, state="submitted", accepted=True):
        self.execution_state = state
        self.accepted = accepted


def build(**kw):
    """A runtime that never really sleeps and has a controllable clock."""
    slept: list[float] = []
    calls: list[tuple[str, str]] = []

    def default_cycle(symbol, cycle_id):
        calls.append((symbol, cycle_id))
        return None

    limits = kw.pop("limits", RuntimeLimits(interval_seconds=1.0, max_cycles=3,
                                            require_market_open=False))
    rt = AutonomousRuntime(
        symbols=kw.pop("symbols", ["SPY"]),
        cycle_fn=kw.pop("cycle_fn", default_cycle),
        limits=limits,
        sleep_fn=kw.pop("sleep_fn", slept.append),
        clock=kw.pop("clock", lambda: NOW),
        **kw,
    )
    return rt, calls, slept


# ============================================ bounds

def test_it_stops_at_max_cycles():
    rt, calls, _ = build()
    health = rt.run()
    assert health.cycles_completed == 3
    assert health.stop_reason is StopReason.MAX_CYCLES
    assert health.state is RuntimeState.STOPPED
    assert len(calls) == 3


def test_it_stops_at_max_orders_before_starting_another_cycle():
    rt, _, _ = build(
        cycle_fn=lambda s, c: Outcome("submitted"),
        limits=RuntimeLimits(interval_seconds=1.0, max_orders=2, max_cycles=99,
                             require_market_open=False),
    )
    health = rt.run()
    assert health.orders_submitted == 2
    assert health.stop_reason is StopReason.MAX_ORDERS


def test_an_ambiguous_submission_still_consumes_the_order_budget():
    """UNKNOWN may have created a real order. If it were free, a string of
    timeouts could place unlimited orders."""
    rt, _, _ = build(
        cycle_fn=lambda s, c: Outcome("unknown"),
        limits=RuntimeLimits(interval_seconds=1.0, max_orders=1, max_cycles=99,
                             require_market_open=False),
    )
    assert rt.run().stop_reason is StopReason.MAX_ORDERS


def test_a_blocked_decision_does_not_consume_the_order_budget():
    rt, _, _ = build(
        cycle_fn=lambda s, c: Outcome("blocked", accepted=False),
        limits=RuntimeLimits(interval_seconds=1.0, max_orders=1, max_cycles=2,
                             require_market_open=False),
    )
    health = rt.run()
    assert health.orders_submitted == 0
    assert health.stop_reason is StopReason.MAX_CYCLES


def test_it_stops_at_the_deadline():
    rt, _, _ = build(
        limits=RuntimeLimits(interval_seconds=1.0, max_cycles=99,
                             until=NOW - timedelta(seconds=1),
                             require_market_open=False),
    )
    health = rt.run()
    assert health.stop_reason is StopReason.DEADLINE
    assert health.cycles_completed == 0


def test_the_kill_switch_file_stops_it(tmp_path):
    switch = tmp_path / "STOP"
    switch.write_text("")
    rt, calls, _ = build(
        limits=RuntimeLimits(interval_seconds=1.0, max_cycles=99,
                             kill_switch_path=switch, require_market_open=False),
    )
    health = rt.run()
    assert health.stop_reason is StopReason.KILL_SWITCH
    assert calls == [], "it must not run even one cycle"


def test_the_kill_switch_is_checked_between_cycles(tmp_path):
    switch = tmp_path / "STOP"
    rt, calls, _ = build(
        cycle_fn=lambda s, c: switch.write_text(""),
        limits=RuntimeLimits(interval_seconds=1.0, max_cycles=99,
                             kill_switch_path=switch, require_market_open=False),
    )
    health = rt.run()
    assert health.cycles_completed == 1, "the current cycle finishes, then it stops"
    assert health.stop_reason is StopReason.KILL_SWITCH


def test_repeated_errors_stop_the_loop_rather_than_spinning():
    def boom(symbol, cycle_id):
        raise RuntimeError("upstream down")
    rt, _, _ = build(
        cycle_fn=boom,
        limits=RuntimeLimits(interval_seconds=1.0, max_cycles=99,
                             max_consecutive_errors=2, require_market_open=False),
    )
    health = rt.run()
    assert health.stop_reason is StopReason.TOO_MANY_ERRORS
    assert health.cycles_completed == 2


def test_a_recovered_error_resets_the_consecutive_counter():
    calls = {"n": 0}

    def flaky(symbol, cycle_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return None
    rt, _, _ = build(
        cycle_fn=flaky,
        limits=RuntimeLimits(interval_seconds=1.0, max_cycles=3,
                             max_consecutive_errors=2, require_market_open=False),
    )
    health = rt.run()
    assert health.stop_reason is StopReason.MAX_CYCLES
    assert health.consecutive_errors == 0


# ============================================ failure isolation

def test_one_failing_symbol_does_not_stop_the_others():
    def selective(symbol, cycle_id):
        if symbol == "BAD":
            raise ValueError("no data")
        return Outcome("blocked", accepted=False)
    rt, _, _ = build(
        symbols=["GOOD1", "BAD", "GOOD2"], cycle_fn=selective,
        limits=RuntimeLimits(interval_seconds=1.0, max_cycles=1,
                             require_market_open=False),
    )
    rt.run()
    cycle = rt.cycles[0]
    assert cycle.symbols == ["GOOD1", "BAD", "GOOD2"]
    assert len(cycle.decisions) == 2
    assert len(cycle.errors) == 1 and "BAD" in cycle.errors[0]


def test_a_failing_observer_cannot_stop_trading():
    def bad_observer(result):
        raise RuntimeError("dashboard blew up")
    rt, _, _ = build(on_cycle=bad_observer)
    assert rt.run().cycles_completed == 3


# ============================================ market hours

def test_it_does_not_trade_when_the_market_is_closed():
    called: list[str] = []
    rt, _, _ = build(
        cycle_fn=lambda s, c: called.append(s),
        market_open_fn=lambda: False,
        limits=RuntimeLimits(interval_seconds=1.0, max_cycles=1,
                             require_market_open=True),
    )
    rt.run()
    assert called == []
    assert "market closed" in rt.cycles[0].skipped_reason


def test_an_unavailable_market_clock_is_a_no_trade_not_an_assumption():
    """Cannot establish the session -> do not price an option against it."""
    def broken():
        raise ConnectionError("clock unreachable")
    called: list[str] = []
    rt, _, _ = build(
        cycle_fn=lambda s, c: called.append(s), market_open_fn=broken,
        limits=RuntimeLimits(interval_seconds=1.0, max_cycles=1,
                             require_market_open=True),
    )
    rt.run()
    assert called == []
    assert rt.cycles[0].errors


# ============================================ pacing and shutdown

def test_it_sleeps_between_cycles_but_not_after_the_last_one():
    rt, _, slept = build()
    rt.run()
    assert slept == [1.0, 1.0], "3 cycles -> 2 gaps"


def test_request_stop_ends_the_run_gracefully():
    rt, _, _ = build(limits=RuntimeLimits(interval_seconds=1.0, max_cycles=99,
                                          require_market_open=False))
    rt.on_cycle = lambda result: rt.request_stop()
    health = rt.run()
    assert health.cycles_completed == 1
    assert health.state is RuntimeState.STOPPED


def test_each_cycle_gets_a_distinct_id_shared_by_its_symbols():
    ids: list[str] = []
    rt, _, _ = build(
        symbols=["A", "B"], cycle_fn=lambda s, c: ids.append(c),
        limits=RuntimeLimits(interval_seconds=1.0, max_cycles=2,
                             require_market_open=False),
    )
    rt.run()
    assert len(set(ids)) == 2, "two cycles, two ids"
    assert ids[0] == ids[1] and ids[2] == ids[3], "one id per cycle"


# ============================================ restart safety gate

def test_it_refuses_to_start_while_an_intent_is_unresolved(tmp_path):
    journal = IntentJournal(tmp_path)
    journal.record_attempt(client_order_id="st-x", decision_id="d", cycle_id="c",
                           symbol="SPY", quantity=1, limit_price=1.0, now=NOW)

    class Unreachable:
        def get_order_by_client_id(self, coid):
            raise ConnectionError("broker down")

    called: list[str] = []
    rt, _, _ = build(cycle_fn=lambda s, c: called.append(s),
                     journal=journal, lookup=Unreachable())
    health = rt.run()
    assert health.state is RuntimeState.HALTED
    assert health.stop_reason is StopReason.UNSAFE_START
    assert called == [], "no cycle may run with an unresolved intent"


def test_it_starts_once_intents_are_resolved(tmp_path):
    journal = IntentJournal(tmp_path)
    journal.record_attempt(client_order_id="st-x", decision_id="d", cycle_id="c",
                           symbol="SPY", quantity=1, limit_price=1.0, now=NOW)

    class NoSuchOrder:
        def get_order_by_client_id(self, coid):
            return None      # broker never saw it -> abandoned

    rt, calls, _ = build(journal=journal, lookup=NoSuchOrder())
    health = rt.run()
    assert health.state is RuntimeState.STOPPED
    assert len(calls) == 3


# ============================================ configuration validation

@pytest.mark.parametrize("kwargs", [
    {"interval_seconds": 0.5}, {"max_cycles": 0},
    {"max_orders": -1}, {"max_consecutive_errors": 0},
])
def test_a_nonsensical_bound_is_refused_at_construction(kwargs):
    with pytest.raises(ValueError):
        AutonomousRuntime(symbols=["SPY"], cycle_fn=lambda s, c: None,
                          limits=RuntimeLimits(**kwargs))


def test_a_runtime_with_no_symbols_is_refused():
    with pytest.raises(ValueError, match="no symbols"):
        AutonomousRuntime(symbols=[], cycle_fn=lambda s, c: None)


def test_health_is_serialisable_for_the_dashboard():
    rt, _, _ = build()
    record = rt.run().to_record()
    assert record["state"] == "stopped"
    assert record["cycles_completed"] == 3

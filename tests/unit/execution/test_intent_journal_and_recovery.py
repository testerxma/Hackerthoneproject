"""
Restart safety: the write-ahead intent journal and crash recovery.

The failure being tested is the expensive one. Without a record written BEFORE
the broker is contacted, a crash between "submit" and "persist the decision"
leaves a live order that nothing on disk knows about, and the next run submits a
second one. Every test here is a variation on "the process died at the worst
possible moment".
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import pytest  # noqa: E402

from speedtrader.execution.authorization import (  # noqa: E402
    AuthorizationRegistry, authorize,
)
from speedtrader.execution.intent_journal import (  # noqa: E402
    RESOLVED_PHASES, IntentJournal, IntentPhase,
)
from speedtrader.execution.options_adapter import (  # noqa: E402
    BrokerRejected, BrokerTimeout, OptionOrderRequest, OptionsExecutionAdapter,
    PositionIntent, SubmissionState,
)
from speedtrader.execution.recovery import recover  # noqa: E402

NOW = datetime(2026, 9, 3, 15, 0, tzinfo=timezone.utc)


def make_request(qty: int = 2) -> OptionOrderRequest:
    return OptionOrderRequest(
        symbol="SPY260930C00600000", quantity=qty,
        intent=PositionIntent.BUY_TO_OPEN, order_type="limit",
        limit_price=3.20, time_in_force="day",
    )


BOOK = {"equity": 100_000.0, "positions": 0}


def licence_for(request: OptionOrderRequest, decision_id: str = "d1"):
    return authorize(
        decision_id=decision_id, snapshot_id="s1",
        proposal=request.to_proposal(), portfolio=BOOK,
        approved_quantity=request.quantity, ttl_seconds=60, now=NOW,
    )


class Broker:
    """Configurable broker: succeed, raise, or return a malformed reply."""

    def __init__(self, behaviour="ok"):
        self.behaviour = behaviour
        self.calls: list[dict] = []

    def submit_option_order(self, payload):
        self.calls.append(dict(payload))
        if self.behaviour == "timeout":
            raise BrokerTimeout("no response in 5s")
        if self.behaviour == "reject":
            raise BrokerRejected("insufficient buying power")
        if self.behaviour == "boom":
            raise RuntimeError("socket exploded")
        if self.behaviour == "no_id":
            return {"status": "accepted"}
        if self.behaviour == "garbage":
            return "not a mapping"
        return {"id": "brk-1", "status": "accepted"}


class Lookup:
    """Read-only broker view keyed by client_order_id."""

    def __init__(self, orders=None, raises=False):
        self.orders = orders or {}
        self.raises = raises
        self.queried: list[str] = []

    def get_order_by_client_id(self, client_order_id):
        self.queried.append(client_order_id)
        if self.raises:
            raise ConnectionError("broker unreachable")
        return self.orders.get(client_order_id)


# ============================================ the write-ahead guarantee

def test_the_intent_is_on_disk_before_the_broker_is_called(tmp_path):
    """The whole point: if the process died inside submit_option_order, the
    attempt would already be durable."""
    journal = IntentJournal(tmp_path)
    seen: list[bool] = []

    class Observing(Broker):
        def submit_option_order(self, payload):
            # At the moment the broker is contacted, is it already recorded?
            seen.append(journal.has_attempt(payload["client_order_id"]))
            return super().submit_option_order(payload)

    request = make_request()
    OptionsExecutionAdapter(Observing(), AuthorizationRegistry(),
                            journal=journal).submit(
        request, licence_for(request), portfolio_snapshot=BOOK, now=NOW)
    assert seen == [True]


def test_a_successful_submission_records_both_attempt_and_outcome(tmp_path):
    journal = IntentJournal(tmp_path)
    request = make_request()
    result = OptionsExecutionAdapter(Broker(), AuthorizationRegistry(),
                                     journal=journal).submit(
        request, licence_for(request), portfolio_snapshot=BOOK, now=NOW)
    phases = [r.phase for r in journal.iter_records()]
    assert phases == [IntentPhase.ATTEMPTED, IntentPhase.SUBMITTED]
    assert result.state is SubmissionState.SUBMITTED


@pytest.mark.parametrize("behaviour,phase", [
    ("timeout", IntentPhase.UNKNOWN),
    ("boom", IntentPhase.UNKNOWN),
    ("no_id", IntentPhase.UNKNOWN),
    ("garbage", IntentPhase.UNKNOWN),
    ("reject", IntentPhase.REJECTED),
])
def test_every_broker_outcome_is_journalled(tmp_path, behaviour, phase):
    journal = IntentJournal(tmp_path)
    request = make_request()
    OptionsExecutionAdapter(Broker(behaviour), AuthorizationRegistry(),
                            journal=journal).submit(
        request, licence_for(request), portfolio_snapshot=BOOK, now=NOW)
    assert [r.phase for r in journal.iter_records()][-1] is phase


def test_an_unwritable_journal_blocks_the_order_rather_than_risking_it(tmp_path):
    """An order that cannot be recorded cannot be recovered, so it is not sent."""
    journal = IntentJournal(tmp_path)

    def explode(**kwargs):
        raise OSError("disk full")
    journal.record_attempt = explode

    broker = Broker()
    request = make_request()
    result = OptionsExecutionAdapter(broker, AuthorizationRegistry(),
                                     journal=journal).submit(
        request, licence_for(request), portfolio_snapshot=BOOK, now=NOW)
    assert result.state is SubmissionState.BLOCKED
    assert broker.calls == [], "the broker must never have been contacted"


def test_a_journal_write_failure_after_the_answer_does_not_mask_the_outcome(tmp_path):
    """Once the broker has answered, that answer is the important fact."""
    journal = IntentJournal(tmp_path)
    original = journal.record_outcome

    def fail_outcome(**kwargs):
        raise OSError("disk full")
    journal.record_outcome = fail_outcome

    request = make_request()
    result = OptionsExecutionAdapter(Broker(), AuthorizationRegistry(),
                                     journal=journal).submit(
        request, licence_for(request), portfolio_snapshot=BOOK, now=NOW)
    assert result.state is SubmissionState.SUBMITTED
    journal.record_outcome = original


# ============================================ duplicate submission

def test_a_client_order_id_already_in_the_journal_is_never_sent_twice(tmp_path):
    """The restart-proof duplicate guard: the journal, not process memory."""
    journal = IntentJournal(tmp_path)
    request = make_request()
    licence = licence_for(request)
    coid = f"st-{licence.nonce}"
    journal.record_attempt(client_order_id=coid, decision_id="d1", cycle_id="c1",
                           symbol=request.symbol, quantity=request.quantity,
                           limit_price=3.20, now=NOW)

    broker = Broker()
    # A FRESH registry, exactly as a restarted process would have.
    result = OptionsExecutionAdapter(broker, AuthorizationRegistry(),
                                     journal=journal).submit(
        request, licence, portfolio_snapshot=BOOK, now=NOW)
    assert result.state is SubmissionState.BLOCKED
    assert broker.calls == [], "a duplicate must never reach the broker"


# ============================================ pending / resolved accounting

def test_an_attempt_with_no_outcome_is_pending(tmp_path):
    journal = IntentJournal(tmp_path)
    journal.record_attempt(client_order_id="st-a", decision_id="d", cycle_id="c",
                           symbol="X", quantity=1, limit_price=1.0, now=NOW)
    assert [p.client_order_id for p in journal.pending()] == ["st-a"]


@pytest.mark.parametrize("phase", sorted(RESOLVED_PHASES))
def test_a_resolved_outcome_clears_the_pending_entry(tmp_path, phase):
    journal = IntentJournal(tmp_path)
    journal.record_attempt(client_order_id="st-a", decision_id="d", cycle_id="c",
                           symbol="X", quantity=1, limit_price=1.0, now=NOW)
    journal.record_outcome(client_order_id="st-a", phase=phase, now=NOW)
    assert journal.pending() == []


@pytest.mark.parametrize("phase", [IntentPhase.ATTEMPTED, IntentPhase.UNKNOWN,
                                   IntentPhase.SUBMITTED])
def test_an_unresolved_outcome_stays_pending(tmp_path, phase):
    """SUBMITTED is not resolved either: the order exists and its fate is open."""
    journal = IntentJournal(tmp_path)
    journal.record_attempt(client_order_id="st-a", decision_id="d", cycle_id="c",
                           symbol="X", quantity=1, limit_price=1.0, now=NOW)
    journal.record_outcome(client_order_id="st-a", phase=phase, now=NOW)
    assert len(journal.pending()) == 1


def test_pending_is_returned_oldest_first(tmp_path):
    journal = IntentJournal(tmp_path)
    for i, offset in enumerate([2, 0, 1]):
        journal.record_attempt(
            client_order_id=f"st-{i}", decision_id="d", cycle_id="c",
            symbol="X", quantity=1, limit_price=1.0,
            now=NOW + timedelta(minutes=offset))
    assert [p.client_order_id for p in journal.pending()] == ["st-1", "st-2", "st-0"]


def test_a_torn_final_line_is_skipped_and_leaves_the_intent_pending(tmp_path):
    """A crash mid-write must not make the log unreadable, and must not make an
    unresolved intent look resolved."""
    journal = IntentJournal(tmp_path)
    journal.record_attempt(client_order_id="st-a", decision_id="d", cycle_id="c",
                           symbol="X", quantity=1, limit_price=1.0, now=NOW)
    with journal.path.open("a") as fh:
        fh.write('{"client_order_id": "st-a", "phase": "reconc')  # truncated
    assert len(journal.pending()) == 1


# ============================================ restart recovery

def test_recovery_asks_the_broker_about_every_pending_intent(tmp_path):
    journal = IntentJournal(tmp_path)
    journal.record_attempt(client_order_id="st-a", decision_id="d", cycle_id="c",
                           symbol="SPY260930C00600000", quantity=2,
                           limit_price=3.2, now=NOW)
    lookup = Lookup({"st-a": {"id": "brk-1", "status": "filled", "qty": "2",
                              "filled_qty": "2", "filled_avg_price": "3.2",
                              "symbol": "SPY260930C00600000"}})
    report = recover(journal, lookup, now=NOW)
    assert lookup.queried == ["st-a"]
    assert report.safe_to_trade
    assert report.positions_found


def test_an_order_the_broker_never_saw_is_abandoned_not_retried(tmp_path):
    journal = IntentJournal(tmp_path)
    journal.record_attempt(client_order_id="st-a", decision_id="d", cycle_id="c",
                           symbol="X", quantity=1, limit_price=1.0, now=NOW)
    report = recover(journal, Lookup({}), now=NOW)
    assert report.recovered[0].phase is IntentPhase.ABANDONED
    assert report.safe_to_trade
    assert not report.positions_found


def test_an_unreachable_broker_blocks_trading_rather_than_assuming(tmp_path):
    """An outage is not evidence that an order is absent."""
    journal = IntentJournal(tmp_path)
    journal.record_attempt(client_order_id="st-a", decision_id="d", cycle_id="c",
                           symbol="X", quantity=1, limit_price=1.0, now=NOW)
    report = recover(journal, Lookup(raises=True), now=NOW)
    assert not report.safe_to_trade
    assert len(report.unresolved) == 1
    assert journal.pending(), "it must stay pending for the next restart"


def test_recovery_writes_its_result_so_a_second_restart_does_not_re_ask(tmp_path):
    journal = IntentJournal(tmp_path)
    journal.record_attempt(client_order_id="st-a", decision_id="d", cycle_id="c",
                           symbol="X", quantity=1, limit_price=1.0, now=NOW)
    recover(journal, Lookup({}), now=NOW)
    second = Lookup({})
    recover(journal, second, now=NOW)
    assert second.queried == [], "already-settled intents are not re-queried"


def test_recovery_cannot_place_an_order(tmp_path):
    """Structural: it is handed a read-only lookup and nothing else."""
    journal = IntentJournal(tmp_path)
    journal.record_attempt(client_order_id="st-a", decision_id="d", cycle_id="c",
                           symbol="X", quantity=1, limit_price=1.0, now=NOW)
    lookup = Lookup({})
    recover(journal, lookup, now=NOW)
    assert not hasattr(lookup, "submit_option_order")


def test_a_clean_journal_reports_a_clean_start(tmp_path):
    report = recover(IntentJournal(tmp_path), Lookup({}), now=NOW)
    assert report.safe_to_trade
    assert "clean start" in report.summary()


def test_the_journal_refuses_to_construct_when_it_cannot_be_written(tmp_path):
    """Fail at startup, not at the moment an order is about to be sent.

    Uses a path blocked by a FILE rather than by permissions: the test must
    hold for root too, and root bypasses the permission bits.
    """
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    with pytest.raises(RuntimeError, match="not writable"):
        IntentJournal(blocker / "journal")


def test_a_refused_journal_names_the_reason_it_matters(tmp_path):
    """The message has to say why this is fatal, or someone will 'fix' it by
    passing verify_writable=False."""
    blocker = tmp_path / "blocked"
    blocker.write_text("")
    with pytest.raises(RuntimeError, match="recovered after a crash"):
        IntentJournal(blocker / "journal")


# ============================================ gaps found by mutation testing
# Each test below exists because a mutant SURVIVED: the guard was real but
# nothing asserted it.

def test_the_attempt_is_fsynced_not_merely_flushed(tmp_path, monkeypatch):
    """flush() leaves the line in the OS page cache, which a power loss
    discards. The entire guarantee is that the record outlives the machine
    stopping, so the fsync is the guarantee — not an optimisation.

    Observable only by watching the call: fsync's effect cannot be seen from
    inside the process that made it.
    """
    import os as os_module
    synced: list[int] = []
    real_fsync = os_module.fsync
    monkeypatch.setattr(
        os_module, "fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1])

    IntentJournal(tmp_path).record_attempt(
        client_order_id="st-a", decision_id="d", cycle_id="c",
        symbol="X", quantity=1, limit_price=1.0, now=NOW)
    assert synced, "the write-ahead record was never fsynced"


def test_the_persisted_phase_names_are_a_stable_wire_format():
    """These strings are written to disk and read back by a LATER version of
    this code during recovery. Renaming one silently orphans every intent a
    previous build wrote, which is exactly the state recovery exists to read.
    """
    assert IntentPhase.ATTEMPTED.value == "attempted"
    assert IntentPhase.SUBMITTED.value == "submitted"
    assert IntentPhase.UNKNOWN.value == "unknown"
    assert IntentPhase.REJECTED.value == "rejected"
    assert IntentPhase.RECONCILED.value == "reconciled"
    assert IntentPhase.ABANDONED.value == "abandoned"


def test_a_corrupt_line_does_not_hide_the_records_after_it(tmp_path):
    """A torn line must be SKIPPED, not treated as end-of-file.

    Stopping at the first unreadable line would hide every intent written
    afterwards — turning one corrupt byte into a fleet of unrecovered orders.
    """
    journal = IntentJournal(tmp_path)
    journal.record_attempt(client_order_id="st-first", decision_id="d",
                           cycle_id="c", symbol="X", quantity=1,
                           limit_price=1.0, now=NOW)
    with journal.path.open("a") as fh:
        fh.write("{not valid json at all\n")
    journal.record_attempt(client_order_id="st-after", decision_id="d",
                           cycle_id="c", symbol="Y", quantity=1,
                           limit_price=1.0, now=NOW)

    ids = {p.client_order_id for p in journal.pending()}
    assert ids == {"st-first", "st-after"}, (
        "an intent written after a corrupt line must still be recovered")

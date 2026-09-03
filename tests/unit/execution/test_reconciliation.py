"""
Order reconciliation.

UNKNOWN is the most dangerous state in the system: an order may or may not be
live. Both wrong instincts lose money — assuming failure double-fills on retry,
assuming success invents a phantom position. These tests pin that neither is ever
taken, and that ambiguity escalates instead of resolving itself.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import pytest  # noqa: E402

from speedtrader.execution.reconciliation import (  # noqa: E402
    POSITION_EXISTS, TERMINAL, ReconciledState, Reconciliation,
    ReconciliationUnavailable, reconcile_all, reconcile_order,
)

NOW = datetime(2026, 9, 2, 16, 0, tzinfo=timezone.utc)
COID = "st-abc123"


class Lookup:
    def __init__(self, order=None, raises=None):
        self.order = order
        self.raises = raises
        self.queried: list[str] = []

    def get_order_by_client_id(self, client_order_id):
        self.queried.append(client_order_id)
        if self.raises:
            raise self.raises
        return self.order


def order(**kw):
    base = {"id": "ord_1", "status": "filled", "qty": "3", "filled_qty": "3",
            "filled_avg_price": "3.25", "symbol": "AAPL260930C00230000"}
    base.update(kw)
    return base


def rec(lookup, **kw):
    args = dict(client_order_id=COID, expected_quantity=3.0,
                expected_symbol="AAPL260930C00230000", now=NOW)
    args.update(kw)
    return reconcile_order(lookup, **args)


# ============================================ resolving the truth

def test_a_filled_order_is_recognised_and_holds_a_position():
    r = rec(Lookup(order()))
    assert r.state is ReconciledState.FILLED
    assert r.position_exists and r.is_terminal
    assert r.filled_quantity == 3.0
    assert r.average_fill_price == 3.25
    assert not r.safe_to_retry


def test_the_lookup_is_by_our_own_client_order_id():
    """The id is derived from the single-use nonce, so it is a stable handle for
    exactly one intended order."""
    lk = Lookup(order())
    rec(lk)
    assert lk.queried == [COID]


@pytest.mark.parametrize("status,expected", [
    ("filled", ReconciledState.FILLED),
    ("partially_filled", ReconciledState.PARTIALLY_FILLED),
    ("new", ReconciledState.OPEN),
    ("accepted", ReconciledState.OPEN),
    ("pending_new", ReconciledState.OPEN),
    ("held", ReconciledState.OPEN),
    ("canceled", ReconciledState.CANCELED),
    ("expired", ReconciledState.EXPIRED),
    ("rejected", ReconciledState.REJECTED),
])
def test_broker_statuses_map_to_terminal_truth(status, expected):
    filled = "3" if status in ("filled",) else "0"
    r = rec(Lookup(order(status=status, filled_qty=filled)))
    assert r.state is expected


def test_a_missing_order_means_the_submission_never_landed():
    """The only case besides an outright rejection where a retry is safe."""
    r = rec(Lookup(None))
    assert r.state is ReconciledState.NOT_FOUND
    assert not r.position_exists
    assert r.safe_to_retry


# ============================================ ambiguity escalates

@pytest.mark.parametrize("status", [
    "suspended", "stopped", "calculated", "done_for_day", "replaced",
    "pending_replace",
])
def test_ambiguous_broker_statuses_need_a_human(status):
    r = rec(Lookup(order(status=status)))
    assert r.state is ReconciledState.NEEDS_HUMAN
    assert not r.safe_to_retry


@pytest.mark.parametrize("status", ["", "some_new_status_alpaca_added", "???"])
def test_an_unrecognised_status_is_never_defaulted_to_benign(status):
    """A silent default would let an unknown broker state masquerade as safe."""
    r = rec(Lookup(order(status=status)))
    assert r.state is ReconciledState.NEEDS_HUMAN
    assert "not classified" in r.reason or "unreadable" in r.reason


def test_a_broker_outage_leaves_the_order_unresolved_rather_than_closed():
    with pytest.raises(ReconciliationUnavailable):
        rec(Lookup(raises=ConnectionError("broker down")))


@pytest.mark.parametrize("payload", ["a string", 42, ["list"]])
def test_an_unreadable_payload_needs_a_human(payload):
    r = rec(Lookup(payload))
    assert r.state is ReconciledState.NEEDS_HUMAN


# ============================================ discrepancies

def test_a_symbol_mismatch_is_never_benign():
    """Our id came back attached to a different instrument."""
    r = rec(Lookup(order(symbol="TSLA260930C00500000")))
    assert r.state is ReconciledState.NEEDS_HUMAN
    assert any("symbol mismatch" in d for d in r.discrepancies)


def test_a_quantity_mismatch_escalates():
    r = rec(Lookup(order(qty="30", filled_qty="30")))
    assert r.state is ReconciledState.NEEDS_HUMAN
    assert any("quantity mismatch" in d for d in r.discrepancies)


def test_an_overfill_escalates():
    r = rec(Lookup(order(qty="3", filled_qty="5")))
    assert r.state is ReconciledState.NEEDS_HUMAN
    assert any("overfill" in d for d in r.discrepancies)


def test_filled_with_zero_quantity_escalates():
    r = rec(Lookup(order(status="filled", filled_qty="0")))
    assert r.state is ReconciledState.NEEDS_HUMAN


def test_a_clean_order_records_no_discrepancies():
    assert rec(Lookup(order())).discrepancies == []


# ============================================ retry safety

@pytest.mark.parametrize("status", [
    "filled", "partially_filled", "new", "accepted", "held", "canceled",
    "expired", "suspended",
])
def test_retry_is_refused_unless_no_order_provably_exists(status):
    """Every state that has, or might still acquire, a position must block a
    retry — that is the double-fill guard."""
    r = rec(Lookup(order(status=status)))
    if r.state not in (ReconciledState.NOT_FOUND, ReconciledState.REJECTED):
        assert not r.safe_to_retry, f"{status} wrongly allowed a retry"


def test_only_not_found_and_rejected_permit_a_retry():
    assert rec(Lookup(None)).safe_to_retry
    assert rec(Lookup(order(status="rejected", filled_qty="0"))).safe_to_retry


def test_reconciliation_cannot_place_or_cancel_orders():
    """Structural: the component that decides what is true must not be able to
    change it, and it holds no authorization."""
    for forbidden in ("submit", "cancel", "retry", "amend", "authorize"):
        assert not hasattr(Reconciliation, forbidden)
    assert not any("auth" in f.lower()
                   for f in Reconciliation.__dataclass_fields__)


# ============================================ persistence

def test_the_record_is_json_serialisable():
    r = rec(Lookup(order()))
    blob = r.to_record()
    assert json.loads(json.dumps(blob)) == blob
    assert blob["state"] == "filled" and blob["position_exists"] is True


def test_the_record_states_whether_a_retry_is_safe():
    """The audit trail must carry the conclusion, not just the raw status."""
    assert rec(Lookup(None)).to_record()["safe_to_retry"] is True
    assert rec(Lookup(order())).to_record()["safe_to_retry"] is False


# ============================================ batches

def test_a_batch_reconciles_every_order():
    lk = Lookup(order())
    out = reconcile_all(lk, [
        {"client_order_id": "st-1", "quantity": 3, "symbol": "AAPL260930C00230000"},
        {"client_order_id": "st-2", "quantity": 3, "symbol": "AAPL260930C00230000"},
    ], now=NOW)
    assert len(out) == 2 and all(r.state is ReconciledState.FILLED for r in out)


def test_one_unreachable_order_does_not_abandon_the_others():
    """A transient outage must not quietly drop an order from the audit trail."""
    class Flaky:
        def __init__(self):
            self.n = 0

        def get_order_by_client_id(self, client_order_id):
            self.n += 1
            if self.n == 1:
                raise ConnectionError("transient")
            return order()

    out = reconcile_all(Flaky(), [
        {"client_order_id": "st-1", "quantity": 3},
        {"client_order_id": "st-2", "quantity": 3},
    ], now=NOW)
    assert out[0].state is ReconciledState.NEEDS_HUMAN
    assert out[1].state is ReconciledState.FILLED


def test_an_empty_client_order_id_is_recorded_not_silently_skipped():
    out = reconcile_all(Lookup(order()), [{"client_order_id": "", "quantity": 3}],
                        now=NOW)
    assert len(out) == 1 and out[0].state is ReconciledState.NEEDS_HUMAN


# ============================================ invariants

def test_position_states_are_exactly_filled_and_partially_filled():
    assert POSITION_EXISTS == {ReconciledState.FILLED,
                               ReconciledState.PARTIALLY_FILLED}


def test_needs_human_is_never_terminal():
    """It demands action; treating it as settled is how ambiguity gets buried."""
    assert ReconciledState.NEEDS_HUMAN not in TERMINAL


def test_open_is_not_terminal():
    assert ReconciledState.OPEN not in TERMINAL

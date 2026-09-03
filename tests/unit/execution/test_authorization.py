"""
Execution authorization — adversarial tests.

Every test here is an attack. The control being tested is the one that stands
between "the system reasoned its way to a trade" and "an order reached a broker",
so passing tests are not the goal: surviving the attacks is.
"""
from __future__ import annotations

import copy
import json
import pickle
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import pytest  # noqa: E402

from speedtrader.execution.authorization import (  # noqa: E402
    AuthorizationError, AuthorizationExpired, AuthorizationForged,
    AuthorizationMismatch, AuthorizationRegistry, AuthorizationReplayed,
    ExecutionAuthorization, authorize, canonical_hash, verify,
)

NOW = datetime(2026, 9, 2, 15, 0, tzinfo=timezone.utc)
PROPOSAL = {"symbol": "AAPL260930C00230000", "side": "buy_to_open",
            "quantity": 3, "order_type": "limit", "limit_price": 3.20}
PORTFOLIO = {"positions": [], "balance": 100_000.0}


def mint(**kw):
    args = dict(decision_id="dec_1", snapshot_id="snap_1", proposal=PROPOSAL,
                portfolio=PORTFOLIO, approved_quantity=3, now=NOW)
    args.update(kw)
    return authorize(**args)


def reg():
    return AuthorizationRegistry()


def check(auth, **kw):
    args = dict(proposal=PROPOSAL, portfolio=PORTFOLIO, registry=reg(), now=NOW)
    args.update(kw)
    return verify(auth, **args)


# ============================================ the happy path exists

def test_a_valid_authorization_verifies_once():
    check(mint())


def test_authorization_records_what_it_licenses():
    a = mint()
    assert a.approved_quantity == 3
    assert a.decision_id == "dec_1" and a.snapshot_id == "snap_1"
    assert a.expires_at == NOW + timedelta(seconds=30)


# ============================================ ATTACK: forgery

def test_cannot_be_constructed_outside_the_engine():
    """The single most important property: only the risk engine mints."""
    with pytest.raises(AuthorizationForged):
        ExecutionAuthorization(
            decision_id="dec_1", snapshot_id="snap_1",
            proposal_hash=canonical_hash(PROPOSAL),
            portfolio_hash=canonical_hash(PORTFOLIO),
            approved_quantity=999, expires_at=NOW + timedelta(seconds=30),
            nonce="deadbeef", _signature="forged",
        )


def test_a_tampered_signature_is_rejected():
    a = mint()
    forged = object.__new__(ExecutionAuthorization)
    for f in ("decision_id", "snapshot_id", "proposal_hash", "portfolio_hash",
              "approved_quantity", "expires_at", "nonce"):
        object.__setattr__(forged, f, getattr(a, f))
    object.__setattr__(forged, "_signature", "0" * 64)
    object.__setattr__(forged, "_token", None)
    with pytest.raises(AuthorizationForged):
        check(forged)


def test_escalating_the_approved_quantity_invalidates_the_signature():
    """Approve 3, try to submit 300."""
    a = mint()
    tampered = object.__new__(ExecutionAuthorization)
    for f in ("decision_id", "snapshot_id", "proposal_hash", "portfolio_hash",
              "expires_at", "nonce", "_signature"):
        object.__setattr__(tampered, f, getattr(a, f))
    object.__setattr__(tampered, "approved_quantity", 300)
    object.__setattr__(tampered, "_token", None)
    with pytest.raises(AuthorizationForged):
        check(tampered)


@pytest.mark.parametrize("impostor", [
    None, "not-an-authorization", 42, {"approved_quantity": 3}, object(),
])
def test_non_authorization_objects_are_refused(impostor):
    """Absence and duck-typing are both forgery."""
    with pytest.raises(AuthorizationForged):
        check(impostor)


def test_a_lookalike_class_cannot_stand_in():
    class FakeAuthorization:
        decision_id = snapshot_id = "x"
        proposal_hash = portfolio_hash = "y"
        approved_quantity = 10_000
        expires_at = NOW + timedelta(hours=1)
        nonce = "n"
        _signature = "s"
    with pytest.raises(AuthorizationForged):
        check(FakeAuthorization())


# ============================================ ATTACK: expiry

def test_an_expired_authorization_is_rejected():
    a = mint()
    with pytest.raises(AuthorizationExpired):
        check(a, now=NOW + timedelta(seconds=31))


def test_expiry_is_inclusive_at_the_boundary():
    """At exactly expires_at the licence is already dead."""
    a = mint()
    with pytest.raises(AuthorizationExpired):
        check(a, now=a.expires_at)
    check(a, now=a.expires_at - timedelta(microseconds=1))


def test_expiry_cannot_be_extended_by_mutation():
    a = mint()
    with pytest.raises(Exception):          # frozen dataclass
        a.expires_at = NOW + timedelta(days=1)


# ============================================ ATTACK: replay / duplicate orders

def test_the_same_authorization_cannot_be_used_twice():
    a, r = mint(), reg()
    verify(a, proposal=PROPOSAL, portfolio=PORTFOLIO, registry=r, now=NOW)
    with pytest.raises(AuthorizationReplayed):
        verify(a, proposal=PROPOSAL, portfolio=PORTFOLIO, registry=r, now=NOW)


def test_each_mint_gets_a_distinct_nonce():
    assert len({mint().nonce for _ in range(200)}) == 200


def test_a_failed_verification_does_not_burn_the_nonce():
    """A rejection for one reason must not silently consume a licence that was
    otherwise still valid — that would turn a recoverable error into a lost trade."""
    a, r = mint(), reg()
    with pytest.raises(AuthorizationMismatch):
        verify(a, proposal={**PROPOSAL, "quantity": 4}, portfolio=PORTFOLIO,
               registry=r, now=NOW)
    assert a.nonce not in r
    verify(a, proposal=PROPOSAL, portfolio=PORTFOLIO, registry=r, now=NOW)


# ============================================ ATTACK: substitution

@pytest.mark.parametrize("field,value", [
    ("quantity", 300), ("symbol", "TSLA260930C00500000"),
    ("side", "sell_to_open"), ("limit_price", 99.0), ("order_type", "market"),
])
def test_changing_any_part_of_the_proposal_invalidates_it(field, value):
    """Approve one order, submit a different one."""
    a = mint()
    with pytest.raises(AuthorizationMismatch):
        check(a, proposal={**PROPOSAL, field: value})


def test_adding_a_field_to_the_proposal_invalidates_it():
    a = mint()
    with pytest.raises(AuthorizationMismatch):
        check(a, proposal={**PROPOSAL, "take_profit": 6.0})


def test_a_changed_portfolio_invalidates_the_authorization():
    """The approval was computed against a book that no longer exists."""
    a = mint()
    moved = copy.deepcopy(PORTFOLIO)
    moved["positions"].append({"symbol": "MSFT", "qty": 100})
    with pytest.raises(AuthorizationMismatch):
        check(a, portfolio=moved)


def test_proposal_hash_is_order_independent():
    """Dict ordering must not create spurious mismatches."""
    reordered = {k: PROPOSAL[k] for k in reversed(list(PROPOSAL))}
    assert canonical_hash(reordered) == canonical_hash(PROPOSAL)
    check(mint(), proposal=reordered)


def test_hashing_never_raises_on_awkward_types():
    """A hash that throws would be indistinguishable from a rejection."""
    assert canonical_hash({"when": NOW, "qty": 3, "tags": {"a", "b"} and ["a"]})


# ============================================ ATTACK: leakage

def test_the_signature_never_appears_in_repr_or_str():
    a = mint()
    assert a._signature not in repr(a)
    assert a._signature not in str(a)
    assert "REDACTED" in repr(a)


def test_it_cannot_be_pickled():
    with pytest.raises(TypeError):
        pickle.dumps(mint())


def test_it_cannot_be_json_serialised():
    with pytest.raises(TypeError):
        json.dumps(mint().__dict__ if hasattr(mint(), "__dict__") else mint(),
                   default=lambda o: (_ for _ in ()).throw(TypeError()))


@pytest.mark.parametrize("method", ["model_dump", "dict"])
def test_pydantic_style_serialisation_is_blocked(method):
    """The surrounding system persists pydantic models to JSONL; inheriting that
    behaviour is exactly how a bearer token lands in an audit file."""
    with pytest.raises(TypeError):
        getattr(mint(), method)()


def test_an_authorization_is_not_a_risk_gate_result():
    """RiskGateResult explains and is persisted; this licenses and is not."""
    a = mint()
    assert not hasattr(a, "checks")
    assert not hasattr(a, "verdict")


# ============================================ ATTACK: zero / negative approvals

@pytest.mark.parametrize("qty", [0, -1, -100])
def test_refuses_to_authorize_a_non_positive_quantity(qty):
    """'Approved for zero' is not an approval; letting it exist invites a caller
    to treat the presence of an authorization as permission and supply its own size."""
    with pytest.raises(AuthorizationError):
        mint(approved_quantity=qty)


@pytest.mark.parametrize("ttl", [0, -5])
def test_refuses_a_non_positive_ttl(ttl):
    with pytest.raises(AuthorizationError):
        mint(ttl_seconds=ttl)


# ============================================ registry concurrency

def test_concurrent_consumption_admits_exactly_one_winner():
    """Two threads racing the same licence must not both submit an order."""
    import threading
    a, r = mint(), reg()
    wins, errors = [], []

    def attempt():
        try:
            verify(a, proposal=PROPOSAL, portfolio=PORTFOLIO, registry=r, now=NOW)
            wins.append(1)
        except AuthorizationReplayed:
            errors.append(1)

    threads = [threading.Thread(target=attempt) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(wins) == 1, f"{len(wins)} threads were authorized for one licence"
    assert len(errors) == 15

"""
Options execution adapter — the broker boundary.

The properties under test: no order without a valid licence, and no outcome ever
upgraded to something better than what the broker actually told us.
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
from speedtrader.execution.options_adapter import (  # noqa: E402
    BrokerRejected, BrokerTimeout, ExecutionResult, OptionOrderRequest,
    OptionsExecutionAdapter, PositionIntent, SubmissionState, limit_price_for,
)
from speedtrader.options.contracts import (  # noqa: E402
    ContractType, OptionContract, OptionQuote,
)

NOW = datetime(2026, 9, 2, 15, 0, tzinfo=timezone.utc)
PORTFOLIO = {"positions": [], "balance": 100_000.0}
REQUEST = OptionOrderRequest(symbol="AAPL260930C00230000", quantity=3,
                             limit_price=3.20)


_DEFAULT = object()          # sentinel: None is a response worth testing


class FakeBroker:
    def __init__(self, response=_DEFAULT, raises=None):
        self.response = {"id": "ord_1"} if response is _DEFAULT else response
        self.raises = raises
        self.calls: list[dict] = []

    def submit_option_order(self, payload):
        self.calls.append(dict(payload))
        if self.raises:
            raise self.raises
        return self.response


def adapter(broker=None, registry=None):
    return OptionsExecutionAdapter(broker or FakeBroker(),
                                   registry or AuthorizationRegistry())


def licence(request=REQUEST, portfolio=PORTFOLIO, qty=None):
    return authorize(decision_id="dec_1", snapshot_id="snap_1",
                     proposal=request.to_proposal(), portfolio=portfolio,
                     approved_quantity=qty if qty is not None else request.quantity,
                     now=NOW)


# ============================================ the licence gates everything

def test_a_valid_authorization_submits_exactly_once():
    b = FakeBroker()
    r = adapter(b).submit(REQUEST, licence(), portfolio_snapshot=PORTFOLIO, now=NOW)
    assert r.state is SubmissionState.SUBMITTED
    assert r.broker_order_id == "ord_1"
    assert len(b.calls) == 1


@pytest.mark.parametrize("bad", [None, "not-an-auth", 42, object()])
def test_no_valid_authorization_means_no_broker_call(bad):
    """The critical property: the broker is never contacted without a licence."""
    b = FakeBroker()
    r = adapter(b).submit(REQUEST, bad, portfolio_snapshot=PORTFOLIO, now=NOW)
    assert r.state is SubmissionState.BLOCKED
    assert b.calls == [], "an order was sent without a valid authorization"


def test_an_expired_authorization_blocks_before_the_broker():
    b = FakeBroker()
    r = adapter(b).submit(REQUEST, licence(), portfolio_snapshot=PORTFOLIO,
                          now=NOW + timedelta(seconds=31))
    assert r.state is SubmissionState.BLOCKED and "Expired" in r.reason
    assert b.calls == []


def test_a_replayed_authorization_cannot_submit_twice():
    """The duplicate-order attack."""
    b, reg = FakeBroker(), AuthorizationRegistry()
    a = licence()
    first = adapter(b, reg).submit(REQUEST, a, portfolio_snapshot=PORTFOLIO, now=NOW)
    second = adapter(b, reg).submit(REQUEST, a, portfolio_snapshot=PORTFOLIO, now=NOW)
    assert first.state is SubmissionState.SUBMITTED
    assert second.state is SubmissionState.BLOCKED
    assert len(b.calls) == 1, "a replayed licence reached the broker twice"


def test_submitting_a_different_order_than_was_authorized_is_blocked():
    """Approve 3 contracts, try to send 300."""
    b = FakeBroker()
    a = licence()
    swapped = OptionOrderRequest(symbol=REQUEST.symbol, quantity=300,
                                 limit_price=3.20)
    r = adapter(b).submit(swapped, a, portfolio_snapshot=PORTFOLIO, now=NOW)
    assert r.state is SubmissionState.BLOCKED
    assert b.calls == []


@pytest.mark.parametrize("field,value", [
    ("symbol", "TSLA260930C00500000"), ("limit_price", 0.01),
    ("order_type", "market"), ("time_in_force", "gtc"),
])
def test_any_economic_change_to_the_order_is_blocked(field, value):
    b = FakeBroker()
    a = licence()
    altered = OptionOrderRequest(**{**REQUEST.__dict__, field: value})
    r = adapter(b).submit(altered, a, portfolio_snapshot=PORTFOLIO, now=NOW)
    assert r.state is SubmissionState.BLOCKED
    assert b.calls == []


def test_a_changed_portfolio_blocks_submission():
    b = FakeBroker()
    a = licence()
    r = adapter(b).submit(REQUEST, a,
                          portfolio_snapshot={"positions": [{"s": "MSFT"}],
                                              "balance": 100_000.0}, now=NOW)
    assert r.state is SubmissionState.BLOCKED
    assert b.calls == []


def test_quantity_mismatch_with_the_licence_is_blocked_explicitly():
    """Belt and braces: the hash already covers quantity, but the invariant is
    asserted directly so it survives any change to to_proposal()."""
    b = FakeBroker()
    req = OptionOrderRequest(symbol=REQUEST.symbol, quantity=5, limit_price=3.20)
    a = authorize(decision_id="d", snapshot_id="s", proposal=req.to_proposal(),
                  portfolio=PORTFOLIO, approved_quantity=3, now=NOW)
    r = adapter(b).submit(req, a, portfolio_snapshot=PORTFOLIO, now=NOW)
    assert r.state is SubmissionState.BLOCKED
    assert "does not match the authorized" in r.reason
    assert b.calls == []


# ============================================ outcomes are never upgraded

def test_a_timeout_is_unknown_not_failed_and_not_filled():
    """The order may exist. Calling it failed invites a double-fill retry."""
    b = FakeBroker(raises=BrokerTimeout("no response in 5s"))
    r = adapter(b).submit(REQUEST, licence(), portfolio_snapshot=PORTFOLIO, now=NOW)
    assert r.state is SubmissionState.UNKNOWN
    assert r.needs_reconciliation and not r.is_filled


def test_an_unexpected_exception_is_also_unknown():
    """The request may still have been transmitted."""
    b = FakeBroker(raises=RuntimeError("socket closed mid-write"))
    r = adapter(b).submit(REQUEST, licence(), portfolio_snapshot=PORTFOLIO, now=NOW)
    assert r.state is SubmissionState.UNKNOWN
    assert "outcome unknown" in r.reason


def test_an_explicit_rejection_is_definite_and_needs_no_reconciliation():
    b = FakeBroker(raises=BrokerRejected("insufficient options level"))
    r = adapter(b).submit(REQUEST, licence(), portfolio_snapshot=PORTFOLIO, now=NOW)
    assert r.state is SubmissionState.REJECTED
    assert not r.needs_reconciliation


@pytest.mark.parametrize("response", [{}, {"status": "ok"}, {"id": None}])
def test_a_response_without_an_order_id_is_unknown_not_success(response):
    b = FakeBroker(response=response)
    r = adapter(b).submit(REQUEST, licence(), portfolio_snapshot=PORTFOLIO, now=NOW)
    assert r.state is SubmissionState.UNKNOWN


@pytest.mark.parametrize("response", ["ok", 200, ["ord_1"], None])
def test_an_unreadable_response_type_is_unknown(response):
    b = FakeBroker(response=response)
    r = adapter(b).submit(REQUEST, licence(), portfolio_snapshot=PORTFOLIO, now=NOW)
    assert r.state is SubmissionState.UNKNOWN


def test_submitted_is_never_filled():
    """§62. No path in this adapter may report a fill."""
    r = adapter().submit(REQUEST, licence(), portfolio_snapshot=PORTFOLIO, now=NOW)
    assert r.state is SubmissionState.SUBMITTED
    assert r.is_filled is False
    assert r.needs_reconciliation


def test_no_state_is_named_filled():
    assert "filled" not in {s.value for s in SubmissionState}


# ============================================ idempotency

def test_the_client_order_id_is_derived_from_the_single_use_nonce():
    b = FakeBroker()
    a = licence()
    adapter(b).submit(REQUEST, a, portfolio_snapshot=PORTFOLIO, now=NOW)
    assert b.calls[0]["client_order_id"] == f"st-{a.nonce}"


def test_distinct_authorizations_produce_distinct_client_order_ids():
    ids = set()
    for _ in range(50):
        b = FakeBroker()
        adapter(b).submit(REQUEST, licence(), portfolio_snapshot=PORTFOLIO, now=NOW)
        ids.add(b.calls[0]["client_order_id"])
    assert len(ids) == 50


def test_the_payload_sent_is_exactly_what_was_authorized():
    b = FakeBroker()
    adapter(b).submit(REQUEST, licence(), portfolio_snapshot=PORTFOLIO, now=NOW)
    sent = b.calls[0]
    for k, v in REQUEST.to_proposal().items():
        assert sent[k] == v


# ============================================ live trading is structural

def test_the_adapter_refuses_to_construct_against_a_live_account():
    with pytest.raises(ValueError, match="live"):
        OptionsExecutionAdapter(FakeBroker(), AuthorizationRegistry(), paper=False)


# ============================================ pricing

def test_a_marketable_limit_is_placed_at_the_ask():
    """A market order on a thin option book can fill far from the quote, which
    would break the max-loss figure the position was sized on."""
    c = OptionContract(symbol="X", underlying="T", type=ContractType.CALL,
                       strike=115.0, expiration=NOW.date(), multiplier=100,
                       quote=OptionQuote(bid=3.0, ask=3.2))
    assert limit_price_for(c) == 3.2
    assert limit_price_for(c, cross_spread=False) == pytest.approx(3.1)


def test_cannot_price_an_order_without_a_quote():
    c = OptionContract(symbol="X", underlying="T", type=ContractType.CALL,
                       strike=115.0, expiration=NOW.date(), multiplier=100)
    with pytest.raises(ValueError):
        limit_price_for(c)


def test_the_default_order_type_is_limit_not_market():
    assert OptionOrderRequest(symbol="X", quantity=1).order_type == "limit"
    assert OptionOrderRequest(symbol="X", quantity=1).intent is PositionIntent.BUY_TO_OPEN


def test_execution_result_carries_no_authorization():
    """The result is persisted; a licence must never ride along into JSONL."""
    r: ExecutionResult = adapter().submit(REQUEST, licence(),
                                          portfolio_snapshot=PORTFOLIO, now=NOW)
    assert not any("auth" in f.lower() or "signature" in f.lower()
                   for f in r.__dataclass_fields__)

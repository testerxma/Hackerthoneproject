"""
Alpaca MCP broker port.

The transport needs credentials and a live server, so it is not exercised here.
What IS tested is everything that decides what reaches the broker and how a
failure is interpreted — the two places a bug becomes a wrong or duplicated
order.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import pytest  # noqa: E402

from speedtrader.execution.mcp_broker import (  # noqa: E402
    AlpacaMCPBroker, MCPUnavailable, classify_failure, parse_tool_result,
    to_mcp_arguments,
)
from speedtrader.execution.options_adapter import (  # noqa: E402
    BrokerRejected, BrokerTimeout,
)

PAYLOAD = {
    "symbol": "AAPL260930C00230000",
    "quantity": 3,
    "intent": "buy_to_open",
    "order_type": "limit",
    "limit_price": 3.20,
    "time_in_force": "day",
    "client_order_id": "st-abc123",
}


# ============================================ argument mapping

def test_maps_to_the_official_tool_arguments():
    a = to_mcp_arguments(PAYLOAD)
    assert a["symbol"] == "AAPL260930C00230000"
    assert a["side"] == "buy"
    assert a["position_intent"] == "buy_to_open"
    assert a["type"] == "limit"
    assert a["time_in_force"] == "day"


def test_numeric_fields_are_sent_as_strings():
    """The MCP tool takes qty and limit_price as strings; passing floats would be
    coerced out of sight, and the number reaching Alpaca must be the number we
    authorized."""
    a = to_mcp_arguments(PAYLOAD)
    assert a["qty"] == "3" and isinstance(a["qty"], str)
    assert a["limit_price"] == "3.2" and isinstance(a["limit_price"], str)


def test_the_idempotency_key_is_carried_through_unchanged():
    """It is the single-use authorization nonce; the server treats it as an
    idempotency key that is safe to retry after a timeout."""
    assert to_mcp_arguments(PAYLOAD)["client_order_id"] == "st-abc123"


@pytest.mark.parametrize("intent,side", [
    ("buy_to_open", "buy"), ("buy_to_close", "buy"),
    ("sell_to_open", "sell"), ("sell_to_close", "sell"),
])
def test_side_is_derived_from_intent_and_intent_is_still_sent(intent, side):
    a = to_mcp_arguments({**PAYLOAD, "intent": intent})
    assert a["side"] == side
    assert a["position_intent"] == intent


def test_a_limit_order_without_a_price_is_refused():
    with pytest.raises(ValueError, match="limit_price"):
        to_mcp_arguments({**PAYLOAD, "limit_price": None})


@pytest.mark.parametrize("qty", [0, -1])
def test_a_non_positive_quantity_is_refused(qty):
    with pytest.raises(ValueError):
        to_mcp_arguments({**PAYLOAD, "quantity": qty})


def test_time_in_force_is_always_day_for_options():
    a = to_mcp_arguments({**PAYLOAD, "time_in_force": "gtc"})
    assert a["time_in_force"] == "day"


# ============================================ failure classification

@pytest.mark.parametrize("message", [
    "insufficient buying power",
    "order rejected by exchange",
    "insufficient options trading level for this strategy",
    "invalid symbol AAPL999",
    "the market is closed",
])
def test_a_definite_refusal_is_classified_rejected(message):
    assert isinstance(classify_failure(message), BrokerRejected)


@pytest.mark.parametrize("message", [
    "connection reset by peer", "timeout waiting for response",
    "500 internal server error", "", "something unexpected",
])
def test_anything_ambiguous_becomes_unknown_not_rejected(message):
    """Misclassifying ambiguity as rejection is what makes a retry double-fill,
    so the default must be the cautious one."""
    assert isinstance(classify_failure(message), BrokerTimeout)


# ============================================ result parsing

class Result:
    def __init__(self, structured=None, text=None, is_error=False):
        self.structuredContent = structured
        self.isError = is_error
        self.content = [type("B", (), {"text": text})()] if text else []


def test_a_structured_result_is_returned_as_a_mapping():
    assert parse_tool_result(Result(structured={"id": "ord_9"}))["id"] == "ord_9"


def test_a_nested_result_envelope_is_unwrapped():
    assert parse_tool_result(Result(structured={"result": {"id": "ord_9"}}))["id"] == "ord_9"


def test_a_json_text_result_is_parsed():
    assert parse_tool_result(Result(text='{"id": "ord_9"}'))["id"] == "ord_9"


def test_an_error_result_is_raised_not_returned():
    with pytest.raises(BrokerRejected):
        parse_tool_result(Result(text="order rejected", is_error=True))


def test_an_ambiguous_error_result_raises_the_unknown_variant():
    with pytest.raises(BrokerTimeout):
        parse_tool_result(Result(text="upstream unavailable", is_error=True))


def test_unparseable_output_yields_no_order_id_so_the_adapter_reports_unknown():
    """Never invent success from a reply we cannot read."""
    out = parse_tool_result(Result(text="something went sideways"))
    assert "id" not in out and "order_id" not in out


def test_an_empty_result_yields_no_order_id():
    assert parse_tool_result(Result()) == {}


# ============================================ construction safety

def test_missing_credentials_fail_closed_at_construction():
    with pytest.raises(MCPUnavailable):
        AlpacaMCPBroker(env={})


def test_live_trading_is_refused_structurally():
    with pytest.raises(ValueError, match="paper-only"):
        AlpacaMCPBroker(env={"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s"},
                        paper=False)


def test_paper_mode_is_forced_even_if_the_environment_says_otherwise():
    b = AlpacaMCPBroker(env={"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s",
                             "ALPACA_PAPER": "false"})
    assert b._env["ALPACA_PAPER"] == "true"
    assert b._env["ALPACA_PAPER_TRADE"] == "true"


def test_credentials_never_appear_in_repr():
    b = AlpacaMCPBroker(env={"ALPACA_API_KEY": "SUPERSECRETKEY",
                             "ALPACA_SECRET_KEY": "ALSOSECRET"})
    assert "SUPERSECRETKEY" not in repr(b)
    assert "ALSOSECRET" not in repr(b)


def test_it_satisfies_the_broker_port_used_by_the_adapter():
    """Structural: the adapter depends on this one method existing."""
    assert callable(AlpacaMCPBroker.submit_option_order)

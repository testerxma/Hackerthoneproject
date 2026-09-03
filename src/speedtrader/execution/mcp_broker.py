"""
SpeedTrader AI — Alpaca MCP Server broker port

Implements `BrokerPort` by calling Alpaca's OFFICIAL MCP server
(https://github.com/alpacahq/alpaca-mcp-server) tool `place_option_order`.

--------------------------------------------------------------------------------
WHY THE MCP SERVER SITS *BELOW* THE SAFETY GATE, NOT ABOVE IT
--------------------------------------------------------------------------------
Alpaca's MCP server is built so an LLM can trade "in plain English". Wired the
obvious way — model in, orders out — it is precisely the architecture this
project exists to argue against: a language model with direct broker authority
and nothing deterministic in between.

So it is used as a TRANSPORT, not as an agent's hand. The order that reaches
this module has already been:

    scored by S07 -> gated by the deterministic risk engine
    -> sized by the options risk model -> bound to a single-use
    ExecutionAuthorization

and this module only carries the resulting, already-authorized payload to
Alpaca. No LLM can reach `place_option_order` without passing every one of those
gates first, because nothing else in the system holds a reference to the broker.

The MCP server is therefore genuinely in the path — it is how orders reach the
Trading API — while the authority to trade stays deterministic.

--------------------------------------------------------------------------------
ERROR TRANSLATION IS THE POINT
--------------------------------------------------------------------------------
The adapter above distinguishes REJECTED (definite, no order exists) from
UNKNOWN (ambiguous, an order MAY exist). Getting that mapping wrong is how a
system double-fills, so a failure is only ever classified REJECTED when the
server has clearly said the order was refused. Anything else — a timeout, a
transport error, an unparseable reply — is UNKNOWN.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from typing import Any, Mapping

from .options_adapter import BrokerRejected, BrokerTimeout

#: Substrings that indicate the broker positively REFUSED the order, so no order
#: exists. Anything not matching stays ambiguous and is escalated to UNKNOWN.
_DEFINITE_REJECTION_MARKERS = (
    "insufficient buying power",
    "insufficient options trading level",
    "not permitted",
    "forbidden",
    "invalid symbol",
    "unprocessable",
    "rejected",
    "contract not found",
    "market is closed",
)


class MCPUnavailable(RuntimeError):
    """The MCP server could not be started or reached. NO EXECUTION."""


def to_mcp_arguments(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Translate an adapter payload into `place_option_order` arguments.

    Pure and separately tested: this mapping decides what actually reaches the
    broker, so it must not be entangled with transport concerns.

    The MCP tool takes qty and limit_price as STRINGS. Passing a float would be
    coerced somewhere out of our sight; converting explicitly keeps the number
    that reaches Alpaca the number we authorized.
    """
    intent = str(payload["intent"])
    qty = int(payload["quantity"])
    if qty <= 0:
        raise ValueError(f"refusing to build an order for quantity {qty}")

    args: dict[str, Any] = {
        "symbol": str(payload["symbol"]),
        "qty": str(qty),
        # buy_to_open -> buy, sell_to_close -> sell. The intent is also sent
        # verbatim so Alpaca records open vs close rather than inferring it.
        "side": "buy" if intent.startswith("buy") else "sell",
        "position_intent": intent,
        "type": str(payload.get("order_type", "limit")),
        # Options support "day" only.
        "time_in_force": "day",
        # The single-use authorization nonce. The MCP server documents this as an
        # idempotency key that is safe to retry after a timeout, which is exactly
        # the guarantee the UNKNOWN path depends on.
        "client_order_id": str(payload["client_order_id"]),
    }
    limit = payload.get("limit_price")
    if args["type"] == "limit":
        if limit is None:
            raise ValueError("a limit order requires a limit_price")
        args["limit_price"] = str(limit)
    return args


def classify_failure(message: str) -> Exception:
    """Definite refusal, or ambiguous? When in doubt, ambiguous.

    Misclassifying an ambiguous failure as a rejection is what causes a retry to
    double-fill, so the default is deliberately the cautious one.
    """
    low = (message or "").lower()
    if any(m in low for m in _DEFINITE_REJECTION_MARKERS):
        return BrokerRejected(message)
    return BrokerTimeout(f"ambiguous broker failure, outcome unknown: {message}")


class AlpacaMCPBroker:
    """BrokerPort backed by the official Alpaca MCP server over stdio.

    Credentials are passed through the environment to the child process and are
    never logged, never stored on the instance, and never included in an
    exception message.
    """

    TOOL = "place_option_order"

    def __init__(
        self,
        *,
        command: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float = 30.0,
        paper: bool = True,
    ):
        if not paper:
            raise ValueError("AlpacaMCPBroker is paper-only")
        self.command = command or shutil.which("alpaca-mcp-server") or "alpaca-mcp-server"
        self.timeout_seconds = timeout_seconds
        self._env = dict(env if env is not None else os.environ)
        if not self._env.get("ALPACA_API_KEY") or not self._env.get("ALPACA_SECRET_KEY"):
            raise MCPUnavailable(
                "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set to reach the "
                "Alpaca MCP server."
            )
        # Force paper regardless of ambient environment.
        self._env["ALPACA_PAPER_TRADE"] = "true"
        self._env["ALPACA_PAPER"] = "true"

    # ------------------------------------------------------------------ #
    def submit_option_order(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Synchronous BrokerPort entry point."""
        args = to_mcp_arguments(payload)
        try:
            return asyncio.run(self._call(args))
        except (BrokerRejected, BrokerTimeout):
            raise
        except asyncio.TimeoutError as e:
            raise BrokerTimeout(f"MCP call exceeded {self.timeout_seconds}s") from e
        except Exception as e:
            # Transport-level problems are ambiguous: the request may have been
            # transmitted before the failure.
            raise BrokerTimeout(
                f"MCP transport failure, outcome unknown: {type(e).__name__}: {e}"
            ) from e

    async def _call(self, args: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as e:      # pragma: no cover - environment dependent
            raise MCPUnavailable(
                "the 'mcp' package is required for the MCP broker: pip install mcp"
            ) from e

        params = StdioServerParameters(command=self.command, args=[], env=self._env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(),
                                       timeout=self.timeout_seconds)
                result = await asyncio.wait_for(
                    session.call_tool(self.TOOL, dict(args)),
                    timeout=self.timeout_seconds,
                )
        return parse_tool_result(result)


def parse_tool_result(result: Any) -> Mapping[str, Any]:
    """Turn an MCP CallToolResult into the mapping BrokerPort promises.

    An error result is classified rather than returned: the adapter treats a
    returned mapping without an id as UNKNOWN, but a server that explicitly said
    "rejected" deserves the definite classification.
    """
    if getattr(result, "isError", False):
        raise classify_failure(_text_of(result))

    payload = getattr(result, "structuredContent", None)
    if isinstance(payload, Mapping):
        inner = payload.get("result", payload)
        if isinstance(inner, Mapping):
            return inner

    text = _text_of(result)
    if text:
        import json
        try:
            parsed = json.loads(text)
        except ValueError:
            # Not JSON and not an explicit error: we cannot confirm an order id,
            # so the adapter must treat it as UNKNOWN rather than success.
            return {"raw_text": text}
        if isinstance(parsed, Mapping):
            return parsed
    return {}


def _text_of(result: Any) -> str:
    parts = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(str(text))
    return "\n".join(parts)

"""
SpeedTrader AI — Alpaca Client
Spec: §8 Alpaca-Only Architecture, §10 Paper Trading, §111 Failure Policy

Owns credentials and connection. Nothing else in the codebase reads ALPACA_* env vars.

PAPER ENFORCEMENT IS STRUCTURAL, NOT ADVISORY.
Going live must require editing configs/execution.yaml AND passing an explicit flag.
A single mistyped env var must never be enough to route real money through a system
whose strategies have not been validated on this asset class.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"


class AlpacaConfigError(RuntimeError):
    """Raised at startup for missing or unsafe configuration. Fails closed (§111)."""


class AlpacaUnavailable(RuntimeError):
    """Broker unreachable. Callers must treat this as NO EXECUTION, never as success."""


@dataclass(frozen=True)
class AlpacaCredentials:
    api_key: str
    secret_key: str
    paper: bool = True

    @property
    def base_url(self) -> str:
        return PAPER_URL if self.paper else LIVE_URL

    def redacted(self) -> str:
        tail = self.api_key[-4:] if len(self.api_key) >= 4 else "????"
        return f"AlpacaCredentials(key=***{tail}, paper={self.paper})"

    def __repr__(self) -> str:      # never let a key reach a log or traceback
        return self.redacted()

    __str__ = __repr__


def load_credentials(
    execution_config: Mapping[str, Any],
    *,
    env: Mapping[str, str] | None = None,
    allow_live: bool = False,
) -> AlpacaCredentials:
    """Load and validate credentials. Two independent conditions gate live trading."""
    env = env if env is not None else os.environ

    key = (env.get("ALPACA_API_KEY") or "").strip()
    secret = (env.get("ALPACA_SECRET_KEY") or "").strip()
    if not key or not secret:
        raise AlpacaConfigError(
            "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set. "
            "Copy .env.example to .env and fill them in."
        )

    configured = str(execution_config.get("environment", "paper")).lower()
    if configured not in {"paper", "live"}:
        raise AlpacaConfigError(f"execution.environment must be 'paper' or 'live', got {configured!r}")

    if configured == "live" and not allow_live:
        raise AlpacaConfigError(
            "execution.environment is 'live' but allow_live=False. Live trading requires "
            "BOTH the config change AND an explicit allow_live=True at the call site. "
            "This is deliberate: one mistake should not be enough."
        )

    env_paper = (env.get("ALPACA_PAPER", "true").strip().lower() not in {"false", "0", "no"})
    if configured == "paper" and not env_paper:
        raise AlpacaConfigError(
            "Conflict: config says paper, ALPACA_PAPER says live. Refusing to guess."
        )

    return AlpacaCredentials(api_key=key, secret_key=secret, paper=(configured == "paper"))


class AlpacaClient:
    """Thin wrapper over alpaca-py. Constructed once, shared by the data/order layers.

    alpaca-py is imported lazily so the rest of the system — schemas, quant core, risk
    engine — can be imported, tested and reasoned about without the SDK installed.
    """

    def __init__(self, credentials: AlpacaCredentials, *, feed: str = "iex"):
        self.credentials = credentials
        self.feed = feed
        self._trading = None
        self._stock_data = None

    # -- lazy SDK handles -------------------------------------------------
    @property
    def trading(self):
        if self._trading is None:
            try:
                from alpaca.trading.client import TradingClient
            except ImportError as e:  # pragma: no cover
                raise AlpacaUnavailable("alpaca-py is not installed: pip install alpaca-py") from e
            self._trading = TradingClient(
                api_key=self.credentials.api_key,
                secret_key=self.credentials.secret_key,
                paper=self.credentials.paper,
            )
        return self._trading

    @property
    def stock_data(self):
        if self._stock_data is None:
            try:
                from alpaca.data.historical import StockHistoricalDataClient
            except ImportError as e:  # pragma: no cover
                raise AlpacaUnavailable("alpaca-py is not installed: pip install alpaca-py") from e
            self._stock_data = StockHistoricalDataClient(
                api_key=self.credentials.api_key,
                secret_key=self.credentials.secret_key,
            )
        return self._stock_data

    # -- health -----------------------------------------------------------
    def ping(self) -> bool:
        """Used by the health watchdog. Never raises — returns False on any failure,
        and the caller treats False as 'no execution' (§111)."""
        try:
            self.trading.get_account()
            return True
        except Exception:
            return False

    def is_market_open(self) -> bool:
        try:
            return bool(self.trading.get_clock().is_open)
        except Exception as e:
            raise AlpacaUnavailable(f"clock unavailable: {e}") from e

    def __repr__(self) -> str:
        return f"AlpacaClient({self.credentials.redacted()}, feed={self.feed})"

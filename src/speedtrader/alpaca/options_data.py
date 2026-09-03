"""
SpeedTrader AI — Alpaca Options Data Adapter

Translates Alpaca's option contract and quote payloads into the domain objects in
`speedtrader.options.contracts`. This is the ONLY module that knows what Alpaca's
options responses look like; selection, sizing and cost stay broker-agnostic and
offline-testable.

--------------------------------------------------------------------------------
WHY THE MAPPING IS DEFENSIVE
--------------------------------------------------------------------------------
Alpaca returns several numeric fields as STRINGS (`open_interest`, `close_price`,
contract `size`). A silent str/float confusion here would not raise — it would
produce a contract whose multiplier or open interest is wrong, and the resulting
position would be sized against a max loss that does not exist. Every field is
therefore converted explicitly, and a field that cannot be converted drops the
contract rather than defaulting it.

A dropped contract is safe: selection simply has fewer candidates and fails
closed if none remain. A silently defaulted contract is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Sequence

from ..options.contracts import (
    STANDARD_CONTRACT_MULTIPLIER,
    ContractType,
    OptionContract,
    OptionQuote,
)


#: Alpaca caps the latest-quote endpoint at 100 symbols per request. Found by
#: calling the live API: a 500-contract chain returns
#: `APIError: {"message":"symbol limit is 100"}`. Quotes are therefore batched.
#: The adapter failed CLOSED when it hit this (no chain rather than an empty
#: one), which was correct but still meant no contract could ever be priced.
QUOTE_BATCH_LIMIT = 100


class OptionsDataUnavailable(RuntimeError):
    """Options data could not be obtained. Callers treat this as NO TRADE —
    never as an empty chain, which would look like 'no opportunity'."""


@dataclass(frozen=True)
class ChainRequest:
    underlying: str
    min_dte: int = 7
    max_dte: int = 60
    #: Strikes within this fraction of spot. Bounds the request; the full chain
    #: of a liquid name is thousands of contracts and most are irrelevant to an
    #: at-the-money selection.
    strike_window_pct: float = 15.0


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if f != f else f          # reject NaN


def _to_int(value: Any) -> int | None:
    f = _to_float(value)
    return None if f is None else int(f)


def _to_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _contract_type(value: Any) -> ContractType | None:
    raw = getattr(value, "value", value)
    try:
        return ContractType(str(raw).lower())
    except ValueError:
        return None


def map_contract(raw: Any, quote: Any = None) -> OptionContract | None:
    """Map one Alpaca contract (+ optional quote) to the domain object.

    Returns None when any field required to bound risk is missing or
    unconvertible. Dropping a contract is always safe; defaulting one is not.
    """
    def get(name: str) -> Any:
        if isinstance(raw, dict):
            return raw.get(name)
        return getattr(raw, name, None)

    symbol = get("symbol")
    underlying = get("underlying_symbol")
    ctype = _contract_type(get("type"))
    strike = _to_float(get("strike_price"))
    expiry = _to_date(get("expiration_date"))
    if not symbol or not underlying or ctype is None or strike is None or expiry is None:
        return None
    if strike <= 0:
        return None

    # `size` is the deliverable share count and arrives as a string. A contract
    # whose size is unreadable is dropped, never assumed to be 100: an adjusted
    # contract does not deliver 100 shares and would corrupt max-loss sizing.
    size = _to_int(get("size"))
    if size is None:
        size = STANDARD_CONTRACT_MULTIPLIER if get("size") is None else None
    if size is None or size <= 0:
        return None

    status = get("status")
    status_ok = str(getattr(status, "value", status) or "active").lower() == "active"
    tradable = bool(get("tradable")) if get("tradable") is not None else True

    return OptionContract(
        symbol=str(symbol),
        underlying=str(underlying),
        type=ctype,
        strike=strike,
        expiration=expiry,
        multiplier=size,
        quote=map_quote(quote),
        open_interest=_to_int(get("open_interest")),
        tradable=tradable and status_ok,
    )


def map_quote(raw: Any) -> OptionQuote | None:
    """Map a latest-quote payload. Returns None unless a two-sided market exists."""
    if raw is None:
        return None

    def get(name: str) -> Any:
        if isinstance(raw, dict):
            return raw.get(name)
        return getattr(raw, name, None)

    bid = _to_float(get("bid_price"))
    ask = _to_float(get("ask_price"))
    if bid is None or ask is None:
        return None
    if bid <= 0 or ask <= 0:
        # A one-sided or empty book cannot be traded against with a bounded
        # price, so it is not a quote for our purposes.
        return None
    return OptionQuote(bid=bid, ask=ask)


def _batched(items: list[Any], size: int) -> list[list[Any]]:
    """Split into request-sized chunks. Never yields an empty batch."""
    if size < 1:
        raise ValueError(f"batch size must be >= 1, got {size}")
    return [items[i:i + size] for i in range(0, len(items), size)]


def build_chain(
    contracts: Iterable[Any],
    quotes: dict[str, Any] | None = None,
) -> list[OptionContract]:
    """Map a batch, dropping anything unusable. Never raises on a bad element."""
    quotes = quotes or {}
    out: list[OptionContract] = []
    for raw in contracts:
        symbol = raw.get("symbol") if isinstance(raw, dict) else getattr(raw, "symbol", None)
        mapped = map_contract(raw, quotes.get(str(symbol)) if symbol else None)
        if mapped is not None:
            out.append(mapped)
    return out


class AlpacaOptionsData:
    """Fetches contracts and quotes. Thin on purpose: all judgement lives in
    `speedtrader.options`, so this stays a translation layer."""

    def __init__(self, trading_client: Any, data_client: Any):
        self.trading = trading_client
        self.data = data_client

    def fetch_chain(
        self, request: ChainRequest, *, spot: float, asof: date
    ) -> list[OptionContract]:
        """Contracts around the money with live quotes attached.

        Any failure raises OptionsDataUnavailable. It must never return an empty
        list on error: empty is indistinguishable from 'no contract qualified',
        which reads as a legitimate no-trade rather than a data outage.
        """
        from datetime import timedelta

        if spot <= 0:
            raise OptionsDataUnavailable(f"invalid spot price {spot}")

        lo = spot * (1 - request.strike_window_pct / 100.0)
        hi = spot * (1 + request.strike_window_pct / 100.0)
        try:
            from alpaca.trading.requests import GetOptionContractsRequest

            resp = self.trading.get_option_contracts(
                GetOptionContractsRequest(
                    underlying_symbols=[request.underlying],
                    expiration_date_gte=asof + timedelta(days=request.min_dte),
                    expiration_date_lte=asof + timedelta(days=request.max_dte),
                    strike_price_gte=str(round(lo, 2)),
                    strike_price_lte=str(round(hi, 2)),
                    limit=500,
                )
            )
        except Exception as e:
            raise OptionsDataUnavailable(
                f"could not list option contracts for {request.underlying}: "
                f"{type(e).__name__}: {e}"
            ) from e

        raw_contracts = list(getattr(resp, "option_contracts", None) or [])
        if not raw_contracts:
            return []

        symbols = [getattr(c, "symbol", None) for c in raw_contracts]
        symbols = [s for s in symbols if s]
        quotes: dict[str, Any] = {}
        if symbols:
            from alpaca.data.requests import OptionLatestQuoteRequest

            for batch in _batched(symbols, QUOTE_BATCH_LIMIT):
                try:
                    quotes.update(self.data.get_option_latest_quote(
                        OptionLatestQuoteRequest(symbol_or_symbols=batch)
                    ) or {})
                except Exception as e:
                    # One failed batch means part of the chain is unpriced, and a
                    # partially-priced chain would silently narrow selection to
                    # whichever contracts happened to load. Fail the whole fetch.
                    raise OptionsDataUnavailable(
                        f"could not fetch option quotes for a batch of "
                        f"{len(batch)}: {type(e).__name__}: {e}"
                    ) from e

        return build_chain(raw_contracts, quotes)

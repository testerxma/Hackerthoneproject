"""
SpeedTrader AI — Deterministic Options Contract Selection

    S07 says WHICH WAY and HOW HARD.
    This module says WHICH CONTRACT expresses that.

S07 is an equity momentum-breakout strategy and its formula is NOT modified to
suit options (see docs/reference/SpeedTraderBot_v6.1.mq5, read-only). It emits a
direction, an entry, a 1.5*ATR stop and a 3.0*ATR target on the UNDERLYING. This
module is the translation layer that turns that directional view into one
tradeable option contract, deterministically.

--------------------------------------------------------------------------------
WHY LONG SINGLE-LEG
--------------------------------------------------------------------------------
A long call/put was chosen over a vertical spread for one reason that is about
correctness, not convenience:

    MAXIMUM LOSS = PREMIUM PAID, known exactly at entry.

An equity stop-loss is an *intention*: price can gap through it overnight and the
realised loss is unbounded in principle. A long option cannot lose more than its
debit under any price path, so the deterministic risk model can size on an EXACT
maximum loss rather than an assumed one. That is a stronger safety property than
the equity path it replaces, not a weaker one.

Secondary reasons: one leg is one fill (a two-leg spread can leg out and leave an
unhedged position), and buying options requires the lowest options approval level.

The structure is deliberately pluggable — `Structure` — so a defined-risk vertical
debit spread can be added without touching selection, risk or execution. It is
NOT implemented yet and is not claimed to be.

--------------------------------------------------------------------------------
SELECTION IS DETERMINISTIC AND OFFLINE
--------------------------------------------------------------------------------
This module performs no network I/O. It takes contracts and quotes that were
already fetched and returns a choice, so every rule below is unit-testable
without a broker connection and reproduces exactly in a replay.

Ordering of the gates is deliberate: cheap structural filters first, liquidity
last, so a rejection reason names the most specific applicable problem.

--------------------------------------------------------------------------------
WHAT IS NOT MODELLED — STATED, NOT HIDDEN
--------------------------------------------------------------------------------
    greeks            not used. Alpaca exposes them on snapshots, but selection
                      here is by moneyness and DTE, which are exact and need no
                      pricing model. No delta target is claimed.
    early assignment  irrelevant while LONG: the holder chooses to exercise. It
                      becomes relevant the moment a short leg is introduced.
    dividends/borrow  affect option pricing; not modelled, and not needed to
                      bound max loss.
    IV / theta        a long option decays. The DTE floor exists to give the
                      underlying move time to occur; no volatility forecast is
                      made and none is implied.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Sequence

#: US listed equity options. Alpaca returns contract size as a string; 100 is the
#: standard multiplier and anything else (adjusted contracts after a corporate
#: action) is REJECTED rather than guessed at — an adjusted contract does not
#: represent 100 shares and would silently corrupt every risk number downstream.
STANDARD_CONTRACT_MULTIPLIER = 100


class Structure(StrEnum):
    """How the directional view is expressed. Only LONG_SINGLE is implemented."""
    LONG_SINGLE = "long_single"          # long call (BUY) / long put (SELL)
    VERTICAL_DEBIT = "vertical_debit"    # planned, not implemented


class ContractType(StrEnum):
    CALL = "call"
    PUT = "put"


class SelectionError(Exception):
    """No contract satisfied the rules. Always a rejection, never an exception
    that a caller may interpret as 'proceed anyway'."""


@dataclass(frozen=True)
class OptionQuote:
    """A live two-sided market for one contract."""
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def spread_pct_of_mid(self) -> float:
        m = self.mid
        return float("inf") if m <= 0 else self.spread / m * 100.0


@dataclass(frozen=True)
class OptionContract:
    """One contract plus its market. Mirrors the fields Alpaca actually returns."""
    symbol: str                       # OCC symbol, e.g. AAPL260918C00230000
    underlying: str
    type: ContractType
    strike: float
    expiration: date
    multiplier: int
    quote: OptionQuote | None = None
    open_interest: int | None = None
    tradable: bool = True

    def dte(self, asof: date) -> int:
        return (self.expiration - asof).days


@dataclass(frozen=True)
class SelectionPolicy:
    """Every threshold is explicit and configured; none is inferred at runtime."""
    min_dte: int = 7
    max_dte: int = 60
    target_dte: int = 30
    #: Reject a market wider than this. A wide market means the mid is fiction
    #: and the true entry cost is unknowable, which makes max-loss sizing wrong.
    max_spread_pct_of_mid: float = 15.0
    min_open_interest: int = 100
    #: A contract cheaper than this is usually a far-OTM lottery ticket whose
    #: quote is noise; one cent of spread is a large fraction of its value.
    min_ask: float = 0.05

    def validate(self) -> None:
        if not 0 < self.min_dte <= self.target_dte <= self.max_dte:
            raise ValueError(
                f"DTE window invalid: min={self.min_dte} target={self.target_dte} "
                f"max={self.max_dte}; require 0 < min <= target <= max"
            )
        if self.max_spread_pct_of_mid <= 0:
            raise ValueError("max_spread_pct_of_mid must be positive")
        if self.min_open_interest < 0 or self.min_ask <= 0:
            raise ValueError("min_open_interest must be >= 0 and min_ask > 0")


@dataclass(frozen=True)
class Selection:
    """The chosen contract and why. `rejected` is the audit trail of what was
    considered and discarded — a selection with no record of the alternatives
    cannot be reviewed after the fact."""
    contract: OptionContract
    structure: Structure
    reason: str
    considered: int
    rejected: dict[str, int]


def contract_type_for(direction: str) -> ContractType:
    """S07 BUY -> long call, S07 SELL -> long put.

    A long put, not a short call: the directional view is expressed with defined
    risk in both directions. Shorting to express a bearish view would create
    unbounded loss and is not permitted by this module.
    """
    d = str(direction).upper()
    if d == "BUY":
        return ContractType.CALL
    if d == "SELL":
        return ContractType.PUT
    raise SelectionError(f"direction must be BUY or SELL, got {direction!r}")


def select_contract(
    contracts: Sequence[OptionContract],
    *,
    direction: str,
    underlying_price: float,
    asof: date,
    policy: SelectionPolicy | None = None,
    structure: Structure = Structure.LONG_SINGLE,
) -> Selection:
    """Pick one contract, or raise SelectionError. Never returns a fallback.

    Tie-breaks are total and deterministic: nearest strike to spot, then nearest
    DTE to target, then the lowest OCC symbol. Two runs over the same chain
    always choose the same contract, which is what makes a replay meaningful.
    """
    if structure is not Structure.LONG_SINGLE:
        raise SelectionError(
            f"structure {structure.value!r} is not implemented; only "
            f"{Structure.LONG_SINGLE.value!r} is available"
        )
    policy = policy or SelectionPolicy()
    policy.validate()
    if underlying_price <= 0:
        raise SelectionError(f"underlying price must be positive, got {underlying_price}")

    want = contract_type_for(direction)
    rejected: dict[str, int] = {}

    def drop(reason: str) -> None:
        rejected[reason] = rejected.get(reason, 0) + 1

    eligible: list[OptionContract] = []
    for c in contracts:
        if c.type is not want:
            drop("wrong_type"); continue
        if not c.tradable:
            drop("not_tradable"); continue
        if c.multiplier != STANDARD_CONTRACT_MULTIPLIER:
            # Adjusted contracts do not deliver 100 shares; sizing on them would
            # silently misstate max loss.
            drop("non_standard_multiplier"); continue
        dte = c.dte(asof)
        if dte < policy.min_dte:
            drop("expires_too_soon"); continue
        if dte > policy.max_dte:
            drop("expires_too_late"); continue
        if c.quote is None:
            drop("no_quote"); continue
        if c.quote.bid <= 0 or c.quote.ask <= 0:
            drop("no_two_sided_market"); continue
        if c.quote.ask < c.quote.bid:
            drop("crossed_quote"); continue
        if c.quote.ask < policy.min_ask:
            drop("premium_below_floor"); continue
        if c.quote.spread_pct_of_mid > policy.max_spread_pct_of_mid:
            drop("spread_too_wide"); continue
        if c.open_interest is not None and c.open_interest < policy.min_open_interest:
            drop("insufficient_open_interest"); continue
        eligible.append(c)

    if not eligible:
        raise SelectionError(
            f"no {want.value} contract satisfied the selection policy "
            f"({len(contracts)} considered; {rejected or 'none eligible'})"
        )

    # ATM: nearest strike to spot. Highest delta per dollar of extrinsic among
    # liquid strikes, and the most liquid part of the chain.
    chosen = min(
        eligible,
        key=lambda c: (
            abs(c.strike - underlying_price),
            abs(c.dte(asof) - policy.target_dte),
            c.symbol,
        ),
    )
    return Selection(
        contract=chosen,
        structure=structure,
        reason=(
            f"nearest-the-money {want.value} at strike {chosen.strike:g} vs spot "
            f"{underlying_price:g}, {chosen.dte(asof)}d to expiry "
            f"(target {policy.target_dte}d)"
        ),
        considered=len(contracts),
        rejected=dict(sorted(rejected.items())),
    )

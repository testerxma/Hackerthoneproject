"""
SpeedTrader AI — Pre-Trade Transaction Cost Policy (price- and direction-aware)

THIS IS A PRE-TRADE ESTIMATE. IT IS NOT A REALIZED FEE.

    pre-trade  (here)     per-share, quantity-UNAWARE, priced from the proposal
                          geometry, feeds expected_value
    post-trade (future)   quantity- and notional-AWARE, applies caps, mirrors the
                          broker's daily aggregation, feeds TradeOutcome.fees

They must never share an implementation. Alpaca aggregates each fee type at the
daily, per-account level and rounds the aggregate up to the nearest cent; a
per-decision estimate structurally cannot reproduce that.

--------------------------------------------------------------------------------
WHY THE FEES ARE NOT ONE FLAT PER-SHARE NUMBER
--------------------------------------------------------------------------------
The three US equity regulatory components have three different shapes:

    SEC   sells only,   rate x TRADE VALUE      -> proportional to PRICE
    TAF   sells only,   per share, capped       -> per share, cap non-linear in qty
    CAT   buys + sells, per share               -> per share, both sides

A single constant is wrong by roughly an order of magnitude between a $12 stock
and a $900 stock, because the SEC component scales with price and the others do
not. That is the same class of error as carrying an FX pip constant into equities.

--------------------------------------------------------------------------------
WHICH LEG IS THE SELL — WHY DIRECTION MATTERS
--------------------------------------------------------------------------------
SEC and TAF are charged on the SELL side only. Which leg that is depends on the
direction of the trade, and that determines how well we can price it:

    SELL (short)   the sell leg IS the entry      -> price KNOWN      -> exact
    BUY  (long)    the sell leg is the exit       -> priced at the
                                                     take_profit, the upper of
                                                     the two outcomes  -> conservative

Pricing the long's sell leg at `take_profit` is deliberate. The real exit price
does not exist at EV time, and take_profit is the higher of the two modelled
outcomes, so the SEC component is OVERSTATED rather than mis-stated in an unknown
direction. A positive-EV gate may overstate cost safely; understating it lets a
losing trade through. Every approximation in this module is therefore chosen to
err toward MORE cost, with one documented exception below.

--------------------------------------------------------------------------------
EXACTNESS OF EACH COMPONENT -- STATED, NOT ASSUMED
--------------------------------------------------------------------------------
    CAT             EXACT         per share, both sides, no cap
    TAF             CONSERVATIVE  per-share rate applied with the per-trade cap NOT
                                  applied. Uncapped >= capped, so cost is
                                  overstated and EV understated.
    SEC on SELL     EXACT         the sell leg is the entry; the price is known
    SEC on BUY      CONSERVATIVE  sell leg priced at take_profit
    commission      depends on the configured arrangement; provenance recorded
    slippage        OPERATOR      never authoritative; no vendor publishes it
    daily rounding  NOT MODELLED  the ONE non-conservative omission. Alpaca
                                  aggregates per fee type daily per account and
                                  rounds up to the cent; that cannot be attributed
                                  to a single pre-trade decision. Surfaced on every
                                  estimate as quality["daily_rounding"] rather than
                                  hidden.

--------------------------------------------------------------------------------
ROUND TRIP
--------------------------------------------------------------------------------
One round trip is one buy and one sell.

    SEC, TAF     sell side only          -> counted ONCE
    CAT          both sides              -> counted `sides_per_round_trip` times
    commission   charged on each leg     -> per-share counted twice, and the
                                            notional rate applied to BOTH prices
    slippage     an execution assumption -> passed through as configured

--------------------------------------------------------------------------------
WHAT THIS MODULE WILL NOT DO
--------------------------------------------------------------------------------
It will not invent a rate. Every required field must be supplied explicitly, and
a missing or half-specified one raises rather than defaulting. An unexplained
operator assumption is rejected too: a number with no stated reasoning is
indistinguishable from a fabricated one.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

COST_BLOCK = "transaction_cost"

#: Must equal `transaction_cost.model` in the execution config. The two are
#: cross-checked at parse time: a config written for a different cost model than
#: the code implements is a silent-mispricing risk, not a cosmetic mismatch.
MODEL_NAME = "pre_trade_round_trip_per_share_estimate"

#: Everything the estimate knowingly leaves out, carried onto every decision.
#: Only `daily_fee_aggregation_rounding` can understate cost; the rest either
#: overstate it or are out of scope for a pre-trade per-share figure.
EXCLUSIONS = (
    "taf_per_trade_cap",                # quantity-dependent; omitting it overstates
    "daily_fee_aggregation_rounding",   # the one non-conservative omission
    "market_impact",                    # not modelled at all
    "short_borrow_cost",                # not modelled at all
)

REQUIRED_TOP = ("model", "rates_effective_date", "rates_source", "default")
REQUIRED_COMPONENTS = ("commission", "regulatory", "slippage")
REQUIRED_COMMISSION = ("per_share", "rate_of_notional", "source")
REQUIRED_REGULATORY = ("sec_rate_of_notional", "taf_per_share", "cat_per_share",
                       "sides_per_round_trip", "source")
REQUIRED_SLIPPAGE = ("per_share", "source")


class CostSource(StrEnum):
    """Whether a component came from a dated published schedule or a human."""
    AUTHORITATIVE = "authoritative"
    OPERATOR_ASSUMPTION = "operator_assumption"


class Quality(StrEnum):
    EXACT = "exact"
    CONSERVATIVE = "conservative"                # overstates cost, understates EV
    NOT_MODELLED_UNDERSTATES = "not_modelled_understates"


#: The overall grade of an estimate: conservative everywhere except the daily
#: aggregation rounding, which is not attributable to one decision.
OVERALL_QUALITY = "conservative_except_daily_rounding"


class CostPolicyError(RuntimeError):
    """Base for every cost-configuration failure. Always fail closed."""


class EVCostNotConfigured(CostPolicyError):
    """Required cost configuration is absent or incomplete.

    A subclass, so `except CostPolicyError` catches both this and invalid values
    while the two stay distinguishable: a missing policy and a nonsensical one
    are different operator mistakes.
    """


class CostPolicyInvalid(CostPolicyError):
    """Cost configuration is present but the values are not usable."""


# ==========================================================================
# Components
# ==========================================================================

@dataclass(frozen=True)
class Commission:
    """Per-share AND rate-of-notional, additively.

    Both shapes are supported at once because the fee schedule quotes commission
    as a PERCENTAGE range while a per-share arrangement is also possible; an
    account may carry either or both. Forcing one shape would require a price to
    express the other, which is exactly the conflation this module avoids.
    """
    per_share: float
    rate_of_notional: float
    source: CostSource
    assumption: str = ""

    def cost(self, entry: float, exit_price: float) -> float:
        """Commission is charged on BOTH legs of the round trip."""
        return (
            self.per_share * 2.0
            + self.rate_of_notional * (entry + exit_price)
        )


@dataclass(frozen=True)
class Regulatory:
    """US equity pass-through fees, each preserved in its own shape."""
    sec_rate_of_notional: float      # sells only, x trade value
    taf_per_share: float             # sells only, per share
    cat_per_share: float             # buys and sells, per share
    sides_per_round_trip: int
    source: CostSource
    reference: str = ""
    #: Recorded but deliberately NOT applied: the cap is quantity-dependent and
    #: quantity does not exist at EV time. Omitting it overstates cost.
    taf_cap_per_trade: float | None = None
    taf_cap_share_threshold: int | None = None

    def sec(self, sell_price: float) -> float:
        """Sell side only, once per round trip."""
        return self.sec_rate_of_notional * sell_price

    def taf(self) -> float:
        """Sell side only, once per round trip. Uncapped — see EXCLUSIONS."""
        return self.taf_per_share

    def cat(self) -> float:
        """Both legs."""
        return self.cat_per_share * self.sides_per_round_trip


@dataclass(frozen=True)
class Slippage:
    """Never authoritative: no vendor publishes another party's slippage."""
    per_share: float
    source: CostSource
    assumption: str = ""


# ==========================================================================
# Estimate
# ==========================================================================

@dataclass(frozen=True)
class CostEstimate:
    """One priced round trip. Quantity-unaware by construction.

    `per_share` is fees only. Spread is carried separately and added by
    `total_per_share()`, because a spread is a market observation from the
    snapshot, not a configured rate — conflating them would let a fee schedule
    appear to explain a liquidity cost.
    """
    direction: str
    entry: float
    exit_price: float
    sell_leg: str                    # "entry" (short) | "exit" (long)
    sell_price: float
    components: dict[str, float]
    quality: dict[str, str]
    per_share: float                 # fees only, EXCLUDING spread
    spread: float

    def total_per_share(self) -> float:
        return self.per_share + self.spread


# ==========================================================================
# Policy
# ==========================================================================

@dataclass(frozen=True)
class CostPolicy:
    commission: Commission
    regulatory: Regulatory
    slippage: Slippage
    rates_effective_date: str
    rates_source: str
    include_spread: bool = True
    symbol: str | None = None
    override_applied: bool = False

    def estimate(
        self,
        *,
        entry: float,
        take_profit: float,
        direction: str,
        spread: float = 0.0,
    ) -> CostEstimate:
        """Price one round trip from the proposal geometry.

        `direction` decides which leg is the sell, and therefore where the
        sell-only SEC and TAF components land and how exactly they can be priced.
        """
        side = str(direction).upper()
        if side not in ("BUY", "SELL"):
            raise CostPolicyInvalid(
                f"direction must be 'BUY' or 'SELL', got {direction!r}"
            )
        entry = _price(entry, "entry")
        exit_price = _price(take_profit, "take_profit")

        if side == "SELL":
            # The entry IS the sell. Its price is known, so SEC is exact.
            sell_leg, sell_price, sec_quality = "entry", entry, Quality.EXACT
        else:
            # The exit is the sell. Priced at take_profit, the upper of the two
            # modelled outcomes, so SEC is overstated rather than mis-stated.
            sell_leg, sell_price, sec_quality = "exit", exit_price, Quality.CONSERVATIVE

        components = {
            "commission": self.commission.cost(entry, exit_price),
            "sec": self.regulatory.sec(sell_price),
            "taf": self.regulatory.taf(),
            "cat": self.regulatory.cat(),
            "slippage": self.slippage.per_share,
        }
        quality = {
            "sec": sec_quality.value,
            "taf": Quality.CONSERVATIVE.value,      # cap not applied
            "cat": Quality.EXACT.value,
            "daily_rounding": Quality.NOT_MODELLED_UNDERSTATES.value,
            "overall": OVERALL_QUALITY,
        }
        return CostEstimate(
            direction=side,
            entry=entry,
            exit_price=exit_price,
            sell_leg=sell_leg,
            sell_price=sell_price,
            components=components,
            quality=quality,
            per_share=sum(components.values()),
            spread=float(spread) if self.include_spread else 0.0,
        )

    def breakdown(self, estimate: CostEstimate) -> dict[str, Any]:
        """Provenance carried onto every persisted decision.

        Plain JSON-serialisable types only: this is written to the DecisionStore
        and must survive a round trip through JSONL.
        """
        return {
            "model": MODEL_NAME,
            "rates_effective_date": self.rates_effective_date,
            "rates_source": self.rates_source,
            "direction": estimate.direction,
            "entry": estimate.entry,
            "exit_price": estimate.exit_price,
            "sell_leg": estimate.sell_leg,
            "sell_price": estimate.sell_price,
            "components": dict(estimate.components),
            "quality": dict(estimate.quality),
            "per_share_fees": estimate.per_share,
            "spread": estimate.spread,
            "total_per_share": estimate.total_per_share(),
            "provenance": {
                "commission": {
                    "source": self.commission.source.value,
                    "assumption": self.commission.assumption,
                    "per_share": self.commission.per_share,
                    "rate_of_notional": self.commission.rate_of_notional,
                },
                "regulatory": {
                    "source": self.regulatory.source.value,
                    "reference": self.regulatory.reference,
                    "sec_rate_of_notional": self.regulatory.sec_rate_of_notional,
                    "taf_per_share": self.regulatory.taf_per_share,
                    "cat_per_share": self.regulatory.cat_per_share,
                    "sides_per_round_trip": self.regulatory.sides_per_round_trip,
                    "taf_cap_per_trade": self.regulatory.taf_cap_per_trade,
                    "taf_cap_applied": False,
                },
                "slippage": {
                    "source": self.slippage.source.value,
                    "assumption": self.slippage.assumption,
                    "per_share": self.slippage.per_share,
                },
            },
            "excludes": list(EXCLUSIONS),
            "symbol": self.symbol,
            "override_applied": self.override_applied,
        }


# ==========================================================================
# Parsing -- fails closed, never substitutes a default for a missing rate
# ==========================================================================

def _require(mapping: Mapping[str, Any], keys, where: str) -> None:
    missing = [k for k in keys if k not in mapping or mapping[k] is None]
    if missing:
        raise EVCostNotConfigured(
            f"{where} is missing required key(s): {', '.join(missing)}. "
            "No CandidateSignal is produced until an explicit engineering "
            "decision supplies them. This is intentional."
        )


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CostPolicyInvalid(f"{name} must be a number, got {type(value).__name__}")
    v = float(value)
    if v != v or v in (float("inf"), float("-inf")):
        raise CostPolicyInvalid(f"{name} must be finite, got {v}")
    if v < 0:
        raise CostPolicyInvalid(f"{name} must not be negative, got {v}")
    return v


def _price(value: Any, name: str) -> float:
    v = _number(value, name)
    if v <= 0:
        raise CostPolicyInvalid(f"{name} must be positive, got {v}")
    return v


def _enum(cls, value: Any, name: str):
    try:
        return cls(value)
    except ValueError:
        raise CostPolicyInvalid(
            f"{name} must be one of {[e.value for e in cls]}, got {value!r}"
        ) from None


def _text(value: Any, name: str) -> str:
    s = str(value).strip()
    if not s:
        raise CostPolicyInvalid(f"{name} must not be empty")
    return s


def _provenance(block: Mapping[str, Any], name: str) -> tuple[CostSource, str]:
    """Resolve a component's source and its stated reasoning.

    An operator assumption without an assumption is rejected: an unexplained
    number is indistinguishable from a fabricated one, and the whole point of
    recording provenance is that the two can be told apart later.
    """
    source = _enum(CostSource, block["source"], f"{name}.source")
    assumption = str(block.get("assumption") or "").strip()
    if source is CostSource.OPERATOR_ASSUMPTION and not assumption:
        raise CostPolicyInvalid(
            f"{name}.assumption is required when {name}.source is "
            "'operator_assumption'. State why the value was chosen; an "
            "unexplained assumption cannot be audited."
        )
    return source, assumption


def _commission(block: Any) -> Commission:
    if not isinstance(block, Mapping):
        raise CostPolicyInvalid("commission must be a mapping")
    _require(block, REQUIRED_COMMISSION, f"'{COST_BLOCK}...commission'")
    source, assumption = _provenance(block, "commission")
    return Commission(
        per_share=_number(block["per_share"], "commission.per_share"),
        rate_of_notional=_number(block["rate_of_notional"],
                                 "commission.rate_of_notional"),
        source=source,
        assumption=assumption,
    )


def _regulatory(block: Any) -> Regulatory:
    if not isinstance(block, Mapping):
        raise CostPolicyInvalid("regulatory must be a mapping")
    _require(block, REQUIRED_REGULATORY, f"'{COST_BLOCK}...regulatory'")
    sides = block["sides_per_round_trip"]
    if isinstance(sides, bool) or not isinstance(sides, int) or sides < 1:
        raise CostPolicyInvalid(
            f"regulatory.sides_per_round_trip must be an integer >= 1, got {sides!r}"
        )
    source, _ = _provenance(block, "regulatory")
    cap = block.get("taf_cap_per_trade")
    thr = block.get("taf_cap_share_threshold")
    return Regulatory(
        sec_rate_of_notional=_number(block["sec_rate_of_notional"],
                                     "regulatory.sec_rate_of_notional"),
        taf_per_share=_number(block["taf_per_share"], "regulatory.taf_per_share"),
        cat_per_share=_number(block["cat_per_share"], "regulatory.cat_per_share"),
        sides_per_round_trip=sides,
        source=source,
        reference=str(block.get("reference") or ""),
        taf_cap_per_trade=None if cap is None else _number(cap, "taf_cap_per_trade"),
        taf_cap_share_threshold=None if thr is None else int(thr),
    )


def _slippage(block: Any) -> Slippage:
    if not isinstance(block, Mapping):
        raise CostPolicyInvalid("slippage must be a mapping")
    _require(block, REQUIRED_SLIPPAGE, f"'{COST_BLOCK}...slippage'")
    source, assumption = _provenance(block, "slippage")
    if source is CostSource.AUTHORITATIVE:
        raise CostPolicyInvalid(
            "slippage.source cannot be 'authoritative': no vendor publishes "
            "another party's slippage. It is an operator assumption until it is "
            "a measurement from your own execution history."
        )
    return Slippage(
        per_share=_number(block["per_share"], "slippage.per_share"),
        source=source,
        assumption=assumption,
    )


def cost_policy_from_config(
    execution_config: Mapping[str, Any], *, symbol: str | None = None
) -> CostPolicy:
    """Build a policy, or fail closed."""
    if COST_BLOCK not in execution_config or execution_config[COST_BLOCK] is None:
        raise EVCostNotConfigured(
            f"'{COST_BLOCK}' block is absent from the execution configuration. "
            "No CandidateSignal can be produced. This is intentional."
        )
    block = execution_config[COST_BLOCK]
    if not isinstance(block, Mapping):
        raise CostPolicyInvalid(f"'{COST_BLOCK}' must be a mapping")

    _require(block, REQUIRED_TOP, f"'{COST_BLOCK}'")

    declared = _text(block["model"], f"{COST_BLOCK}.model")
    if declared != MODEL_NAME:
        raise CostPolicyInvalid(
            f"'{COST_BLOCK}.model' is {declared!r} but this module implements "
            f"{MODEL_NAME!r}. A configuration written for a different cost model "
            "would be mispriced silently; resolve the mismatch explicitly."
        )

    default = block["default"]
    if not isinstance(default, Mapping):
        raise CostPolicyInvalid(f"'{COST_BLOCK}.default' must be a mapping")

    # A per-symbol override REPLACES the default wholesale; it never inherits
    # component-by-component. Silently inheriting half a component under an
    # override key is the ambiguity this module exists to prevent, so an override
    # that omits a component fails closed exactly like an incomplete default.
    source_block: Mapping[str, Any] = default
    where = f"'{COST_BLOCK}.default'"
    override_applied = False
    overrides = block.get("overrides") or {}
    if symbol and isinstance(overrides, Mapping) and symbol.upper() in overrides:
        ov = overrides[symbol.upper()]
        if not isinstance(ov, Mapping):
            raise CostPolicyInvalid(f"override for {symbol} must be a mapping")
        source_block = ov
        where = f"'{COST_BLOCK}.overrides.{symbol.upper()}'"
        override_applied = True

    _require(source_block, REQUIRED_COMPONENTS, where)

    return CostPolicy(
        commission=_commission(source_block["commission"]),
        regulatory=_regulatory(source_block["regulatory"]),
        slippage=_slippage(source_block["slippage"]),
        rates_effective_date=_text(block["rates_effective_date"],
                                   "rates_effective_date"),
        rates_source=_text(block["rates_source"], "rates_source"),
        include_spread=bool(execution_config.get("include_spread_in_ev_cost", True)),
        symbol=symbol.upper() if symbol else None,
        override_applied=override_applied,
    )

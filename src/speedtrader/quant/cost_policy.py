"""
SpeedTrader AI — Pre-Trade Transaction Cost Policy (price-aware)

THIS IS A PRE-TRADE ESTIMATE. IT IS NOT A REALIZED FEE.

    pre-trade  (here)     per-share at a known price, quantity-UNAWARE, feeds EV
    post-trade (future)   quantity- and notional-AWARE, daily-aggregated,
                          feeds TradeOutcome.fees

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

WHAT IS KNOWABLE AT EV TIME
    price     KNOWN     (CandidateSignal.entry)
    quantity  UNKNOWN   (produced later by risk/measures.size_position)

So a notional-based fee IS expressible per share:  sec_per_share = rate * price.
Only genuinely quantity-dependent mechanics -- the TAF per-trade cap and daily
aggregation -- remain out of reach.

--------------------------------------------------------------------------------
EXACTNESS OF EACH COMPONENT -- STATED, NOT ASSUMED
--------------------------------------------------------------------------------
    CAT          EXACT         per share, both sides, no cap
    TAF          CONSERVATIVE  per-share rate applied with the per-trade cap NOT
                               applied. Uncapped >= capped, so cost is overstated
                               and EV understated: the safe direction for a
                               positive-EV gate.
    SEC          APPROXIMATE   sell-side notional priced at ENTRY. The real sell
                               happens at the exit price, which does not exist at
                               EV time. Error is bounded by the price excursion.
    commission   depends on the configured model; provenance recorded.
    slippage     OPERATOR      never authoritative; no vendor publishes it.

--------------------------------------------------------------------------------
ROUND TRIP
--------------------------------------------------------------------------------
One round trip is one buy and one sell, so exactly ONE sell side regardless of
direction. Sell-only fees (SEC, TAF) are counted once. CAT is counted
`sides_per_round_trip` times.

--------------------------------------------------------------------------------
COST BASIS: live_economics
--------------------------------------------------------------------------------
The execution environment is Alpaca Paper Trading, which charges no real fees.
EV nevertheless models LIVE execution economics, not simulator charges. Zeroing
costs because the simulator does not bill them would make EV describe the
simulator rather than the strategy, and the positive-EV gate would measure
nothing. Recorded on every decision as `cost_basis`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

COST_BLOCK = "transaction_cost"
MODEL_ID = "pre_trade_estimate_v2_price_aware"

REQUIRED_TOP = ("cost_basis", "rates_effective_date", "rates_source",
                "commission", "regulatory", "slippage")
REQUIRED_COMMISSION = ("model", "value", "source")
REQUIRED_REGULATORY = ("sec_rate_of_notional", "taf_per_share", "cat_per_share",
                       "sides_per_round_trip", "source")
REQUIRED_SLIPPAGE = ("per_share", "source", "assumption")


class CostSource(StrEnum):
    """Whether a component came from a dated published schedule or a human."""
    AUTHORITATIVE = "authoritative"
    OPERATOR_ASSUMPTION = "operator_assumption"


class Exactness(StrEnum):
    EXACT = "exact"
    CONSERVATIVE = "conservative"    # overstates cost, understates EV
    APPROXIMATE = "approximate"      # error not signed


class CommissionModel(StrEnum):
    NONE = "none"
    PER_SHARE = "per_share"
    RATE_OF_NOTIONAL = "rate_of_notional"


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
    """Per-share OR rate-of-notional. Both supported because Alpaca expresses
    commissions as a percentage range, and a percentage cannot populate a
    per-share field without a price."""
    model: CommissionModel
    value: float
    source: CostSource
    note: str = ""

    def per_share(self, price: float) -> float:
        if self.model is CommissionModel.NONE:
            return 0.0
        if self.model is CommissionModel.PER_SHARE:
            return self.value
        return self.value * price       # RATE_OF_NOTIONAL


@dataclass(frozen=True)
class Regulatory:
    """US equity pass-through fees, each preserved in its own shape."""
    sec_rate_of_notional: float      # sells only, x trade value
    taf_per_share: float             # sells only, per share
    cat_per_share: float             # buys and sells, per share
    sides_per_round_trip: int
    source: CostSource
    #: Recorded but deliberately NOT applied: the cap is quantity-dependent and
    #: quantity does not exist at EV time. Omitting it overstates cost.
    taf_cap_per_trade: float | None = None
    taf_cap_share_threshold: int | None = None

    def sec_per_share(self, price: float) -> float:
        return self.sec_rate_of_notional * price

    def per_share(self, price: float) -> float:
        return (
            self.sec_per_share(price)                         # sell side, once
            + self.taf_per_share                              # sell side, once
            + self.cat_per_share * self.sides_per_round_trip  # both sides
        )

    def breakdown(self, price: float) -> dict[str, Any]:
        return {
            "sec_per_share": self.sec_per_share(price),
            "sec_rate_of_notional": self.sec_rate_of_notional,
            "sec_priced_at": price,
            "sec_applies": "sell_side_only",
            "sec_exactness": Exactness.APPROXIMATE.value,
            "sec_approximation_reason":
                "sell-side notional priced at entry; the exit price does not "
                "exist when EV is computed",
            "taf_per_share": self.taf_per_share,
            "taf_applies": "sell_side_only",
            "taf_exactness": Exactness.CONSERVATIVE.value,
            "taf_cap_per_trade": self.taf_cap_per_trade,
            "taf_cap_share_threshold": self.taf_cap_share_threshold,
            "taf_cap_applied": False,
            "taf_cap_reason":
                "cap is quantity-dependent and quantity is unknown pre-trade; "
                "uncapped >= capped, so cost is overstated",
            "cat_per_share": self.cat_per_share,
            "cat_sides": self.sides_per_round_trip,
            "cat_applies": "buy_and_sell",
            "cat_exactness": Exactness.EXACT.value,
            "total_per_share": self.per_share(price),
            "source": self.source.value,
        }


@dataclass(frozen=True)
class Slippage:
    """Never authoritative: no vendor publishes another party's slippage."""
    per_share: float
    source: CostSource
    assumption: str


# ==========================================================================
# Policy
# ==========================================================================

@dataclass(frozen=True)
class CostPolicy:
    commission: Commission
    regulatory: Regulatory
    slippage: Slippage
    cost_basis: str
    rates_effective_date: str
    rates_source: str
    include_spread: bool = True
    symbol: str | None = None
    override_applied: bool = False

    def per_share_cost(self, price: float) -> float:
        """Round-trip per-share cost at `price`, EXCLUDING spread.

        Spread is a market observation from the snapshot, not a configured rate.
        Conflating them would let a fee schedule appear to explain a liquidity cost.
        """
        if price <= 0:
            raise CostPolicyInvalid(f"price must be positive, got {price}")
        return (
            self.commission.per_share(price)
            + self.regulatory.per_share(price)
            + self.slippage.per_share
        )

    def breakdown(self, price: float, spread: float = 0.0) -> dict[str, Any]:
        """Provenance carried onto every persisted decision."""
        return {
            "model": MODEL_ID,
            "cost_basis": self.cost_basis,
            "priced_at": price,
            "spread": spread,
            "commission_per_share": self.commission.per_share(price),
            "commission_model": self.commission.model.value,
            "commission_value": self.commission.value,
            "commission_source": self.commission.source.value,
            "commission_note": self.commission.note,
            "regulatory": self.regulatory.breakdown(price),
            "slippage_per_share": self.slippage.per_share,
            "slippage_source": self.slippage.source.value,
            "slippage_assumption": self.slippage.assumption,
            "fixed": self.per_share_cost(price),
            "rates_effective_date": self.rates_effective_date,
            "rates_source": self.rates_source,
            "symbol": self.symbol,
            "override_applied": self.override_applied,
            "excludes": [
                "taf_per_trade_cap",
                "daily_fee_aggregation_and_round_up_to_cent",
                "exit_price_for_sec_notional",
                "market_impact",
            ],
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


def _commission(block: Any) -> Commission:
    if not isinstance(block, Mapping):
        raise CostPolicyInvalid("commission must be a mapping")
    _require(block, REQUIRED_COMMISSION, f"'{COST_BLOCK}.commission'")
    model = _enum(CommissionModel, block["model"], "commission.model")
    value = _number(block["value"], "commission.value")
    if model is CommissionModel.NONE and value != 0.0:
        raise CostPolicyInvalid(
            "commission.model 'none' requires value 0.0; a non-zero value with "
            "model 'none' is ambiguous"
        )
    return Commission(
        model=model, value=value,
        source=_enum(CostSource, block["source"], "commission.source"),
        note=str(block.get("note") or ""),
    )


def _regulatory(block: Any) -> Regulatory:
    if not isinstance(block, Mapping):
        raise CostPolicyInvalid("regulatory must be a mapping")
    _require(block, REQUIRED_REGULATORY, f"'{COST_BLOCK}.regulatory'")
    sides = block["sides_per_round_trip"]
    if isinstance(sides, bool) or not isinstance(sides, int) or sides < 1:
        raise CostPolicyInvalid(
            f"regulatory.sides_per_round_trip must be an integer >= 1, got {sides!r}"
        )
    cap = block.get("taf_cap_per_trade")
    thr = block.get("taf_cap_share_threshold")
    return Regulatory(
        sec_rate_of_notional=_number(block["sec_rate_of_notional"],
                                     "regulatory.sec_rate_of_notional"),
        taf_per_share=_number(block["taf_per_share"], "regulatory.taf_per_share"),
        cat_per_share=_number(block["cat_per_share"], "regulatory.cat_per_share"),
        sides_per_round_trip=sides,
        source=_enum(CostSource, block["source"], "regulatory.source"),
        taf_cap_per_trade=None if cap is None else _number(cap, "taf_cap_per_trade"),
        taf_cap_share_threshold=None if thr is None else int(thr),
    )


def _slippage(block: Any) -> Slippage:
    if not isinstance(block, Mapping):
        raise CostPolicyInvalid("slippage must be a mapping")
    _require(block, REQUIRED_SLIPPAGE, f"'{COST_BLOCK}.slippage'")
    source = _enum(CostSource, block["source"], "slippage.source")
    if source is CostSource.AUTHORITATIVE:
        raise CostPolicyInvalid(
            "slippage.source cannot be 'authoritative': no vendor publishes "
            "another party's slippage. It is an operator assumption until it is "
            "a measurement from your own execution history."
        )
    return Slippage(
        per_share=_number(block["per_share"], "slippage.per_share"),
        source=source,
        assumption=_text(block["assumption"], "slippage.assumption"),
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

    merged: dict[str, Any] = {k: block[k]
                              for k in ("commission", "regulatory", "slippage")}
    override_applied = False
    overrides = block.get("overrides") or {}
    if symbol and isinstance(overrides, Mapping) and symbol.upper() in overrides:
        ov = overrides[symbol.upper()]
        if not isinstance(ov, Mapping):
            raise CostPolicyInvalid(f"override for {symbol} must be a mapping")
        # A whole component is replaced, never merged field-by-field: silently
        # inheriting half a component under an override key is the ambiguity this
        # module exists to prevent.
        for comp in ("commission", "regulatory", "slippage"):
            if comp in ov:
                merged[comp] = ov[comp]
                override_applied = True

    return CostPolicy(
        commission=_commission(merged["commission"]),
        regulatory=_regulatory(merged["regulatory"]),
        slippage=_slippage(merged["slippage"]),
        cost_basis=_text(block["cost_basis"], "cost_basis"),
        rates_effective_date=_text(block["rates_effective_date"],
                                   "rates_effective_date"),
        rates_source=_text(block["rates_source"], "rates_source"),
        include_spread=bool(execution_config.get("include_spread_in_ev_cost", True)),
        symbol=symbol.upper() if symbol else None,
        override_applied=override_applied,
    )

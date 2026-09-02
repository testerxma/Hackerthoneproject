"""
SpeedTrader AI — Options Position Sizing (deterministic)

    Equities:  shares   = risk_money / stop_distance
    Options:   contracts = risk_money / (premium * multiplier)

These are NOT the same formula with different names, and the equity one must
never be reused here. Its denominator is a *price distance to an intended stop*;
this one's denominator is the *entire amount at risk*. Substituting one for the
other overstates position size by roughly the ratio of premium to stop distance.

--------------------------------------------------------------------------------
MAX LOSS IS EXACT, NOT ASSUMED
--------------------------------------------------------------------------------
For a long option the maximum loss is the debit paid, under every price path,
including a gap through the underlying stop. So:

    max_loss_per_contract = entry_premium * multiplier

and the risk budget divides by that exactly. There is no slippage-through-stop
term because there is no stop to slip through. This is the one place where the
options expression is quantitatively SAFER than the equity original, and the
sizing math should say so plainly rather than carrying over a stop-based
approximation that no longer applies.

--------------------------------------------------------------------------------
PRICED AT THE ASK
--------------------------------------------------------------------------------
Sizing uses the ASK, not the mid. We are buying; the ask is the price actually
payable now, the mid is a price nobody is obliged to fill. Using the mid would
understate max loss by half the spread on every position, which is the
non-conservative direction.

--------------------------------------------------------------------------------
THE UNDERLYING STOP AND TARGET DO NOT VANISH
--------------------------------------------------------------------------------
S07's 1.5*ATR stop and 3.0*ATR target still describe the thesis on the
UNDERLYING, and they are carried through onto the proposal so the position can
be managed and the decision audited against the original signal. They are
deliberately NOT used to size the option, and they are NOT translated into an
option premium via a pricing model — doing so would invent a volatility
assumption this system has not earned.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .contracts import OptionContract


class OptionsRiskError(Exception):
    """Programmer error only. A rejection is a result, never an exception."""


@dataclass(frozen=True)
class OptionsSizingPolicy:
    risk_per_trade_pct: float = 1.0
    #: Hard ceiling regardless of the risk budget. A cheap contract can otherwise
    #: produce an enormous contract count that is fine on paper and unfillable
    #: in practice.
    max_contracts: int = 50
    #: Cap on total premium as a share of account balance, applied after the
    #: per-trade budget. Bounds concentration when several positions are open.
    max_premium_pct_of_balance: float = 5.0

    def validate(self) -> None:
        if not 0 < self.risk_per_trade_pct <= 100:
            raise ValueError("risk_per_trade_pct must be in (0, 100]")
        if self.max_contracts < 1:
            raise ValueError("max_contracts must be >= 1")
        if not 0 < self.max_premium_pct_of_balance <= 100:
            raise ValueError("max_premium_pct_of_balance must be in (0, 100]")


@dataclass(frozen=True)
class OptionsSizing:
    """The result. `quantity == 0` means REJECT, and the reason says why."""
    quantity: int
    premium_per_contract: float          # per share of the deliverable
    max_loss_per_contract: float         # premium * multiplier
    total_debit: float                   # what leaves the account now
    max_loss_total: float                # identical to total_debit for a long option
    risk_budget: float
    multiplier: int
    reason: str = ""
    caps_applied: list[str] = field(default_factory=list)

    @property
    def approved(self) -> bool:
        return self.quantity > 0


def size_option_position(
    *,
    contract: OptionContract,
    account_balance: float,
    policy: OptionsSizingPolicy | None = None,
    open_premium: float = 0.0,
) -> OptionsSizing:
    """Contracts affordable within the risk budget. Fails closed to quantity 0.

    `open_premium` is premium already committed to open option positions, so the
    concentration cap accounts for the whole book rather than this trade alone.
    """
    policy = policy or OptionsSizingPolicy()
    policy.validate()

    if contract.quote is None:
        return _reject("contract has no quote; cannot bound max loss", contract, 0.0)
    ask = contract.quote.ask
    if ask <= 0:
        return _reject(f"non-positive ask {ask}", contract, 0.0)
    if contract.multiplier <= 0:
        raise OptionsRiskError(f"invalid multiplier {contract.multiplier}")
    if account_balance <= 0:
        return _reject("non-positive account balance", contract, 0.0)

    risk_budget = account_balance * policy.risk_per_trade_pct / 100.0
    max_loss_per_contract = ask * contract.multiplier

    caps: list[str] = []
    qty = math.floor(risk_budget / max_loss_per_contract)

    if qty < 1:
        # One contract already exceeds the per-trade risk budget. This is a
        # REJECT, never a rounding-up to one.
        return _reject(
            f"one contract risks {max_loss_per_contract:.2f}, over the "
            f"{risk_budget:.2f} budget ({policy.risk_per_trade_pct}% of balance)",
            contract, risk_budget,
        )

    if qty > policy.max_contracts:
        qty = policy.max_contracts
        caps.append(f"max_contracts={policy.max_contracts}")

    # Concentration cap across the whole option book.
    allowed_premium = account_balance * policy.max_premium_pct_of_balance / 100.0
    remaining = allowed_premium - open_premium
    if remaining <= 0:
        return _reject(
            f"open option premium {open_premium:.2f} already at the "
            f"{policy.max_premium_pct_of_balance}% concentration cap",
            contract, risk_budget,
        )
    affordable = math.floor(remaining / max_loss_per_contract)
    if affordable < qty:
        qty = affordable
        caps.append(f"max_premium_pct_of_balance={policy.max_premium_pct_of_balance}")
    if qty < 1:
        return _reject(
            f"concentration cap leaves {remaining:.2f}, below one contract at "
            f"{max_loss_per_contract:.2f}",
            contract, risk_budget,
        )

    total = qty * max_loss_per_contract
    return OptionsSizing(
        quantity=qty,
        premium_per_contract=ask,
        max_loss_per_contract=max_loss_per_contract,
        total_debit=total,
        # For a LONG option these are the same number. They are reported
        # separately because they stop being the same the moment a short leg is
        # added, and a caller must never learn to treat them as interchangeable.
        max_loss_total=total,
        risk_budget=risk_budget,
        multiplier=contract.multiplier,
        reason=(
            f"{qty} contract(s) at {ask:.2f} x{contract.multiplier} = "
            f"{total:.2f} max loss, within a {risk_budget:.2f} budget"
        ),
        caps_applied=caps,
    )


def _reject(reason: str, contract: OptionContract, budget: float) -> OptionsSizing:
    ask = contract.quote.ask if contract.quote else 0.0
    return OptionsSizing(
        quantity=0,
        premium_per_contract=ask,
        max_loss_per_contract=ask * contract.multiplier,
        total_debit=0.0,
        max_loss_total=0.0,
        risk_budget=budget,
        multiplier=contract.multiplier,
        reason=reason,
    )

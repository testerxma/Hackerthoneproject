"""
SpeedTrader AI — Options Round-Trip Cost (pre-trade estimate)

Equity fees do NOT apply to options and the equity model must not be reused: it
is per SHARE, options fees are per CONTRACT, and options carry two fees
(ORF, OCC) that have no equity equivalent at all. Reusing it would understate
options cost by roughly two orders of magnitude per contract.

Rates transcribed from the Alpaca Brokerage Fee Schedule, revised 2026-09-01
(page 3, "Options"), read from the PDF text — see
docs/decisions/0001-transaction-cost-research.md.

    SEC Transaction Fee   sells only        $0.0000206 x Trade Value
    FINRA TAF             sells only        $0.00329  per contract
    FINRA CAT             buys and sells    $0.000003 per equivalent share
                                            (1 contract = 100 equiv. shares)
    ORF                   buys and sells    $0.015    per contract
    OCC Clearing          buys and sells    $0.025    per contract

INDEX OPTIONS ARE OUT OF SCOPE. The schedule adds a $0.50/contract Alpaca
commission on US-listed index options. This module REFUSES to price them rather
than silently omitting that commission — see `index_option=True`.

--------------------------------------------------------------------------------
THE ONE ASSUMPTION, NAMED
--------------------------------------------------------------------------------
SEC is charged on the SELL, whose trade value is the EXIT premium — unknown at
entry. This system deliberately models no option pricing (no volatility
assumption has been earned), so the exit premium cannot be derived.

It is therefore priced at `exit_premium_multiple` x entry premium, defaulting to
2.0 — the modelled winning exit, i.e. the upper of the two outcomes, mirroring
how the equity model prices its sell leg at take_profit. It is recorded as an
ASSUMPTION, not a citation, and its effect is bounded: at a $3.20 premium the SEC
term is under $0.02 per contract against roughly $0.13 of exact per-contract
fees, so even a large error here cannot move the total materially.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SEC_RATE_OF_NOTIONAL = 0.0000206     # sells only, x trade value
TAF_PER_CONTRACT = 0.00329           # sells only
CAT_PER_EQUIVALENT_SHARE = 0.000003  # both sides; 1 contract = 100 equiv. shares
ORF_PER_CONTRACT = 0.015             # both sides
OCC_PER_CONTRACT = 0.025             # both sides
INDEX_OPTION_COMMISSION_PER_CONTRACT = 0.50

RATES_EFFECTIVE_DATE = "2026-09-01"
RATES_SOURCE = "https://files.alpaca.markets/disclosures/library/BrokFeeSched.pdf"
MODEL_NAME = "options_pre_trade_round_trip_per_contract_estimate"

EXCLUSIONS = (
    "daily_fee_aggregation_rounding",   # the one non-conservative omission
    "exercise_and_assignment_fees",     # not applicable while long and closed before expiry
    "market_impact",
)


class OptionsCostError(Exception):
    """Fail closed: a cost that cannot be computed is never treated as zero."""


@dataclass(frozen=True)
class OptionsCostEstimate:
    per_contract_round_trip: float
    total: float
    contracts: int
    components: dict[str, float]
    quality: dict[str, str]
    assumptions: dict[str, float | str] = field(default_factory=dict)

    def as_breakdown(self) -> dict:
        """Provenance carried onto the persisted decision — same contract as the
        equity model, so a decision record is readable either way."""
        return {
            "model": MODEL_NAME,
            "rates_effective_date": RATES_EFFECTIVE_DATE,
            "rates_source": RATES_SOURCE,
            "contracts": self.contracts,
            "per_contract_round_trip": self.per_contract_round_trip,
            "total": self.total,
            "components": dict(self.components),
            "quality": dict(self.quality),
            "assumptions": dict(self.assumptions),
            "excludes": list(EXCLUSIONS),
        }


def estimate_options_cost(
    *,
    entry_premium: float,
    contracts: int,
    multiplier: int = 100,
    index_option: bool = False,
    exit_premium_multiple: float = 2.0,
) -> OptionsCostEstimate:
    """Round-trip (buy to open + sell to close) fee estimate for a long option."""
    if index_option:
        raise OptionsCostError(
            "index options are out of scope: the schedule adds a "
            f"${INDEX_OPTION_COMMISSION_PER_CONTRACT:.2f}/contract commission "
            "that this model does not apply. Refusing to price rather than "
            "silently omitting it."
        )
    if entry_premium <= 0:
        raise OptionsCostError(f"entry_premium must be positive, got {entry_premium}")
    if contracts < 1:
        raise OptionsCostError(f"contracts must be >= 1, got {contracts}")
    if multiplier <= 0:
        raise OptionsCostError(f"multiplier must be positive, got {multiplier}")
    if exit_premium_multiple <= 0:
        raise OptionsCostError("exit_premium_multiple must be positive")

    exit_premium = entry_premium * exit_premium_multiple
    sell_notional = exit_premium * multiplier

    per_contract = {
        # sells only, once per round trip
        "sec": SEC_RATE_OF_NOTIONAL * sell_notional,
        "taf": TAF_PER_CONTRACT,
        # both sides
        "cat": CAT_PER_EQUIVALENT_SHARE * multiplier * 2,
        "orf": ORF_PER_CONTRACT * 2,
        "occ": OCC_PER_CONTRACT * 2,
        # Standard (non-index) equity options carry no Alpaca commission under
        # the retail arrangement attested in configs/execution_config.yaml.
        "commission": 0.0,
    }
    unit = sum(per_contract.values())
    return OptionsCostEstimate(
        per_contract_round_trip=unit,
        total=unit * contracts,
        contracts=contracts,
        components={k: v * contracts for k, v in per_contract.items()},
        quality={
            "taf": "exact",
            "cat": "exact",
            "orf": "exact",
            "occ": "exact",
            "sec": "assumption_exit_premium_unknown",
            "commission": "attested_retail_non_index",
            "daily_rounding": "not_modelled_understates",
            "overall": "exact_except_sec_and_daily_rounding",
        },
        assumptions={
            "exit_premium_multiple": exit_premium_multiple,
            "exit_premium_priced_at": exit_premium,
            "basis": (
                "SEC is charged on the sell, whose premium is unknown pre-trade; "
                "priced at the modelled winning exit, mirroring the equity model's "
                "use of take_profit. No option pricing model is used."
            ),
        },
    )

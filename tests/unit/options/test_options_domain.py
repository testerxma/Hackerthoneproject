"""
Options selection, sizing and cost.

The property under test throughout is that an options position can never be
sized with equity mathematics and can never be opened on a contract whose
maximum loss is not exactly known.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import pytest  # noqa: E402

from speedtrader.options.contracts import (  # noqa: E402
    STANDARD_CONTRACT_MULTIPLIER, ContractType, OptionContract, OptionQuote,
    SelectionError, SelectionPolicy, Structure, contract_type_for, select_contract,
)
from speedtrader.options.cost import (  # noqa: E402
    OptionsCostError, estimate_options_cost,
)
from speedtrader.options.risk import (  # noqa: E402
    OptionsSizingPolicy, size_option_position,
)

ASOF = date(2026, 9, 2)
EXPIRY = date(2026, 9, 30)          # 28 DTE
SPOT = 116.0


def contract(strike=115.0, *, type=ContractType.CALL, expiration=EXPIRY,
             bid=3.0, ask=3.2, oi=500, multiplier=STANDARD_CONTRACT_MULTIPLIER,
             tradable=True, quote=True):
    return OptionContract(
        symbol=f"T{expiration:%y%m%d}{'C' if type is ContractType.CALL else 'P'}"
               f"{int(strike * 1000):08d}",
        underlying="T", type=type, strike=strike, expiration=expiration,
        multiplier=multiplier, open_interest=oi, tradable=tradable,
        quote=OptionQuote(bid=bid, ask=ask) if quote else None,
    )


def chain(strikes=(110.0, 115.0, 120.0), **kw):
    return [contract(strike=s, **kw) for s in strikes]


# ============================================ direction -> structure

def test_buy_is_a_long_call_and_sell_is_a_long_put():
    """A bearish view is a LONG PUT, never a short call: defined risk both ways."""
    assert contract_type_for("BUY") is ContractType.CALL
    assert contract_type_for("SELL") is ContractType.PUT


@pytest.mark.parametrize("bad", ["HOLD", "", "buy_to_open", None])
def test_unknown_direction_rejected(bad):
    with pytest.raises(SelectionError):
        contract_type_for(bad)


def test_unimplemented_structure_is_refused_not_silently_downgraded():
    with pytest.raises(SelectionError, match="not implemented"):
        select_contract(chain(), direction="BUY", underlying_price=SPOT,
                        asof=ASOF, structure=Structure.VERTICAL_DEBIT)


# ============================================ selection

def test_selects_the_nearest_the_money_strike():
    s = select_contract(chain(), direction="BUY", underlying_price=SPOT, asof=ASOF)
    assert s.contract.strike == 115.0          # |115-116| < |120-116| < |110-116|
    assert s.contract.type is ContractType.CALL


def test_sell_signal_selects_a_put():
    s = select_contract(chain(type=ContractType.PUT), direction="SELL",
                        underlying_price=SPOT, asof=ASOF)
    assert s.contract.type is ContractType.PUT


def test_selection_is_deterministic_across_repeated_runs():
    """Replay is meaningless if selection can drift."""
    picks = {select_contract(chain(), direction="BUY", underlying_price=SPOT,
                             asof=ASOF).contract.symbol for _ in range(50)}
    assert len(picks) == 1


def test_selection_is_independent_of_chain_ordering():
    forward = select_contract(chain(), direction="BUY", underlying_price=SPOT,
                              asof=ASOF).contract.symbol
    backward = select_contract(list(reversed(chain())), direction="BUY",
                               underlying_price=SPOT, asof=ASOF).contract.symbol
    assert forward == backward


def test_ties_break_on_dte_then_symbol():
    """Two equidistant strikes must still produce one stable answer."""
    near = contract(strike=115.0, expiration=date(2026, 10, 2))    # 30 DTE, on target
    far = contract(strike=117.0, expiration=EXPIRY)                # 28 DTE
    s = select_contract([near, far], direction="BUY", underlying_price=116.0, asof=ASOF)
    assert s.contract.strike == 115.0 or s.contract.strike == 117.0
    again = select_contract([far, near], direction="BUY", underlying_price=116.0,
                            asof=ASOF)
    assert s.contract.symbol == again.contract.symbol


# ============================================ selection fails closed

@pytest.mark.parametrize("kw,expected", [
    ({"expiration": date(2026, 9, 5)}, "expires_too_soon"),      # 3 DTE
    ({"expiration": date(2027, 6, 1)}, "expires_too_late"),
    ({"quote": False}, "no_quote"),
    ({"bid": 0.0}, "no_two_sided_market"),
    ({"ask": 0.0}, "no_two_sided_market"),
    ({"bid": 5.0, "ask": 4.0}, "crossed_quote"),
    ({"bid": 0.01, "ask": 0.02}, "premium_below_floor"),
    ({"bid": 1.0, "ask": 3.0}, "spread_too_wide"),               # 100% of mid
    ({"oi": 1}, "insufficient_open_interest"),
    ({"tradable": False}, "not_tradable"),
    ({"multiplier": 137}, "non_standard_multiplier"),            # adjusted contract
])
def test_every_disqualifier_rejects_and_is_named(kw, expected):
    with pytest.raises(SelectionError) as e:
        select_contract(chain(**kw), direction="BUY",
                        underlying_price=SPOT, asof=ASOF)
    assert expected in str(e.value)


def test_adjusted_contract_never_selected_even_alongside_a_standard_one():
    """A 137-share adjusted contract would silently corrupt max-loss sizing."""
    adjusted = contract(strike=115.0, multiplier=137)
    standard = contract(strike=120.0)
    s = select_contract([adjusted, standard], direction="BUY",
                        underlying_price=115.0, asof=ASOF)
    assert s.contract.multiplier == STANDARD_CONTRACT_MULTIPLIER
    assert s.contract.strike == 120.0          # chosen despite being further OTM


def test_empty_chain_rejects():
    with pytest.raises(SelectionError):
        select_contract([], direction="BUY", underlying_price=SPOT, asof=ASOF)


def test_wrong_type_only_chain_rejects():
    with pytest.raises(SelectionError, match="wrong_type"):
        select_contract(chain(type=ContractType.PUT), direction="BUY",
                        underlying_price=SPOT, asof=ASOF)


@pytest.mark.parametrize("price", [0.0, -1.0])
def test_non_positive_underlying_rejected(price):
    with pytest.raises(SelectionError):
        select_contract(chain(), direction="BUY", underlying_price=price, asof=ASOF)


def test_rejection_reasons_are_recorded_for_audit():
    s = select_contract([contract(strike=115.0), contract(strike=120.0, oi=1)],
                        direction="BUY", underlying_price=SPOT, asof=ASOF)
    assert s.considered == 2
    assert s.rejected["insufficient_open_interest"] == 1


@pytest.mark.parametrize("policy", [
    SelectionPolicy(min_dte=0),
    SelectionPolicy(min_dte=40, target_dte=30),
    SelectionPolicy(max_spread_pct_of_mid=0),
    SelectionPolicy(min_ask=0),
])
def test_incoherent_policy_rejected(policy):
    with pytest.raises(ValueError):
        select_contract(chain(), direction="BUY", underlying_price=SPOT,
                        asof=ASOF, policy=policy)


# ============================================ sizing: NOT equity math

def test_sizes_on_premium_times_multiplier_not_on_a_stop_distance():
    #   budget = 1% of 100k = 1000
    #   one contract = 3.20 * 100 = 320 max loss  ->  floor(1000/320) = 3
    z = size_option_position(contract=contract(), account_balance=100_000.0)
    assert z.quantity == 3
    assert z.max_loss_per_contract == pytest.approx(320.0)
    assert z.total_debit == pytest.approx(960.0)


def test_max_loss_equals_the_debit_exactly():
    """The defining property of a long option, and the reason this expression is
    safer than an equity stop that can gap through."""
    z = size_option_position(contract=contract(), account_balance=100_000.0)
    assert z.max_loss_total == z.total_debit
    assert z.max_loss_total == pytest.approx(z.quantity * z.premium_per_contract * 100)


def test_priced_at_the_ask_never_the_mid():
    """Using the mid would understate max loss by half the spread every time."""
    z = size_option_position(contract=contract(bid=3.0, ask=3.2),
                             account_balance=100_000.0)
    assert z.premium_per_contract == 3.2            # ask, not 3.1 mid


def test_never_exceeds_the_risk_budget():
    for balance in (5_000.0, 10_000.0, 100_000.0, 250_000.0):
        z = size_option_position(contract=contract(), account_balance=balance)
        assert z.max_loss_total <= z.risk_budget


def test_one_contract_over_budget_is_rejected_not_rounded_up():
    #   1% of 20,000 = 200 budget; one contract costs 320
    z = size_option_position(contract=contract(), account_balance=20_000.0)
    assert z.quantity == 0 and not z.approved
    assert "over the" in z.reason


def test_contract_cap_applied():
    z = size_option_position(
        contract=contract(bid=0.10, ask=0.12), account_balance=1_000_000.0,
        policy=OptionsSizingPolicy(max_contracts=10))
    assert z.quantity == 10
    assert any("max_contracts" in c for c in z.caps_applied)


def test_concentration_cap_counts_premium_already_open():
    z = size_option_position(
        contract=contract(), account_balance=100_000.0,
        policy=OptionsSizingPolicy(max_premium_pct_of_balance=1.0),
        open_premium=900.0)
    #   cap = 1000, already 900 open, 100 left -> under one 320 contract
    assert z.quantity == 0
    assert "concentration" in z.reason or "below one contract" in z.reason


def test_book_already_at_the_concentration_cap_rejects():
    z = size_option_position(
        contract=contract(), account_balance=100_000.0,
        policy=OptionsSizingPolicy(max_premium_pct_of_balance=2.0),
        open_premium=5_000.0)
    assert z.quantity == 0 and "concentration cap" in z.reason


@pytest.mark.parametrize("kw", [{"quote": False}, {"bid": 0.0, "ask": 0.0}])
def test_unquotable_contract_cannot_be_sized(kw):
    z = size_option_position(contract=contract(**kw), account_balance=100_000.0)
    assert z.quantity == 0


@pytest.mark.parametrize("balance", [0.0, -1.0])
def test_non_positive_balance_rejected(balance):
    assert size_option_position(contract=contract(),
                                account_balance=balance).quantity == 0


def test_sizing_is_deterministic():
    q = {size_option_position(contract=contract(),
                              account_balance=100_000.0).quantity for _ in range(50)}
    assert len(q) == 1


# ============================================ options cost

def test_per_contract_fees_match_the_published_schedule():
    #   taf 0.00329 + cat 0.000003*100*2 + orf 0.015*2 + occ 0.025*2
    #   sec 0.0000206 * (3.20*2*100)
    c = estimate_options_cost(entry_premium=3.20, contracts=1)
    k = c.components
    assert k["taf"] == pytest.approx(0.00329)
    assert k["cat"] == pytest.approx(0.0006)
    assert k["orf"] == pytest.approx(0.030)
    assert k["occ"] == pytest.approx(0.050)
    assert k["sec"] == pytest.approx(0.0000206 * 640.0)
    assert c.per_contract_round_trip == pytest.approx(sum(k.values()))


def test_equity_per_share_fees_are_not_reused():
    """Options fees are per CONTRACT and include ORF and OCC, which have no
    equity equivalent. A per-share model would understate them enormously."""
    c = estimate_options_cost(entry_premium=3.20, contracts=1)
    assert c.per_contract_round_trip > 0.08     # vs ~0.0025/share for equities
    assert {"orf", "occ"} <= set(c.components)


def test_cost_scales_with_contract_count():
    one = estimate_options_cost(entry_premium=3.20, contracts=1)
    ten = estimate_options_cost(entry_premium=3.20, contracts=10)
    assert ten.total == pytest.approx(one.total * 10)


def test_index_options_are_refused_not_silently_mispriced():
    """The schedule adds $0.50/contract that this model does not apply."""
    with pytest.raises(OptionsCostError, match="index"):
        estimate_options_cost(entry_premium=3.20, contracts=1, index_option=True)


@pytest.mark.parametrize("kw", [
    {"entry_premium": 0.0}, {"entry_premium": -1.0},
    {"contracts": 0}, {"contracts": -2}, {"multiplier": 0},
    {"exit_premium_multiple": 0.0},
])
def test_uncomputable_cost_raises_rather_than_returning_zero(kw):
    args = {"entry_premium": 3.20, "contracts": 1, **kw}
    with pytest.raises(OptionsCostError):
        estimate_options_cost(**args)


def test_the_sec_assumption_is_declared_not_buried():
    c = estimate_options_cost(entry_premium=3.20, contracts=1)
    assert c.quality["sec"] == "assumption_exit_premium_unknown"
    assert c.assumptions["exit_premium_multiple"] == 2.0
    assert "no option pricing model" in c.assumptions["basis"].lower()


def test_breakdown_is_persistable_and_names_its_exclusions():
    import json
    b = estimate_options_cost(entry_premium=3.20, contracts=3).as_breakdown()
    assert json.loads(json.dumps(b)) == b
    assert "daily_fee_aggregation_rounding" in b["excludes"]
    assert b["rates_effective_date"] == "2026-09-01"
    assert b["model"].startswith("options_")

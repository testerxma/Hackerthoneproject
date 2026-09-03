"""
Price-aware cost policy tests.

Rate VALUES are asserted only where they are transcribed from the official Alpaca
Brokerage Fee Schedule (revised 2026-09-01). Everything else tests structure,
arithmetic, provenance and failure.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import pytest  # noqa: E402
import yaml  # noqa: E402

from speedtrader.quant.cost_policy import (  # noqa: E402
    COST_BLOCK, EXCLUSIONS, MODEL_NAME, CostPolicyError, CostPolicyInvalid,
    EVCostNotConfigured, cost_policy_from_config,
)

ROOT = Path(__file__).resolve().parents[3]
PROD = yaml.safe_load((ROOT / "configs" / "execution_config.yaml").read_text())

# Official rates, transcribed from the schedule the production config cites.
SEC_RATE, TAF, CAT = 0.0000206, 0.000195, 0.000003

# --- TEST FIXTURE ONLY: supplies the commission the operator has not yet resolved.
COMMISSION_FIXTURE = {"per_share": 0.0, "rate_of_notional": 0.0,
                      "source": "operator_assumption",
                      "assumption": "test fixture"}


def cfg(**patch):
    c = copy.deepcopy(PROD)
    c[COST_BLOCK]["default"]["commission"] = dict(COMMISSION_FIXTURE)
    for k, v in patch.items():
        c[COST_BLOCK]["default"][k] = v
    return c


def policy(**patch):
    return cost_policy_from_config(cfg(**patch))


# ============================================ production config state

def test_production_regulatory_rates_are_authoritative():
    r = PROD[COST_BLOCK]["default"]["regulatory"]
    assert r["sec_rate_of_notional"] == SEC_RATE
    assert r["taf_per_share"] == TAF
    assert r["cat_per_share"] == CAT
    assert r["sides_per_round_trip"] == 2
    assert r["source"] == "authoritative"
    assert "2026-09-01" in r["reference"]


def test_production_provenance_recorded():
    b = PROD[COST_BLOCK]
    assert b["rates_effective_date"] == "2026-09-01"
    assert b["rates_source"].startswith("https://files.alpaca.markets/")
    assert b["model"] == MODEL_NAME


def test_production_slippage_is_marked_as_an_assumption_not_a_measurement():
    s = PROD[COST_BLOCK]["default"]["slippage"]
    assert s["per_share"] == 0.0
    assert s["source"] == "operator_assumption"
    assert "NOT a claim" in s["assumption"]


def test_production_commission_is_resolved_with_recorded_provenance():
    """Resolved 2026-09-02 to standard-retail commission-free.

    The rate being ZERO is not the same as the rate being UNKNOWN, and the
    difference has to be visible in the record: a zero with no provenance is
    indistinguishable from a value nobody ever decided.
    """
    c = PROD[COST_BLOCK]["default"]["commission"]
    assert c["per_share"] == 0.0 and c["rate_of_notional"] == 0.0
    # NOT 'authoritative': the published schedule supplies the rate, but the
    # claim that this account is standard retail is an attestation, not a citation.
    assert c["source"] == "operator_assumption"
    assert c["assumption"].strip(), "a zero commission with no stated basis"
    cost_policy_from_config(PROD)          # the production config now builds


def test_production_config_still_fails_closed_without_the_commission():
    """The gate that was protecting us is still there — it is simply satisfied
    now. Removing the decision must re-close the pipeline immediately."""
    import copy as _copy
    c = _copy.deepcopy(PROD)
    del c[COST_BLOCK]["default"]["commission"]
    with pytest.raises(EVCostNotConfigured, match="commission"):
        cost_policy_from_config(c)


def test_production_commission_may_not_be_silently_upgraded_to_authoritative():
    """No vendor document can attest to which arrangement an account is on."""
    c = cfg(); c[COST_BLOCK]["default"]["commission"] = {
        "per_share": 0.0, "rate_of_notional": 0.0, "source": "authoritative"}
    p = cost_policy_from_config(c)
    # It parses (a schedule CAN state a commission), but the production config
    # must not claim it, which the test above pins.
    assert p.commission.source.value == "authoritative"


# ============================================ fail closed

def test_absent_block_raises():
    with pytest.raises(EVCostNotConfigured, match=COST_BLOCK):
        cost_policy_from_config({"include_spread_in_ev_cost": True})


@pytest.mark.parametrize("drop", ["rates_effective_date", "rates_source", "default"])
def test_missing_top_level_key_raises(drop):
    c = cfg(); del c[COST_BLOCK][drop]
    with pytest.raises(EVCostNotConfigured, match=drop):
        cost_policy_from_config(c)


@pytest.mark.parametrize("block,key", [
    ("commission", "per_share"), ("commission", "rate_of_notional"),
    ("commission", "source"),
    ("regulatory", "sec_rate_of_notional"), ("regulatory", "taf_per_share"),
    ("regulatory", "cat_per_share"), ("regulatory", "sides_per_round_trip"),
    ("regulatory", "source"),
    ("slippage", "per_share"), ("slippage", "source"),
])
def test_every_required_key_fails_closed_individually(block, key):
    c = cfg(); del c[COST_BLOCK]["default"][block][key]
    with pytest.raises(EVCostNotConfigured, match=key):
        cost_policy_from_config(c)


@pytest.mark.parametrize("block", ["commission", "regulatory", "slippage"])
def test_missing_whole_sub_block_raises(block):
    c = cfg(); del c[COST_BLOCK]["default"][block]
    with pytest.raises(EVCostNotConfigured, match=block):
        cost_policy_from_config(c)


def test_partial_override_is_rejected_not_merged():
    c = cfg()
    c[COST_BLOCK]["overrides"] = {"AAPL": {"regulatory": {"taf_per_share": 0.0004}}}
    with pytest.raises(EVCostNotConfigured):
        cost_policy_from_config(c, symbol="AAPL")


# ============================================ invalid values

@pytest.mark.parametrize("bad", [-0.001, float("nan"), float("inf"), "0.01", True])
def test_invalid_rate_rejected(bad):
    r = dict(PROD[COST_BLOCK]["default"]["regulatory"]); r["taf_per_share"] = bad
    with pytest.raises(CostPolicyError):
        policy(regulatory=r)


@pytest.mark.parametrize("bad", [0, -1, 2.5, "two", True])
def test_invalid_sides_rejected(bad):
    r = dict(PROD[COST_BLOCK]["default"]["regulatory"]); r["sides_per_round_trip"] = bad
    with pytest.raises(CostPolicyInvalid, match="sides_per_round_trip"):
        policy(regulatory=r)


def test_unknown_provenance_source_rejected():
    with pytest.raises(CostPolicyInvalid, match="source"):
        policy(commission={**COMMISSION_FIXTURE, "source": "vibes"})


def test_operator_assumption_without_an_assumption_is_rejected():
    """An unexplained assumption is indistinguishable from a fabricated value."""
    with pytest.raises(CostPolicyInvalid, match="assumption"):
        policy(commission={"per_share": 0.0, "rate_of_notional": 0.0,
                           "source": "operator_assumption"})


def test_invalid_is_distinguishable_from_absent():
    assert issubclass(EVCostNotConfigured, CostPolicyError)
    assert issubclass(CostPolicyInvalid, CostPolicyError)
    assert not issubclass(CostPolicyInvalid, EVCostNotConfigured)


# ============================================ price-aware arithmetic

def test_buy_prices_sell_leg_at_exit():
    #   BUY: enter=buy@115, exit=sell@121 (take_profit, upper of two outcomes)
    #   sec = 0.0000206 * 121 = 0.00249260
    #   taf = 0.000195                      (one sell leg, uncapped)
    #   cat = 0.000003 * 2 = 0.000006       (both legs)
    e = policy().estimate(entry=115.0, take_profit=121.0, direction="BUY")
    assert e.sell_leg == "exit" and e.sell_price == 121.0
    assert e.components["sec"] == pytest.approx(SEC_RATE * 121.0)
    assert e.components["taf"] == pytest.approx(TAF)
    assert e.components["cat"] == pytest.approx(CAT * 2)
    assert e.per_share == pytest.approx(SEC_RATE * 121.0 + TAF + CAT * 2)


def test_sell_prices_sell_leg_at_entry_and_is_exact():
    #   SELL: enter=sell@115 (known), exit=buy@109
    e = policy().estimate(entry=115.0, take_profit=109.0, direction="SELL")
    assert e.sell_leg == "entry" and e.sell_price == 115.0
    assert e.components["sec"] == pytest.approx(SEC_RATE * 115.0)
    assert e.quality["sec"] == "exact"


def test_buy_sec_is_conservative_not_exact():
    e = policy().estimate(entry=115.0, take_profit=121.0, direction="BUY")
    assert e.quality["sec"] == "conservative"


def test_sec_scales_with_price_flat_constant_would_not():
    """The whole reason the flat model was replaced."""
    cheap = policy().estimate(entry=12.0, take_profit=12.6, direction="BUY")
    rich = policy().estimate(entry=900.0, take_profit=945.0, direction="BUY")
    assert rich.components["sec"] / cheap.components["sec"] == pytest.approx(75.0, rel=1e-6)


def test_cat_applies_to_both_legs_sec_and_taf_to_one():
    e = policy().estimate(entry=100.0, take_profit=110.0, direction="BUY")
    assert e.components["cat"] == pytest.approx(CAT * 2)
    assert e.components["taf"] == pytest.approx(TAF)          # x1, not x2
    assert e.components["sec"] == pytest.approx(SEC_RATE * 110.0)   # x1


def test_commission_applies_to_both_legs():
    p = policy(commission={"per_share": 0.005, "rate_of_notional": 0.001,
                           "source": "operator_assumption", "assumption": "fixture"})
    e = p.estimate(entry=100.0, take_profit=110.0, direction="BUY")
    #   0.005*2 + 0.001*(100+110) = 0.01 + 0.21
    assert e.components["commission"] == pytest.approx(0.22)


def test_percentage_commission_is_supported_not_forced_per_share():
    """The schedule quotes a percentage range; the model must accept that shape."""
    p = policy(commission={"per_share": 0.0, "rate_of_notional": 0.003,
                           "source": "operator_assumption", "assumption": "fixture"})
    e = p.estimate(entry=100.0, take_profit=100.0, direction="BUY")
    assert e.components["commission"] == pytest.approx(0.6)


def test_slippage_passes_through_unmodified():
    p = policy(slippage={"per_share": 0.02, "source": "operator_assumption",
                         "assumption": "fixture"})
    e = p.estimate(entry=100.0, take_profit=110.0, direction="BUY")
    assert e.components["slippage"] == 0.02


def test_spread_is_separate_from_fees():
    e = policy().estimate(entry=100.0, take_profit=110.0, direction="BUY", spread=0.05)
    assert e.spread == 0.05
    assert "spread" not in e.components
    assert e.total_per_share() == pytest.approx(e.per_share + 0.05)


def test_spread_excluded_when_configured_off():
    c = cfg(); c["include_spread_in_ev_cost"] = False
    e = cost_policy_from_config(c).estimate(entry=100.0, take_profit=110.0,
                                            direction="BUY", spread=0.05)
    assert e.spread == 0.0


def test_invalid_direction_rejected():
    with pytest.raises(CostPolicyInvalid, match="direction"):
        policy().estimate(entry=100.0, take_profit=110.0, direction="HOLD")


# ============================================ honesty of the record

def test_breakdown_declares_model_and_exclusions():
    p = policy()
    b = p.breakdown(p.estimate(entry=115.0, take_profit=121.0, direction="BUY"))
    assert b["model"] == MODEL_NAME
    assert "taf_per_trade_cap" in b["excludes"]
    assert "daily_fee_aggregation_rounding" in b["excludes"]
    # The persisted record must list exactly what the module declares it omits;
    # a breakdown that under-reports its own exclusions is a false audit trail.
    assert b["excludes"] == list(EXCLUSIONS)


def test_breakdown_records_per_component_provenance():
    p = policy()
    b = p.breakdown(p.estimate(entry=115.0, take_profit=121.0, direction="BUY"))
    assert b["provenance"]["regulatory"]["source"] == "authoritative"
    assert b["provenance"]["slippage"]["source"] == "operator_assumption"
    assert b["rates_effective_date"] == "2026-09-01"
    assert b["rates_source"].startswith("https://")


def test_daily_rounding_flagged_as_the_non_conservative_omission():
    """The one place the estimate can understate cost. Must not be hidden."""
    e = policy().estimate(entry=115.0, take_profit=121.0, direction="BUY")
    assert e.quality["daily_rounding"] == "not_modelled_understates"
    assert e.quality["overall"] == "conservative_except_daily_rounding"


def test_uncapped_taf_is_conservative():
    """The cap can only reduce the fee, so omitting it overstates cost."""
    e = policy().estimate(entry=115.0, take_profit=121.0, direction="BUY")
    assert e.quality["taf"] == "conservative"
    assert e.components["taf"] == pytest.approx(TAF)   # full rate, no cap


def test_paper_mode_does_not_zero_the_cost():
    """EV models execution economics, not what the simulator debits."""
    e = policy().estimate(entry=115.0, take_profit=121.0, direction="BUY")
    assert e.per_share > 0.0


# ============================================ structure & determinism

def test_policy_is_frozen():
    p = policy()
    with pytest.raises(Exception):
        p.slippage_per_share = 9.9


def test_cost_is_quantity_independent():
    p = policy()
    for f in list(p.__dataclass_fields__) + list(p.regulatory.__dataclass_fields__):
        assert "quantity" not in f


def test_deterministic():
    vals = {policy().estimate(entry=115.0, take_profit=121.0,
                              direction="BUY").per_share for _ in range(50)}
    assert len(vals) == 1


def test_full_override_applied():
    c = cfg()
    c[COST_BLOCK]["overrides"] = {"AAPL": {
        "commission": dict(COMMISSION_FIXTURE),
        "regulatory": {**PROD[COST_BLOCK]["default"]["regulatory"],
                       "taf_per_share": 0.0004},
        "slippage": {"per_share": 0.01, "source": "operator_assumption",
                     "assumption": "fixture"}}}
    p = cost_policy_from_config(c, symbol="AAPL")
    assert p.override_applied is True
    assert p.regulatory.taf_per_share == 0.0004
    assert cost_policy_from_config(c, symbol="MSFT").override_applied is False


# ============================================ config/code drift & malformed input
# Every branch below is a fail-closed guard. An untested fail-closed guard is
# indistinguishable from a fail-OPEN one, so each is exercised explicitly.

def test_model_drift_between_config_and_code_is_refused():
    """A config written for a different cost model must not be silently mispriced.
    This is the exact failure class that desynchronised the module in the first
    place, so it fails loudly rather than parsing a foreign schema."""
    c = cfg(); c[COST_BLOCK]["model"] = "some_other_cost_model_v9"
    with pytest.raises(CostPolicyInvalid, match="model"):
        cost_policy_from_config(c)


def test_declared_model_must_not_be_empty():
    c = cfg(); c[COST_BLOCK]["model"] = "   "
    with pytest.raises(CostPolicyInvalid, match="model"):
        cost_policy_from_config(c)


def test_slippage_may_never_be_authoritative():
    """No vendor publishes another party's slippage. Marking an assumption as
    authoritative would launder a guess into a citation."""
    with pytest.raises(CostPolicyInvalid, match="authoritative"):
        policy(slippage={"per_share": 0.01, "source": "authoritative",
                         "assumption": "claimed from a schedule"})


@pytest.mark.parametrize("component", ["commission", "regulatory", "slippage"])
def test_non_mapping_component_rejected(component):
    with pytest.raises(CostPolicyInvalid, match=component):
        policy(**{component: ["not", "a", "mapping"]})


def test_non_mapping_cost_block_rejected():
    with pytest.raises(CostPolicyInvalid, match=COST_BLOCK):
        cost_policy_from_config({COST_BLOCK: "not a mapping"})


def test_non_mapping_default_rejected():
    c = cfg(); c[COST_BLOCK]["default"] = "not a mapping"
    with pytest.raises(CostPolicyInvalid, match="default"):
        cost_policy_from_config(c)


def test_non_mapping_override_rejected():
    c = cfg(); c[COST_BLOCK]["overrides"] = {"AAPL": "not a mapping"}
    with pytest.raises(CostPolicyInvalid, match="AAPL"):
        cost_policy_from_config(c, symbol="AAPL")


@pytest.mark.parametrize("field", ["rates_effective_date", "rates_source"])
def test_blank_provenance_field_rejected(field):
    c = cfg(); c[COST_BLOCK][field] = "   "
    with pytest.raises(CostPolicyInvalid, match=field):
        cost_policy_from_config(c)


@pytest.mark.parametrize("entry,tp", [(0.0, 110.0), (-1.0, 110.0),
                                      (100.0, 0.0), (100.0, -5.0)])
def test_non_positive_prices_rejected(entry, tp):
    """A zero or negative price would silently zero the notional fees."""
    with pytest.raises(CostPolicyInvalid):
        policy().estimate(entry=entry, take_profit=tp, direction="BUY")


def test_regulatory_operator_assumption_needs_an_assumption_too():
    """The rule applies to every component, not just slippage."""
    r = {**PROD[COST_BLOCK]["default"]["regulatory"], "source": "operator_assumption"}
    r.pop("assumption", None)
    with pytest.raises(CostPolicyInvalid, match="assumption"):
        policy(regulatory=r)


def test_override_lookup_is_case_insensitive_on_the_symbol():
    c = cfg()
    c[COST_BLOCK]["overrides"] = {"AAPL": {
        "commission": dict(COMMISSION_FIXTURE),
        "regulatory": dict(PROD[COST_BLOCK]["default"]["regulatory"]),
        "slippage": {"per_share": 0.03, "source": "operator_assumption",
                     "assumption": "fixture"}}}
    assert cost_policy_from_config(c, symbol="aapl").override_applied is True
    assert cost_policy_from_config(c, symbol="AAPL").slippage.per_share == 0.03

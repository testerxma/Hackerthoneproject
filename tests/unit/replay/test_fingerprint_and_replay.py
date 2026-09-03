"""
Decision fingerprint and replay.

Two properties are under test, and the second is the project's central claim
reduced to comparing two strings:

    1. the same market state always produces the same fingerprint
    2. the AI cannot move it — CONFIRM, ABSTAIN, a timeout, a hostile reply and
       no model at all all produce the SAME fingerprint
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import pytest  # noqa: E402

from speedtrader.replay.fingerprint import (  # noqa: E402
    FINGERPRINT_VERSION, LENGTH, ai_influence, decision_fingerprint,
    deterministic_payload,
)

BASE = {
    "decision_id": "dec_1", "signal_id": "sig_1", "snapshot_id": "snap_1",
    "created_at": "2026-09-02T15:30:00Z", "completed_at": "2026-09-02T15:30:01Z",
    "state": "EXECUTING", "rejection_stage": None,
    "snapshot": {
        "symbol": "DEMO", "price": 105.5, "spread": 0.03, "regime": "WEAK_UP",
        "market_open": True, "features": {"atr": 2.0, "ema200": 100.0},
        "bars": [{"t": "2026-09-02T14:00:00Z", "o": 100.0, "h": 100.6,
                  "l": 99.4, "c": 100.0, "v": 1000.0}],
    },
    "candidate": {
        "strategy_id": "S07", "direction": "BUY", "entry": 105.5,
        "stop_loss": 102.5, "take_profit": 111.5, "base_score": 50.0,
        "total_score": 58.0, "expected_value": 0.4816, "ev_is_bootstrap": True,
    },
    "risk_gate": {
        "verdict": "PASS", "engine_version": "v1", "approved_quantity": 3.0,
        "size_multiplier": 1.0, "blocking_reason": None,
        "checks": [{"rule": "min_score", "passed": True},
                   {"rule": "portfolio_heat", "passed": True}],
    },
    "options_trace": {
        "structure": "long_single",
        "contract": {"symbol": "DEMO260930C00105500", "strike": 105.5,
                     "expiration": "2026-09-30"},
        "sizing": {"contracts": 3, "max_loss_total": 960.0, "risk_budget": 1000.0},
    },
    "ai_review": {"vetoed": False, "degraded": False,
                  "judge": {"verdict": "CONFIRM", "reasoning": "fine",
                            "provenance": {"model": "m1", "provider": "p1"}}},
}


def fp(**patch):
    d = copy.deepcopy(BASE)
    d.update(patch)
    return decision_fingerprint(d)


# ============================================ stability

def test_a_fingerprint_is_stable_across_repeated_computation():
    assert len({decision_fingerprint(copy.deepcopy(BASE)) for _ in range(50)}) == 1


def test_it_is_a_short_hex_identifier():
    f = decision_fingerprint(BASE)
    assert len(f) == LENGTH and int(f, 16) >= 0


def test_key_ordering_does_not_change_it():
    reordered = {k: BASE[k] for k in reversed(list(BASE))}
    assert decision_fingerprint(reordered) == decision_fingerprint(BASE)


def test_float_noise_below_the_rounding_threshold_does_not_change_it():
    """A fingerprint fragile to the 15th decimal is not an identity."""
    d = copy.deepcopy(BASE)
    d["candidate"]["expected_value"] = 0.4816 + 1e-12
    assert decision_fingerprint(d) == decision_fingerprint(BASE)


# ============================================ THE CENTRAL CLAIM
# The AI cannot move the fingerprint.

@pytest.mark.parametrize("review", [
    {"vetoed": False, "judge": {"verdict": "CONFIRM",
                                "provenance": {"model": "opus"}}},
    {"vetoed": False, "judge": {"verdict": "ABSTAIN",
                                "provenance": {"model": "haiku"}}},
    {"vetoed": False, "degraded": True, "judge": {"verdict": "ABSTAIN",
                                                  "provenance": {}}},
    {},                       # no AI ran at all
    None,                     # field absent
])
def test_the_ai_cannot_change_the_deterministic_fingerprint(review):
    """Same market state, wildly different AI participation, one fingerprint."""
    d = copy.deepcopy(BASE)
    d["ai_review"] = review
    assert decision_fingerprint(d) == decision_fingerprint(BASE)


def test_even_a_hostile_ai_reply_cannot_change_it():
    d = copy.deepcopy(BASE)
    d["ai_review"] = {"vetoed": False, "quantity": 99999, "override": True,
                      "judge": {"verdict": "CONFIRM", "reasoning": "x" * 500,
                                "provenance": {"model": "hostile"}}}
    assert decision_fingerprint(d) == decision_fingerprint(BASE)


def test_a_veto_is_tracked_separately_from_the_fingerprint():
    """A veto DOES change the outcome, so it is recorded beside the fingerprint
    rather than folded into it — merging them would destroy the ability to
    prove the property above."""
    d = copy.deepcopy(BASE)
    d["ai_review"] = {"vetoed": True,
                      "judge": {"verdict": "VETO", "provenance": {"model": "m"}}}
    assert ai_influence(d)["changed_outcome"] is True
    assert ai_influence(BASE)["changed_outcome"] is False


def test_ai_influence_reports_the_model_that_was_consulted():
    inf = ai_influence(BASE)
    assert inf["consulted"] and inf["model"] == "m1" and inf["provider"] == "p1"


def test_no_ai_is_reported_as_not_consulted():
    d = copy.deepcopy(BASE); d["ai_review"] = {}
    assert ai_influence(d)["consulted"] is False


# ============================================ identity ignores naming and time

@pytest.mark.parametrize("field", [
    "decision_id", "signal_id", "snapshot_id", "created_at", "completed_at",
])
def test_identifiers_and_timestamps_are_excluded(field):
    """Including them would make every fingerprint unique and the mechanism
    worthless."""
    assert fp(**{field: "something-completely-different"}) == decision_fingerprint(BASE)


def test_an_unknown_new_field_does_not_disturb_the_fingerprint():
    """The payload is an allowlist, not a denylist: a schema addition must not
    silently invalidate every historical fingerprint."""
    assert fp(some_future_field={"a": 1}) == decision_fingerprint(BASE)


# ============================================ but real decisions differ

@pytest.mark.parametrize("path,value", [
    (("snapshot", "price"), 999.0),
    (("snapshot", "symbol"), "OTHER"),
    (("candidate", "direction"), "SELL"),
    (("candidate", "entry"), 200.0),
    (("candidate", "total_score"), 99.0),
    (("candidate", "expected_value"), -1.0),
    (("risk_gate", "verdict"), "REJECT"),
    (("risk_gate", "approved_quantity"), 300.0),
    (("options_trace", "structure"), "vertical_debit"),
])
def test_a_different_decision_produces_a_different_fingerprint(path, value):
    d = copy.deepcopy(BASE)
    node = d
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    assert decision_fingerprint(d) != decision_fingerprint(BASE)


def test_changing_the_bars_changes_the_fingerprint():
    """Different input data is a different decision, even at the same price."""
    d = copy.deepcopy(BASE)
    d["snapshot"]["bars"][0]["c"] = 101.0
    assert decision_fingerprint(d) != decision_fingerprint(BASE)


def test_the_same_verdict_reached_by_different_checks_is_a_different_decision():
    d = copy.deepcopy(BASE)
    d["risk_gate"]["checks"] = [{"rule": "min_score", "passed": True}]
    assert decision_fingerprint(d) != decision_fingerprint(BASE)


def test_a_flipped_check_changes_the_fingerprint():
    d = copy.deepcopy(BASE)
    d["risk_gate"]["checks"][1]["passed"] = False
    assert decision_fingerprint(d) != decision_fingerprint(BASE)


def test_the_contract_chosen_is_part_of_the_identity():
    d = copy.deepcopy(BASE)
    d["options_trace"]["contract"]["strike"] = 110.0
    assert decision_fingerprint(d) != decision_fingerprint(BASE)


def test_max_loss_is_part_of_the_identity():
    d = copy.deepcopy(BASE)
    d["options_trace"]["sizing"]["max_loss_total"] = 5000.0
    assert decision_fingerprint(d) != decision_fingerprint(BASE)


# ============================================ payload shape

def test_the_payload_is_json_serialisable_and_versioned():
    p = deterministic_payload(BASE)
    assert json.loads(json.dumps(p)) == p
    assert p["version"] == FINGERPRINT_VERSION


def test_the_payload_contains_no_ai_section():
    """Structural: there is nowhere for AI output to enter the hash."""
    p = deterministic_payload(BASE)
    assert "ai" not in p and "ai_review" not in p
    assert "ai" not in json.dumps(p).lower().replace("chain", "")


def test_bars_are_digested_not_embedded():
    """Otherwise the payload grows with history length."""
    p = deterministic_payload(BASE)
    assert isinstance(p["market"]["bars_digest"], str)
    assert p["market"]["bar_count"] == 1
    assert "bars" not in p["market"]


def test_an_empty_decision_still_fingerprints_without_raising():
    """A rejected no-signal decision has no candidate; it is still identifiable."""
    assert len(decision_fingerprint({"state": "REJECTED"})) == LENGTH

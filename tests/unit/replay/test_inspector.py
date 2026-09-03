"""
Decision inspector.

The inspector is what a judge reads to answer "who decided this, and where did
it stop". Two properties matter above all others:

  1. It must never claim a stage was reached that the record does not evidence.
  2. It must keep the three authority classes separate. Conflating an AI opinion
     with deterministic authority is precisely the misunderstanding this whole
     project exists to prevent, so it is asserted, not assumed.

It is also fed deliberately broken records throughout, because a crash mid-cycle
leaves a partial one and an inspector that raises on incomplete data is useless
exactly when it is most needed.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import pytest  # noqa: E402

from speedtrader.replay.inspector import (  # noqa: E402
    Authority, StageState, evidence_for, inspect,
)


def decision(**kw):
    d = {
        "decision_id": "dec_1", "symbol": "SPY", "state": "EXECUTING",
        "rejection_stage": None, "rejection_reason": None,
        "snapshot": {"symbol": "SPY", "price": 600.0, "spread": 0.02,
                     "market_open": True, "regime": "TREND", "bars": [1, 2, 3],
                     "timestamp": "2026-09-03T15:30:00Z",
                     "source": {"vendor": "alpaca", "freshness": "fresh"}},
        "candidate": {"strategy_id": "S07", "direction": "BUY", "entry": 600.0,
                      "stop_loss": 594.0, "take_profit": 612.0,
                      "reward_risk": 2.0, "total_score": 58.0},
        "risk_gate": {"verdict": "PASS", "evaluated_at": "2026-09-03T15:30:01Z",
                      "approved_quantity": 3, "engine_version": "v1",
                      "checks": [{"rule": "min_score", "passed": True, "observed": 58},
                                 {"rule": "portfolio_heat", "passed": True,
                                  "observed": 0.0}]},
        "options_trace": {"structure": "long_single",
                          "contract": {"symbol": "SPY260930C00600000",
                                       "strike": 600.0,
                                       "expiration": "2026-09-30"},
                          "sizing": {"contracts": 3, "max_loss_total": 960.0},
                          "estimated_fees": {"model": "per_contract", "total": 2.4}},
        "ai_review": {"vetoed": False, "prompt_version": "veto-v1",
                      "judge": {"verdict": "CONFIRM", "confidence": 0.7,
                                "reasoning": "no disqualifying reason",
                                "concerns": [],
                                "provenance": {"model": "m1"}}},
    }
    d.update(kw)
    return d


def stage(result, key):
    return next((s for s in result.stages if s.key == key), None)


# ============================================ authority separation

def test_the_ai_review_is_advisory_and_risk_is_deterministic():
    """The single most important assertion in this file."""
    r = inspect(decision())
    assert stage(r, "ai_review").authority is Authority.ADVISORY
    assert stage(r, "risk").authority is Authority.DETERMINISTIC


def test_the_broker_stages_are_observed_not_decided():
    r = inspect(decision(
        execution={"state": "submitted", "broker_order_id": "b1",
                   "client_order_id": "st-x"},
        reconciliation={"state": "filled", "filled_quantity": 3,
                        "requested_quantity": 3}))
    assert stage(r, "execution").authority is Authority.BROKER
    assert stage(r, "execution").state is StageState.OBSERVED
    assert stage(r, "reconciliation").state is StageState.OBSERVED


def test_no_advisory_stage_is_ever_marked_deterministic():
    r = inspect(decision())
    advisory = {"debate", "ai_review"}
    for s in r.stages:
        if s.key in advisory:
            assert s.authority is Authority.ADVISORY, s.key


# ============================================ where it stopped

def test_a_veto_stops_the_pipeline_at_the_ai_review():
    r = inspect(decision(state="REJECTED", ai_review={
        "vetoed": True, "judge": {"verdict": "VETO", "confidence": 0.9,
                                  "reasoning": "earnings tomorrow"}}))
    assert r.blocked_at.key == "ai_review"
    assert r.reason_code == "AI_VETO"
    assert r.authority_that_stopped_it == "advisory"


def test_stages_after_the_block_are_not_reached_not_passed():
    """A stage that never ran must never render as having passed."""
    r = inspect(decision(state="REJECTED", ai_review={
        "vetoed": True, "judge": {"verdict": "VETO"}}))
    assert stage(r, "risk").state is StageState.NOT_REACHED


def test_a_failed_risk_check_blocks_and_names_the_exact_rule():
    r = inspect(decision(state="REJECTED", risk_gate={
        "verdict": "REJECT",
        "checks": [{"rule": "min_score", "passed": True, "observed": 58},
                   {"rule": "portfolio_heat", "passed": False, "observed": 0.9}]}))
    assert r.blocked_at.key == "risk"
    assert r.reason_code == "PORTFOLIO_HEAT"
    assert r.authority_that_stopped_it == "deterministic"


def test_a_missing_snapshot_blocks_at_the_data_stage():
    r = inspect(decision(snapshot=None, candidate=None, state="REJECTED"))
    assert r.blocked_at.key == "market"
    assert r.reason_code == "NO_SNAPSHOT"


def test_no_signal_blocks_at_quant_not_at_market():
    """The data arrived fine; the strategy found no setup."""
    r = inspect(decision(state="REJECTED", candidate=None, options_trace=None,
                         risk_gate=None, ai_review=None,
                         rejection_stage="rejected_by_quant"))
    assert stage(r, "market").state is StageState.PASSED
    assert r.blocked_at.key == "quant"


def test_an_accepted_decision_has_no_blocking_stage():
    r = inspect(decision())
    assert r.blocked_at is None
    assert r.accepted


# ============================================ honesty about what is absent

def test_an_unbuilt_stage_is_labelled_not_built_rather_than_passed():
    """This system has no analyst team. Rendering one as 'passed' would be a lie;
    omitting it silently would be misleading. It says NOT_BUILT."""
    r = inspect(decision(ai_review=None))
    assert stage(r, "ai_review").state is StageState.NOT_BUILT
    assert "only subtract" in stage(r, "ai_review").summary


def test_a_missing_debate_is_not_reported_as_a_debate_that_happened():
    r = inspect(decision())  # judge only, no bull/bear
    assert stage(r, "debate").state is StageState.NOT_BUILT


def test_a_real_debate_summarises_both_sides():
    r = inspect(decision(ai_review={
        "vetoed": False,
        "judge": {"verdict": "CONFIRM"},
        "bull": {"verdict": "CONFIRM", "confidence": 0.8, "concerns": []},
        "bear": {"verdict": "ABSTAIN", "confidence": 0.4,
                 "concerns": ["elevated volatility", "thin book"]}}))
    s = stage(r, "debate")
    assert s.state is StageState.PASSED
    assert "bull" in s.summary and "bear" in s.summary
    assert "2 concern" in s.summary


# ============================================ malformed and partial records

@pytest.mark.parametrize("missing", [
    "snapshot", "candidate", "risk_gate", "options_trace", "ai_review"])
def test_a_record_missing_any_section_still_inspects(missing):
    r = inspect(decision(**{missing: None}))
    assert r.stages


def test_a_record_that_is_not_a_mapping_is_refused_clearly():
    with pytest.raises(TypeError):
        inspect(["not", "a", "record"])


def test_an_empty_record_inspects_without_raising():
    r = inspect({})
    assert r.blocked_at is not None


@pytest.mark.parametrize("junk", [
    {"risk_gate": {"checks": "not a list"}},
    {"risk_gate": {"checks": [None, 3, "x"]}},
    {"ai_review": {"judge": "not a mapping"}},
    {"snapshot": {"source": "not a mapping"}},
])
def test_structurally_wrong_sections_do_not_raise(junk):
    inspect(decision(**junk))


def test_the_fingerprint_is_derived_not_trusted_from_the_record():
    """A record claiming its own fingerprint must not be believed."""
    r = inspect(decision(fingerprint="0000000000000000"))
    assert r.fingerprint and r.fingerprint != "0000000000000000"


# ============================================ evidence

def test_every_risk_check_becomes_verifiable_evidence():
    ev = evidence_for(decision())
    checks = [e for e in ev if e["evidence_id"].startswith("chk-")]
    assert len(checks) == 2
    assert all(e["verifiable"] and e["source_type"] == "computed" for e in checks)


def test_evidence_records_what_was_observed_not_just_the_verdict():
    ev = evidence_for(decision())
    heat = next(e for e in ev if e["claim"] == "portfolio_heat")
    assert heat["observed"] == 0.0


def test_a_failed_check_is_evidence_that_does_not_support():
    ev = evidence_for(decision(risk_gate={"verdict": "REJECT", "checks": [
        {"rule": "spread", "passed": False, "observed": 0.9}]}))
    assert ev[0]["supports"] is False


def test_no_evidence_is_invented_when_the_record_has_none():
    """A fabricated source count is worse than an empty list."""
    assert evidence_for({}) == []


def test_market_data_freshness_is_evidence_with_a_named_source():
    ev = evidence_for(decision())
    snap = next(e for e in ev if e["evidence_id"] == "snap-00")
    assert snap["source"] == "alpaca"
    assert snap["supports"] is True


def test_stale_market_data_is_evidence_that_does_not_support():
    d = decision()
    d["snapshot"]["source"]["freshness"] = "stale"
    snap = next(e for e in evidence_for(d) if e["evidence_id"] == "snap-00")
    assert snap["supports"] is False

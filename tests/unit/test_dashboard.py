"""
Command centre generator.

Two things matter here, and they are the reason every assertion below exists.

  1. It must NEVER present simulated numbers as real. A judge reading this page
     has to be able to tell broker truth from fixture data at a glance.
  2. It renders persisted decision data — including text produced by a language
     model and by a broker — so everything that reached the decision store is
     UNTRUSTED INPUT to a template.

The page also contains no JavaScript at all, which is asserted rather than
assumed: with no script element there is no execution path for a hostile string,
which is a stronger guarantee than escaping alone.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pytest  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "build_dashboard", ROOT / "scripts" / "build_dashboard.py")
dash = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dash)


def _stage_class(page: str, label: str) -> str:
    """The pipeline class applied to one stage cell.

    The class attribute precedes the label in the markup, so this matches the
    whole cell rather than slicing after the label.
    """
    match = re.search(
        r'<div class="pl (pl-[a-z]+)"[^>]*>(?:(?!</div>).)*?>' + label + "<",
        page, re.S)
    assert match, f"stage {label} not found in the pipeline"
    return match.group(1)


def decision(**kw):
    d = {
        "decision_id": "dec_1", "symbol": "DEMO", "state": "EXECUTING",
        "created_at": "2026-09-02T15:30:00Z", "rejection_stage": None,
        "rejection_reason": None,
        "snapshot": {"symbol": "DEMO", "price": 105.5, "bars": [],
                     "market_open": True, "regime": "TREND"},
        "candidate": {"strategy_id": "S07", "total_score": 58.0,
                      "direction": "BUY", "entry": 105.5, "stop_loss": 103.0,
                      "take_profit": 110.0, "reward_risk": 1.8,
                      "expected_value": 0.48},
        "risk_gate": {"verdict": "PASS", "checks": [
            {"rule": "min_score", "passed": True},
            {"rule": "portfolio_heat", "passed": True}]},
        "options_trace": {"contract": {"symbol": "DEMO260930C00105500",
                                       "strike": 105.5,
                                       "expiration": "2026-09-30"},
                          "sizing": {"contracts": 3, "max_loss_total": 960.0,
                                     "risk_budget": 1000.0}},
        "ai_review": {"vetoed": False, "judge": {
            "verdict": "CONFIRM", "reasoning": "ok",
            "provenance": {"model": "m1"}}},
    }
    d.update(kw)
    return d


ACCOUNT = {
    "account_number": "PA000TEST", "equity": 100_000.0, "cash": 100_000.0,
    "buying_power": 400_000.0, "options_level": 3, "pnl": 0.0,
    "orders": [], "positions": [],
}


# ============================================ honest labelling

def test_demo_output_is_labelled_simulated():
    """Presenting simulated numbers as real is the one unforgivable UI sin."""
    page = dash.build([decision()], simulated=True)
    assert "SIMULATED" in page


def test_live_output_is_still_labelled_paper():
    """Even real broker data is a simulation; it must never read as real money."""
    page = dash.build([decision()], simulated=False)
    assert "PAPER" in page


def test_the_footer_disclaims_any_profitability_claim():
    page = dash.build([decision()], simulated=True)
    low = page.lower()
    assert "no performance claim" in low
    assert "paper trading is a simulation" in low


def test_pnl_is_described_as_measured_never_projected():
    """The judging criterion is P&L, which is exactly why it must not be dressed up."""
    page = dash.build([decision()], simulated=False, account=ACCOUNT)
    low = page.lower()
    assert "measured" in low
    assert "never projected" in low or "not projected" in low


def test_a_zero_pnl_is_shown_as_zero_not_hidden():
    page = dash.build([decision()], simulated=False, account=ACCOUNT)
    assert "$0.00" in page


def test_an_unreachable_broker_omits_figures_rather_than_inventing_them():
    page = dash.build([decision()], simulated=False, account=None)
    assert "not reachable" in page.lower()
    assert "omitted rather than estimated" in page.lower()


# ============================================ the three authority lanes

def test_the_three_authority_lanes_are_rendered_separately():
    """The whole architecture is that these are different things."""
    page = dash.build([decision()], simulated=True)
    assert "AI · ADVISORY" in page
    assert "DETERMINISTIC · AUTHORITY" in page
    assert "EXECUTION · OUTCOME" in page


def test_the_ai_lane_states_it_cannot_authorize():
    page = dash.build([decision()], simulated=True)
    assert "cannot authorize" in page


def test_a_veto_is_shown_as_the_ai_changing_the_outcome():
    vetoed = decision(state="REJECTED", ai_review={
        "vetoed": True,
        "judge": {"verdict": "VETO", "reasoning": "earnings tomorrow"}})
    page = dash.build([vetoed], simulated=True)
    assert "changed outcome: <b>YES" in page
    assert "AI VETO" in page


def test_a_confirm_shows_the_ai_changed_nothing():
    page = dash.build([decision()], simulated=True)
    assert "changed outcome: <b>NO</b>" in page


def test_submitted_is_never_presented_as_filled():
    page = dash.build([decision()], simulated=True)
    assert "SUBMITTED is never FILLED" in page


# ============================================ reproducibility

def test_the_deterministic_fingerprint_is_displayed():
    from speedtrader.replay.fingerprint import decision_fingerprint
    d = decision()
    page = dash.build([d], simulated=True)
    assert decision_fingerprint(d)[:16] in page


# ============================================ why we did NOT trade

def test_why_we_did_not_trade_attributes_each_rejection_to_a_layer():
    decisions = [
        decision(state="REJECTED", rejection_stage="risk_engine"),
        decision(state="REJECTED", ai_review={"vetoed": True, "judge": {}}),
        decision(state="REJECTED", candidate=None, rejection_stage=None),
    ]
    page = dash.build(decisions, simulated=True)
    assert "AI veto" in page
    assert "deterministic risk engine" in page
    assert "no signal" in page


def test_the_same_layer_is_never_split_across_two_rows():
    """Raw stage enums and the fallback label used to produce two rows for one
    layer, which made the attribution look like more distinct causes than exist."""
    decisions = [
        decision(state="REJECTED", rejection_stage="REJECTED_BY_RISK_ENGINE"),
        decision(state="REJECTED", rejection_stage="risk_engine"),
    ]
    page = dash.build(decisions, simulated=True)
    assert page.count("deterministic risk engine") == 1
    assert "REJECTED_BY_RISK_ENGINE" not in page


def test_a_signal_that_never_fired_blames_quant_not_the_data_layer():
    """The market data arrived fine; the strategy simply found no setup."""
    d = decision(state="REJECTED", candidate=None, options_trace=None,
                 risk_gate=None, ai_review=None)
    page = dash.build([d], simulated=True)
    assert _stage_class(page, "MARKET") == "pl-done"
    assert _stage_class(page, "QUANT") == "pl-stop", "quant is where it stopped"


def test_a_correct_no_trade_is_framed_as_the_system_working():
    page = dash.build([decision(state="REJECTED")], simulated=True)
    assert "declines is working" in page


# ============================================ order lifecycle

def test_the_order_lifecycle_table_shows_intent_and_broker_state_separately():
    """The two can disagree — that disagreement is the whole reason to show both."""
    intents = [{"client_order_id": "st-abc", "phase": "unknown", "symbol": "SPY",
                "quantity": 1, "detail": "timeout"}]
    page = dash.build([decision()], simulated=False, intents=intents)
    assert "st-abc" in page
    assert "intent phase" in page
    assert "broker state" in page


def test_a_broker_order_with_no_local_intent_is_shown_not_hidden():
    """History predating the journal is evidence; deleting it is not cleanup."""
    account = {**ACCOUNT, "orders": [{
        "symbol": "F261002C00013500", "status": "new", "qty": "1",
        "filled_qty": "0", "limit_price": "0.98",
        "client_order_id": "st-old", "submitted_at": ""}]}
    page = dash.build([decision()], simulated=False, account=account)
    assert "st-old" in page
    assert "predates the intent journal" in page


def test_no_execution_attempt_is_stated_as_a_fact_not_an_error():
    page = dash.build([decision()], simulated=False, intents=[])
    assert "factual state" in page


def test_the_write_ahead_guarantee_is_explained_on_the_page():
    page = dash.build([decision()], simulated=False)
    assert "before" in page.lower() and "write-ahead" in page.lower()


# ============================================ untrusted input

@pytest.mark.parametrize("payload", [
    "<script>alert(1)</script>", "\"><img src=x onerror=alert(1)>",
    "</td></tr><tr><td>injected", "<b>bold</b>",
])
def test_decision_data_is_escaped_not_injected(payload):
    page = dash.build([decision(symbol=payload)], simulated=True)
    assert payload not in page
    assert "&lt;" in page or "&quot;" in page or "&gt;" in page


def test_a_hostile_ai_reasoning_string_cannot_inject_markup():
    """Model output is the least trusted text on the page."""
    hostile = decision(ai_review={"vetoed": True, "judge": {
        "verdict": "VETO", "reasoning": "<script>steal()</script>"}})
    page = dash.build([hostile], simulated=True)
    assert "<script>steal()</script>" not in page


def test_a_hostile_check_rule_name_cannot_inject_markup():
    hostile = decision(risk_gate={"verdict": "PASS", "checks": [
        {"rule": "<script>x()</script>", "passed": True, "observed": 1}]})
    page = dash.build([hostile], simulated=True)
    assert "<script>x()</script>" not in page


def test_the_page_is_self_contained_and_has_no_javascript():
    """No CDN, no external script, no network — a judge just opens the file.

    The absence of any <script> is a security property, not a style choice:
    this page renders model- and broker-authored text, and with no script
    element a hostile string has no execution path at all.
    """
    page = dash.build([decision()], simulated=True, account=ACCOUNT,
                      intents=[{"client_order_id": "x", "phase": "submitted"}])
    for external in ("http://", "https://", "<script", "cdn.", "onerror=",
                     "onclick=", "javascript:"):
        assert external not in page, f"page reaches for {external}"


# ============================================ robustness

def test_an_empty_store_renders_without_raising():
    page = dash.build([], simulated=True)
    assert "No decisions recorded yet" in page


@pytest.mark.parametrize("missing", [
    "candidate", "risk_gate", "options_trace", "ai_review", "snapshot"])
def test_a_decision_missing_any_section_still_renders(missing):
    """A partial record is what a crash mid-cycle leaves behind."""
    page = dash.build([decision(**{missing: None})], simulated=True)
    assert "SpeedTrader AI" in page


def test_a_no_signal_decision_renders_the_pipeline_as_stopped_early():
    d = decision(state="REJECTED", candidate=None, options_trace=None,
                 risk_gate=None, ai_review=None)
    page = dash.build([d], simulated=True)
    assert "pl-todo" in page, "later stages must render as not reached"


def test_corrupt_lines_are_skipped_not_fatal(tmp_path):
    f = tmp_path / "decisions-2026-09-02.jsonl"
    f.write_text('{"decision_id":"a","symbol":"X"}\nnot json at all\n')
    loaded = dash.load_decisions(tmp_path)
    assert len(loaded) == 1 and loaded[0]["decision_id"] == "a"


def test_a_corrupt_intent_line_is_skipped(tmp_path):
    (tmp_path / "execution_intents.jsonl").write_text(
        '{"client_order_id":"a","phase":"attempted"}\ntruncated{\n')
    assert len(dash.load_intents(tmp_path)) == 1


def test_a_missing_intent_journal_is_not_an_error(tmp_path):
    assert dash.load_intents(tmp_path) == []


# ============================================ credentials

def test_no_credential_value_can_reach_the_page(monkeypatch):
    """The account panel renders an account NUMBER; keys must never appear.

    The sentinels below deliberately do NOT match the credential-shaped regex
    that CI scans the repository with (`(AK|PK|SK)[A-Z0-9]{18,}`) — a test
    fixture that looks exactly like a real key trips every secret scanner it
    passes, and a scanner people learn to ignore is worse than none. The
    underscores break the pattern while keeping the test honest: these are the
    values in the environment, and they must not appear on the page.
    """
    fake_key = "PK_FAKE_TEST_KEY_NOT_A_CREDENTIAL"
    fake_secret = "SK_FAKE_TEST_SECRET_NOT_A_CREDENTIAL"
    monkeypatch.setenv("ALPACA_API_KEY", fake_key)
    monkeypatch.setenv("ALPACA_SECRET_KEY", fake_secret)
    page = dash.build([decision()], simulated=False, account=ACCOUNT)
    assert fake_key not in page
    assert fake_secret not in page
    # The account number is the only account identifier that may be shown.
    assert ACCOUNT["account_number"] in page

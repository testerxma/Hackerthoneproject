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
    # Scoped to the attribution chart: the phrase legitimately appears
    # elsewhere now (stage labels in the decision inspector), and the property
    # under test is specifically that ONE layer produces ONE bar.
    chart = page.split("Why we did NOT trade")[1].split("</div>\n</div>")[0]
    assert chart.count('class="bar-l"') == 1, "one layer must be one bar"
    assert "deterministic risk engine" in chart
    assert "REJECTED_BY_RISK_ENGINE" not in chart


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


# ============================================ decision inspector
# The panel a judge reads to answer "who decided this, and where did it stop".

def test_the_inspector_labels_the_three_authority_classes():
    page = dash.build([decision()], simulated=True)
    assert "advisory" in page and "deterministic" in page and "broker" in page
    assert "can veto, never authorizes" in page
    assert "the only execution authority" in page


def test_a_veto_is_attributed_to_the_advisory_layer_not_the_risk_engine():
    """The single most consequential thing a judge could misread."""
    page = dash.build([decision(state="REJECTED", ai_review={
        "vetoed": True, "judge": {"verdict": "VETO", "reasoning": "x"}})],
        simulated=True)
    assert "stopped at" in page.lower()
    assert "advisory" in page


def test_the_inspector_shows_the_exact_deterministic_reason_code():
    page = dash.build([decision(state="REJECTED", risk_gate={
        "verdict": "REJECT",
        "checks": [{"rule": "portfolio_heat", "passed": False, "observed": 0.9}]})],
        simulated=True)
    assert "PORTFOLIO_HEAT" in page, "the exact rule, never a paraphrase"


def test_stages_after_a_block_render_as_not_reached():
    page = dash.build([decision(state="REJECTED", ai_review={
        "vetoed": True, "judge": {"verdict": "VETO"}})], simulated=True)
    assert "not reached" in page


def test_an_unbuilt_stage_is_labelled_rather_than_silently_omitted():
    page = dash.build([decision(ai_review=None)], simulated=True)
    assert "not built" in page


# ============================================ bull vs bear

def test_the_debate_panel_shows_both_sides_and_their_concerns():
    page = dash.build([decision(ai_review={
        "vetoed": False,
        "judge": {"verdict": "CONFIRM", "reasoning": "ok"},
        "bull": {"verdict": "CONFIRM", "confidence": 0.8, "reasoning": "breakout",
                 "concerns": []},
        "bear": {"verdict": "ABSTAIN", "confidence": 0.4, "reasoning": "vol",
                 "concerns": ["elevated volatility", "thin book"]}})],
        simulated=True)
    assert "BULL CASE" in page and "BEAR CASE" in page
    assert "elevated volatility" in page and "thin book" in page


def test_the_debate_panel_is_absent_when_no_debate_was_run():
    """Better absent than an empty box implying a debate happened."""
    page = dash.build([decision()], simulated=True)
    assert "BULL CASE" not in page


def test_a_hostile_bear_concern_cannot_inject_markup():
    page = dash.build([decision(ai_review={
        "vetoed": False, "judge": {"verdict": "CONFIRM"},
        "bear": {"verdict": "VETO", "concerns": ["<script>x()</script>"]}})],
        simulated=True)
    assert "<script>x()</script>" not in page


# ============================================ evidence

def test_every_risk_check_appears_as_verifiable_evidence():
    page = dash.build([decision()], simulated=True)
    assert "Evidence &amp; provenance" in page
    assert "chk-00" in page


def test_evidence_states_plainly_that_no_news_layer_exists():
    """A fabricated source count would be worse than an honest absence."""
    page = dash.build([decision()], simulated=True)
    assert "No news, sentiment or fundamental evidence layer exists" in page


def test_evidence_shows_what_was_observed_not_just_a_verdict():
    page = dash.build([decision(risk_gate={"verdict": "PASS", "checks": [
        {"rule": "spread", "passed": True, "observed": 0.0123}]})], simulated=True)
    assert "0.0123" in page


def test_a_hostile_check_rule_cannot_inject_through_the_evidence_table():
    page = dash.build([decision(risk_gate={"verdict": "PASS", "checks": [
        {"rule": "<img src=x onerror=alert(1)>", "passed": True,
         "observed": 1}]})], simulated=True)
    assert "<img src=x" not in page


# ============================================ still no JavaScript

def test_the_new_panels_add_no_javascript():
    """Every new panel is CSS-only. This is a security property, not a style."""
    page = dash.build([decision(ai_review={
        "vetoed": True,
        "judge": {"verdict": "VETO", "reasoning": "r"},
        "bull": {"verdict": "CONFIRM", "concerns": ["a"]},
        "bear": {"verdict": "VETO", "concerns": ["b"]}})],
        simulated=False, account=ACCOUNT,
        intents=[{"client_order_id": "x", "phase": "submitted"}])
    for bad in ("<script", "http://", "https://", "onerror=", "onclick=",
                "javascript:"):
        assert bad not in page, f"page reaches for {bad}"


# ============================================ system health
# A status panel that guesses is worse than none, because it gets trusted.

def test_an_uncontacted_broker_is_unknown_not_healthy(tmp_path):
    page = dash.build([decision()], simulated=False, account=None,
                      journal_dir=tmp_path)
    assert "UNKNOWN" in page
    assert "not contacted by this build" in page


def test_a_contacted_broker_reports_connected_with_the_account_number(tmp_path):
    page = dash.build([decision()], simulated=False, account=ACCOUNT,
                      journal_dir=tmp_path)
    assert "CONNECTED" in page
    assert ACCOUNT["account_number"] in page


def test_an_unresolved_intent_is_reported_as_pending_not_clear(tmp_path):
    """This is the state that blocks the next run; it must be visible."""
    page = dash.build([decision()], simulated=False, journal_dir=tmp_path,
                      intents=[{"client_order_id": "st-a", "phase": "unknown"}])
    assert "PENDING" in page
    assert "refuses to trade until these are settled" in page


def test_a_settled_intent_reports_clear(tmp_path):
    page = dash.build([decision()], simulated=False, journal_dir=tmp_path,
                      intents=[{"client_order_id": "st-a", "phase": "attempted"},
                               {"client_order_id": "st-a", "phase": "reconciled"}])
    assert "every intent settled" in page


def test_the_kill_switch_reports_engaged_when_the_file_exists(tmp_path):
    (tmp_path / "STOP").write_text("")
    page = dash.build([decision()], simulated=False, journal_dir=tmp_path)
    assert "ENGAGED" in page


def test_the_kill_switch_reports_armed_when_absent(tmp_path):
    page = dash.build([decision()], simulated=False, journal_dir=tmp_path)
    assert "ARMED" in page


def test_the_risk_engine_is_always_described_as_authoritative(tmp_path):
    page = dash.build([decision()], simulated=False, journal_dir=tmp_path)
    assert "AUTHORITATIVE" in page
    assert "cannot be overridden by the AI" in page


def test_an_unconsulted_ai_is_not_reported_as_available(tmp_path):
    page = dash.build([decision(ai_review=None)], simulated=False,
                      journal_dir=tmp_path)
    assert "NOT CONSULTED" in page


# ============================================ audit / reproducibility

def test_the_audit_panel_states_what_the_fingerprint_excludes():
    page = dash.build([decision()], simulated=True)
    assert "excluded" in page and "by construction" in page


def test_the_audit_panel_refuses_to_claim_deterministic_llm_replay():
    """Claiming a reproducible model would be the easiest lie to tell here."""
    page = dash.build([decision()], simulated=True)
    assert "Not claimed:" in page
    assert "deterministic replay of an LLM response" in page


def test_the_audit_panel_marks_a_veto_as_the_ai_changing_the_outcome():
    page = dash.build([decision(state="REJECTED", ai_review={
        "vetoed": True, "judge": {"verdict": "VETO"}})], simulated=True)
    assert "AI changed outcome" in page


# ============================================ options opportunity inspector
# Options are the hackathon's core requirement: "why THIS contract" must be
# answerable, and the max-loss figure must stay true.

def _opt_decision(**kw):
    d = decision()
    d["options_trace"] = {
        "structure": "long_single",
        "contract": {"symbol": "DEMO260930C00105500", "type": "call",
                     "strike": 105.5, "expiration": "2026-09-30",
                     "multiplier": 100, "open_interest": 800,
                     "bid": 3.0, "ask": 3.2},
        "selection": {"considered": 3,
                      "reason": "nearest-the-money call at strike 105.5"},
        "sizing": {"contracts": 3, "premium_per_contract": 3.2,
                   "max_loss_per_contract": 320.0, "max_loss_total": 960.0,
                   "risk_budget": 1000.0, "caps_applied": []},
        "estimated_fees": {"model": "per_contract", "total": 2.4},
    }
    d["snapshot"]["timestamp"] = "2026-09-03T15:30:00Z"
    d.update(kw)
    return d


def test_the_options_panel_shows_the_full_quote_not_just_the_ask():
    page = dash.build([_opt_decision()], simulated=True)
    assert "BID / ASK" in page and "SPREAD" in page
    assert "priced at the <b>ask</b>" in page


def test_max_loss_is_shown_all_in_and_labelled_exact():
    page = dash.build([_opt_decision()], simulated=True)
    assert "962.40" in page, "premium plus fees, not premium alone"
    assert "EXACT" in page and "ESTIMATED" in page


def test_max_profit_is_never_printed_as_a_number():
    """A big figure beside an exact max loss invites a false expectation."""
    page = dash.build([_opt_decision()], simulated=True)
    assert "unbounded for a long call" in page
    assert "Max profit:" in page


def test_the_panel_explains_why_this_contract_was_selected():
    page = dash.build([_opt_decision()], simulated=True)
    assert "Selected because:" in page
    assert "nearest-the-money" in page
    assert "3 contract(s) considered" in page


def test_thin_open_interest_is_flagged_rather_than_shown_bare():
    d = _opt_decision()
    d["options_trace"]["contract"]["open_interest"] = 12
    assert "thin" in dash.build([d], simulated=True)


def test_a_decision_with_no_contract_renders_no_options_panel():
    page = dash.build([decision(options_trace=None)], simulated=True)
    assert "WHY THIS CONTRACT" not in page


def test_a_hostile_selection_reason_cannot_inject_markup():
    d = _opt_decision()
    d["options_trace"]["selection"]["reason"] = "<script>x()</script>"
    assert "<script>x()</script>" not in dash.build([d], simulated=True)


# ============================================ why / why-not cards

def test_an_authorized_decision_gets_a_why_this_trade_card():
    page = dash.build([_opt_decision()], simulated=True)
    assert "WHY THIS TRADE?" in page
    assert "AUTHORIZED</b> by the deterministic layer, not by the AI" in page


def test_a_rejected_decision_gets_a_why_not_trade_card_naming_the_blocker():
    page = dash.build([decision(state="REJECTED", risk_gate={
        "verdict": "REJECT",
        "checks": [{"rule": "portfolio_heat", "passed": False, "observed": 0.9}]})],
        simulated=True)
    assert "WHY NOT TRADE?" in page
    assert "EXECUTION BLOCKED" in page
    assert "PORTFOLIO_HEAT" in page


def test_a_veto_card_attributes_the_block_to_the_advisory_layer():
    page = dash.build([decision(state="REJECTED", ai_review={
        "vetoed": True, "judge": {"verdict": "VETO", "reasoning": "r"}})],
        simulated=True)
    assert "WHY NOT TRADE?" in page
    assert "advisory layer" in page


def test_the_card_counts_evidence_both_ways():
    page = dash.build([decision(risk_gate={"verdict": "PASS", "checks": [
        {"rule": "a", "passed": True, "observed": 1},
        {"rule": "b", "passed": False, "observed": 2}]})], simulated=True)
    assert "supporting" in page and "contradicting" in page


def test_the_new_panels_still_add_no_javascript():
    page = dash.build([_opt_decision()], simulated=False, account=ACCOUNT,
                      intents=[{"client_order_id": "x", "phase": "submitted"}])
    for bad in ("<script", "http://", "https://", "onerror=", "onclick=",
                "javascript:"):
        assert bad not in page, f"page reaches for {bad}"


# ============================================ analytics honesty
# The account has zero fills. These panels must make that look like a factual
# state professionally reported, never like a gap to be filled with a statistic.

def test_the_funnel_withholds_rates_below_the_sample_threshold():
    page = dash.build([decision() for _ in range(3)], simulated=True)
    assert "Counts only." in page
    assert f"{dash.MIN_SAMPLE_FOR_RATES}-decision threshold" in page


def test_the_funnel_reports_rates_once_the_sample_is_large_enough():
    page = dash.build([decision() for _ in range(dash.MIN_SAMPLE_FOR_RATES)],
                      simulated=True)
    assert "Counts only." not in page
    assert "not performance" in page


def test_the_funnel_counts_are_real_not_derived_from_a_rate():
    page = dash.build([decision(), decision(state="REJECTED", ai_review={
        "vetoed": True, "judge": {"verdict": "VETO"}})], simulated=True)
    assert "Quant produced a signal" in page
    assert "AI vetoed" in page


def test_strategy_analytics_never_claims_a_win_rate():
    # Whitespace-normalised: the copy wraps across lines in the generated HTML.
    page = " ".join(dash.build([decision()], simulated=True).split())
    assert "no performance claim" in page
    assert "undefined without resolved trades" in page


def test_strategy_analytics_says_why_it_withholds_rather_than_printing_zero():
    """Printing 0.0 would invite reading absence of evidence as evidence."""
    page = dash.build([decision()], simulated=True)
    assert "Printing 0.0 would invite" in page


def test_no_strategy_signal_is_stated_rather_than_shown_as_an_empty_table():
    page = dash.build([decision(candidate=None, state="REJECTED")], simulated=True)
    assert "No strategy produced a signal" in page


@pytest.mark.parametrize("word", ["sharpe", "alpha", "win rate", "profitab"])
def test_no_performance_metric_is_ever_asserted_as_a_value(word):
    """Each of these may appear only as a disclaimer, never as a number."""
    page = dash.build([decision() for _ in range(25)], simulated=True).lower()
    idx = page.find(word)
    while idx != -1:
        context = page[max(0, idx - 120):idx + 60]
        assert any(neg in context for neg in
                   ("no ", "not ", "never", "undefined", "without", "insufficient")), \
            f"'{word}' appears without a negating qualifier: {context!r}"
        idx = page.find(word, idx + 1)


# ============================================ AI provider honesty
# Found by looking at the rendered page: a placeholder was being listed as a
# model name, and a scripted stand-in was reported as an available AI.

def test_a_placeholder_is_never_listed_as_a_model_name(tmp_path):
    """provenance.model == "none" means no model answered, not a model called
    "none"."""
    d = decision(ai_review={"vetoed": False, "judge": {
        "verdict": "ABSTAIN", "provenance": {"model": "none"}}})
    page = dash.build([d], simulated=False, journal_dir=tmp_path)
    assert "model(s): none" not in page
    assert "NOT CONSULTED" in page


@pytest.mark.parametrize("placeholder", ["", "none", "N/A", "unknown", "-"])
def test_every_placeholder_form_is_treated_as_no_model(tmp_path, placeholder):
    d = decision(ai_review={"vetoed": False, "judge": {
        "verdict": "ABSTAIN", "provenance": {"model": placeholder}}})
    page = dash.build([d], simulated=False, journal_dir=tmp_path)
    assert "NOT CONSULTED" in page


def test_a_scripted_provider_is_not_reported_as_an_available_ai(tmp_path):
    """A deterministic stand-in is not a language model; calling it AVAILABLE
    would overstate what actually reviewed these decisions."""
    d = decision(ai_review={"vetoed": False, "judge": {
        "verdict": "CONFIRM", "provenance": {"model": "scripted-demo-1"}}})
    page = dash.build([d], simulated=False, journal_dir=tmp_path)
    assert "SCRIPTED" in page
    assert "not a language model" in page
    assert "No LLM reviewed these decisions" in page


def test_a_real_model_is_reported_as_available_and_advisory_only(tmp_path):
    d = decision(ai_review={"vetoed": False, "judge": {
        "verdict": "CONFIRM", "provenance": {"model": "claude-x"}}})
    page = dash.build([d], simulated=False, journal_dir=tmp_path)
    assert "AVAILABLE" in page
    assert "claude-x" in page
    assert "cannot authorize" in page


# ==================================== the strip must blame the right layer
#
# The pipeline strip is read as a causal chain, so a wrong stop point is not a
# cosmetic bug — it accuses a layer that never saw the decision. Two ways it
# used to lie, both pinned below:
#
#   * The strip listed the AI before options and risk, inverting the one claim
#     this project makes about where authority lives.
#   * It derived the broker outcome from an `execution` block the orchestrator
#     never persists, so every decision that actually reached Alpaca rendered
#     as though something upstream had stopped it.


def test_the_strip_runs_in_the_order_the_code_runs():
    """Risk and options precede the AI, because the AI reviews an approved trade."""
    order = [label for label, _ in dash.STAGES]
    assert order.index("RISK") < order.index("AI VETO")
    assert order.index("OPTIONS") < order.index("AI VETO")
    assert order.index("AI VETO") < order.index("EXECUTION")


def test_no_tradeable_contract_blames_the_options_layer_not_the_quant():
    d = decision(state="REJECTED", rejection_stage="REJECTED_BY_RISK_ENGINE",
                 rejection_reason="no tradeable contract: spread too wide",
                 options_trace=None, ai_review=None)
    page = dash.build([d], simulated=True)
    assert _stage_class(page, "QUANT") == "pl-done", "S07 did fire"
    assert _stage_class(page, "OPTIONS") == "pl-stop"


def test_an_unaffordable_contract_blames_the_options_layer():
    d = decision(state="REJECTED", rejection_stage="REJECTED_BY_RISK_ENGINE",
                 rejection_reason="options sizing rejected: one contract risks "
                                  "6200.00, over the 1000.00 budget",
                 options_trace=None, ai_review=None)
    page = dash.build([d], simulated=True)
    assert _stage_class(page, "OPTIONS") == "pl-stop"


def test_a_submitted_decision_reaches_execution_without_an_execution_block():
    """`execution` is never persisted; EXECUTING is the evidence of submission."""
    d = decision(state="EXECUTING")
    assert not d.get("execution")
    page = dash.build([d], simulated=True)
    assert _stage_class(page, "AUTH") == "pl-done"
    assert _stage_class(page, "EXECUTION") == "pl-done"


def test_an_unknown_outcome_is_never_drawn_as_never_submitted():
    """The dangerous direction: an order may exist at the broker."""
    d = decision(state="FAILED",
                 rejection_reason="execution outcome UNKNOWN, reconcile before "
                                  "retrying: no response in 5s")
    page = dash.build([d], simulated=True)
    assert _stage_class(page, "EXECUTION") == "pl-stop"
    assert "UNKNOWN" in page
    assert "an order may exist" in page
    assert "not submitted" not in page


def test_a_refused_licence_stops_before_the_broker():
    d = decision(state="FAILED",
                 rejection_reason="authorization refused: ValueError: stale")
    page = dash.build([d], simulated=True)
    assert _stage_class(page, "AUTH") == "pl-stop"
    assert _stage_class(page, "EXECUTION") == "pl-todo"
    assert "authorization refused before the broker was contacted" in page

"""
Dashboard generator.

Two things matter here. It must never present simulated numbers as real, and it
renders persisted decision data into HTML — so anything that reached the decision
store is untrusted input to a template.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pytest  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "build_dashboard", ROOT / "scripts" / "build_dashboard.py")
dash = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dash)


def decision(**kw):
    d = {
        "decision_id": "dec_1", "symbol": "DEMO", "state": "EXECUTING",
        "created_at": "2026-09-02T15:30:00Z", "rejection_stage": None,
        "rejection_reason": None,
        "snapshot": {"symbol": "DEMO", "price": 105.5, "bars": []},
        "candidate": {"strategy_id": "S07", "total_score": 58.0,
                      "expected_value": 0.48},
        "risk_gate": {"verdict": "PASS", "checks": [
            {"rule": "min_score", "passed": True},
            {"rule": "portfolio_heat", "passed": True}]},
        "options_trace": {"contract": {"symbol": "DEMO260930C00105500"},
                          "sizing": {"contracts": 3, "max_loss_total": 960.0,
                                     "risk_budget": 1000.0}},
        "ai_review": {"vetoed": False, "judge": {
            "verdict": "CONFIRM", "reasoning": "ok",
            "provenance": {"model": "m1"}}},
    }
    d.update(kw)
    return d


# ============================================ honest labelling

def test_demo_output_is_labelled_simulated():
    """Presenting simulated numbers as real is the one unforgivable UI sin."""
    page = dash.build([decision()], simulated=True)
    assert "SIMULATED" in page and "No real capital" in page


def test_live_output_is_still_labelled_paper():
    page = dash.build([decision()], simulated=False)
    assert "PAPER TRADING" in page and "Not real capital" in page


def test_the_footer_disclaims_any_profitability_claim():
    page = dash.build([decision()], simulated=True)
    assert "no claim is made" in page.lower()


# ============================================ the central idea is visible

def test_the_three_authority_lanes_are_rendered_separately():
    """A judge must see AI vs deterministic vs execution without reading code."""
    page = dash.build([decision()], simulated=True)
    for lane in ("lane advisory", "lane authority", "lane outcome"):
        assert lane in page
    assert "AI · advisory" in page
    assert "DETERMINISTIC · authority" in page
    assert "EXECUTION · outcome" in page


def test_a_veto_is_shown_as_the_ai_changing_the_outcome():
    d = decision(state="REJECTED", rejection_stage="REJECTED_BY_RISK_AGENT",
                 rejection_reason="AI veto: earnings in 2 days",
                 ai_review={"vetoed": True, "judge": {
                     "verdict": "VETO", "reasoning": "earnings in 2 days",
                     "provenance": {"model": "m1"}}})
    page = dash.build([d], simulated=True)
    assert "VETO" in page
    assert "changed outcome: <b>YES</b>" in page


def test_a_confirm_shows_the_ai_changed_nothing():
    page = dash.build([decision()], simulated=True)
    assert "changed outcome: <b>NO</b>" in page


def test_the_deterministic_fingerprint_is_displayed():
    page = dash.build([decision()], simulated=True)
    from speedtrader.replay.fingerprint import decision_fingerprint
    assert decision_fingerprint(decision()) in page


def test_why_we_did_not_trade_attributes_each_rejection_to_a_layer():
    """Most systems only show the trades they took."""
    page = dash.build([
        decision(state="REJECTED", rejection_stage="REJECTED_BY_QUANT"),
        decision(state="REJECTED", rejection_stage="REJECTED_BY_RISK_ENGINE"),
        decision(state="REJECTED", rejection_stage="REJECTED_BY_RISK_ENGINE"),
    ], simulated=True)
    assert "Why we did NOT trade" in page
    assert "Risk_Engine" in page and "Quant" in page


# ============================================ untrusted data -> HTML

@pytest.mark.parametrize("payload", [
    "<script>alert(1)</script>",
    '"><img src=x onerror=alert(1)>',
    "</title><script>x</script>",
])
def test_decision_data_is_escaped_not_injected(payload):
    """Anything in the decision store is untrusted input to this template."""
    page = dash.build([decision(symbol=payload)], simulated=True)
    assert payload not in page
    assert "&lt;script&gt;" in page or "&lt;" in page


def test_a_hostile_ai_reasoning_string_cannot_inject_markup():
    d = decision(ai_review={"vetoed": False, "judge": {
        "verdict": "CONFIRM", "reasoning": "<script>steal()</script>",
        "provenance": {"model": "m"}}})
    page = dash.build([d], simulated=True)
    assert "<script>steal()</script>" not in page


# ============================================ robustness

def test_an_empty_store_renders_without_raising():
    page = dash.build([], simulated=True)
    assert "no decisions yet" in page


@pytest.mark.parametrize("missing", [
    "candidate", "risk_gate", "options_trace", "ai_review", "snapshot",
])
def test_a_decision_missing_any_section_still_renders(missing):
    """A rejected decision legitimately has no candidate or contract."""
    d = decision(**{missing: None})
    page = dash.build([d], simulated=True)
    assert "<article class=\"card\">" in page


def test_a_no_signal_decision_renders_the_pipeline_as_stopped_early():
    d = decision(state="REJECTED", candidate=None, risk_gate=None,
                 options_trace=None, rejection_stage="REJECTED_BY_QUANT")
    page = dash.build([d], simulated=True)
    assert "no signal" in page


def test_the_page_is_self_contained():
    """No CDN, no external script, no network. A judge just opens the file."""
    page = dash.build([decision()], simulated=True)
    for external in ("http://", "https://", "<script", "cdn."):
        assert external not in page, f"page reaches for {external}"


def test_corrupt_lines_are_skipped_not_fatal(tmp_path):
    f = tmp_path / "decisions-2026-09-02.jsonl"
    f.write_text('{"decision_id":"a","symbol":"X"}\nnot json at all\n')
    loaded = dash.load_decisions(tmp_path)
    assert len(loaded) == 1 and loaded[0]["decision_id"] == "a"


# ============================================ charts

def test_bars_renders_one_mark_per_category():
    svg = dash.bars([("Quant", 2), ("Risk", 5)])
    assert svg.count("<rect") == 2
    assert "Quant" in svg and "Risk" in svg


def test_bars_labels_every_value_directly():
    """Direct labels mean identity is never carried by colour alone."""
    svg = dash.bars([("A", 3)])
    assert ">3<" in svg


def test_an_empty_chart_says_so_rather_than_drawing_nothing():
    assert "no decisions" in dash.bars([])


def test_a_single_point_series_is_not_plotted_as_a_line():
    assert "not enough data" in dash.sparkline([1.0], label="equity")

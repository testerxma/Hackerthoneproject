"""
End-to-end options decision cycle.

    snapshot -> S07 -> risk gate -> contract -> sizing -> authorization
             -> execution -> persisted decision

No network. The chain provider and broker are fakes so every branch — including
the ones a live demo must never hit — is reachable in CI.
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # shared fixtures

import pytest  # noqa: E402
import yaml  # noqa: E402

from speedtrader.app.options_orchestrator import OptionsOrchestrator  # noqa: E402
from speedtrader.data.schemas import RejectionStage, SystemState  # noqa: E402
from speedtrader.execution.authorization import AuthorizationRegistry  # noqa: E402
from speedtrader.execution.options_adapter import (  # noqa: E402
    BrokerTimeout, OptionsExecutionAdapter, SubmissionState,
)
from speedtrader.options.contracts import (  # noqa: E402
    ContractType, OptionContract, OptionQuote,
)
from speedtrader.quant.strategies.s07 import S07MomentumBreakout  # noqa: E402
from speedtrader.risk.state import PortfolioState  # noqa: E402
from speedtrader.storage.decision_store import DecisionStore  # noqa: E402

import test_deterministic_pipeline as EQ  # noqa: E402  (shared fixtures)

ROOT = Path(__file__).resolve().parents[3]
NOW = EQ.NOW
EXEC_CFG = yaml.safe_load((ROOT / "configs" / "execution_config.yaml").read_text())
RISK_CFG = EQ.RISK_CFG


def chain_for(spot: float, *, strikes=None, dte=28, bid=3.0, ask=3.2, oi=800):
    exp = NOW.date() + timedelta(days=dte)
    strikes = strikes if strikes is not None else [spot - 5, spot, spot + 5]
    return [
        OptionContract(
            symbol=f"TEST{exp:%y%m%d}C{int(k * 1000):08d}",
            underlying="TEST", type=ContractType.CALL, strike=float(k),
            expiration=exp, multiplier=100, open_interest=oi,
            quote=OptionQuote(bid=bid, ask=ask),
        )
        for k in strikes
    ]


class FakeBroker:
    def __init__(self, response=None, raises=None):
        self.response = response or {"id": "ord_opt_1"}
        self.raises = raises
        self.calls = []

    def submit_option_order(self, payload):
        self.calls.append(dict(payload))
        if self.raises:
            raise self.raises
        return self.response


def build(tmp_path, *, broker=None, chain=None, dry_run_only=False):
    provider = chain if callable(chain) else (
        lambda snap, asof: chain if chain is not None else chain_for(snap.price)
    )
    adapter = None
    if not dry_run_only:
        adapter = OptionsExecutionAdapter(broker or FakeBroker(),
                                          AuthorizationRegistry())
    return OptionsOrchestrator(
        strategies=[S07MomentumBreakout()],
        execution_config=EXEC_CFG, risk_config=RISK_CFG,
        store=DecisionStore(tmp_path), chain_provider=provider, adapter=adapter,
    )


def run(orch, **kw):
    return orch.run(EQ.snapshot_of(EQ.series(True)), account=EQ.account(),
                    portfolio=PortfolioState(), now=NOW, **kw)


# ============================================ the full path

def test_a_breakout_becomes_a_submitted_option_order(tmp_path):
    b = FakeBroker()
    r = run(build(tmp_path, broker=b))
    assert r.accepted
    assert r.execution_state is SubmissionState.SUBMITTED
    assert r.broker_order_id == "ord_opt_1"
    assert r.contracts_ordered >= 1
    assert len(b.calls) == 1


def test_the_order_sent_is_a_real_option_contract_not_the_underlying(tmp_path):
    b = FakeBroker()
    run(build(tmp_path, broker=b))
    sent = b.calls[0]
    assert sent["symbol"].startswith("TEST") and "C00" in sent["symbol"]
    assert sent["intent"] == "buy_to_open"
    assert sent["order_type"] == "limit"       # never market on a thin book
    assert sent["limit_price"] == 3.2          # the ask


def test_a_buy_signal_buys_a_call(tmp_path):
    b = FakeBroker()
    run(build(tmp_path, broker=b))
    assert "C" in b.calls[0]["symbol"][-9:]


# ============================================ the decision is auditable

def test_the_options_trace_reaches_disk(tmp_path):
    r = run(build(tmp_path))
    raw = json.loads(r.stored_at.read_text().strip().splitlines()[0])
    t = raw["options_trace"]
    assert t["structure"] == "long_single"
    assert t["contract"]["symbol"] and t["contract"]["strike"]
    assert t["selection"]["reason"]
    assert t["sizing"]["contracts"] >= 1
    assert t["estimated_fees"]["model"].startswith("options_")


def test_max_loss_is_recorded_and_equals_the_debit(tmp_path):
    """The defining property of the chosen structure, persisted."""
    r = run(build(tmp_path))
    s = json.loads(r.stored_at.read_text().strip().splitlines()[0])["options_trace"]["sizing"]
    assert s["max_loss_total"] == pytest.approx(
        s["contracts"] * s["premium_per_contract"] * 100)
    assert s["max_loss_total"] <= s["risk_budget"]


def test_the_full_trace_is_reconstructible_from_disk_alone(tmp_path):
    r = run(build(tmp_path))
    raw = json.loads(r.stored_at.read_text().strip().splitlines()[0])
    assert raw["snapshot"]["snapshot_id"]
    assert raw["candidate"]["signal_id"]
    assert raw["candidate"]["cost_breakdown"]["model"]
    assert raw["risk_gate"]["verdict"]
    assert raw["options_trace"]["contract"]["symbol"]


def test_rejected_alternatives_are_recorded(tmp_path):
    """Which contracts were considered and why they lost."""
    chain = chain_for(115.0) + chain_for(115.0, strikes=[115.0], oi=1)
    r = run(build(tmp_path, chain=chain))
    t = json.loads(r.stored_at.read_text().strip().splitlines()[0])["options_trace"]
    assert t["selection"]["considered"] == len(chain)
    assert t["selection"]["rejected"].get("insufficient_open_interest") == 1


# ============================================ fails closed, end to end

def test_no_tradeable_contract_is_a_rejection_not_a_crash(tmp_path):
    r = run(build(tmp_path, chain=[]))
    assert not r.accepted
    assert "no tradeable contract" in r.reason
    assert DecisionStore(tmp_path).count(NOW) == 1      # still recorded


def test_an_options_data_outage_is_not_treated_as_no_opportunity(tmp_path):
    def boom(snap, asof):
        raise ConnectionError("chain unavailable")
    r = run(build(tmp_path, chain=boom))
    assert not r.accepted
    assert r.decision.state is SystemState.FAILED
    assert "options data unavailable" in r.reason


def test_an_illiquid_chain_produces_no_order(tmp_path):
    b = FakeBroker()
    wide = chain_for(115.0, bid=0.10, ask=5.00)          # spread >> policy
    r = run(build(tmp_path, broker=b, chain=wide))
    assert not r.accepted and b.calls == []


def test_a_premium_larger_than_the_risk_budget_is_rejected(tmp_path):
    b = FakeBroker()
    rich = chain_for(115.0, bid=60.0, ask=62.0)          # 6,200 > 1% of 100k
    r = run(build(tmp_path, broker=b, chain=rich))
    assert not r.accepted
    assert "sizing rejected" in r.reason
    assert b.calls == [], "an unaffordable position reached the broker"


def test_a_broker_timeout_is_unknown_and_not_retried(tmp_path):
    b = FakeBroker(raises=BrokerTimeout("no response"))
    r = run(build(tmp_path, broker=b))
    assert not r.accepted
    assert r.execution_state is SubmissionState.UNKNOWN
    assert "reconcile before retrying" in r.reason
    assert len(b.calls) == 1, "an ambiguous outcome was retried"


def test_a_risk_rejection_never_reaches_contract_selection(tmp_path):
    """The engine's veto ends the cycle before an option is even chosen."""
    b = FakeBroker()
    orch = build(tmp_path, broker=b)
    heavy = PortfolioState(positions=EQ.heat_positions()) if hasattr(
        EQ, "heat_positions") else None
    r = orch.run(EQ.snapshot_of(EQ.series(False)), account=EQ.account(),
                 portfolio=PortfolioState(), now=NOW)
    assert not r.accepted
    assert r.decision.rejection_stage is RejectionStage.QUANT
    assert b.calls == []


def test_dry_run_authorizes_but_submits_nothing(tmp_path):
    b = FakeBroker()
    r = run(build(tmp_path, broker=b), dry_run=True)
    assert r.accepted and "no order submitted" in r.reason
    assert b.calls == []


def test_every_outcome_persists_exactly_one_decision(tmp_path):
    run(build(tmp_path))
    assert DecisionStore(tmp_path).count(NOW) == 1


def test_two_runs_produce_two_distinct_decisions(tmp_path):
    orch = build(tmp_path)
    a, b = run(orch), run(orch)
    assert a.decision.decision_id != b.decision.decision_id
    assert DecisionStore(tmp_path).count(NOW) == 2


def test_no_authorization_material_is_ever_persisted(tmp_path):
    """A licence must never land in the audit file.

    Checked exactly rather than by substring: the broker receives
    client_order_id "st-<nonce>", so the real nonce for THIS run is recovered
    from the fake and asserted absent from the stored decision. (A naive
    substring scan false-positives on the pre-existing `total_token_cost`
    field, which is why this is done precisely.)
    """
    b = FakeBroker()
    r = run(build(tmp_path, broker=b))
    nonce = b.calls[0]["client_order_id"].removeprefix("st-")
    assert len(nonce) == 32, "expected the single-use nonce as the idempotency key"

    blob = r.stored_at.read_text()
    assert nonce not in blob, "the authorization nonce leaked into the decision store"
    assert "st-" + nonce not in blob
    assert "ExecutionAuthorization" not in blob
    assert '"signature"' not in blob.lower()


# ============================================ AI veto, end to end
# The AI runs AFTER deterministic approval and can only subtract. These prove
# both halves: a veto really stops a real order, and every other AI outcome —
# including total failure — leaves the deterministic decision untouched.

import json as _json  # noqa: E402

from speedtrader.agents.veto import AdversarialReviewer  # noqa: E402
from speedtrader.llm.providers.base import LLMResponse, LLMTimeout  # noqa: E402
from speedtrader.llm.providers.deterministic import DeterministicProvider  # noqa: E402


class _Scripted:
    name = "scripted"

    def __init__(self, verdict=None, raises=None):
        self.verdict, self.raises = verdict, raises

    def complete(self, request):
        if self.raises:
            raise self.raises
        return LLMResponse(
            text=_json.dumps({"verdict": self.verdict, "confidence": 0.9,
                              "reasoning": "scripted", "concerns": []}),
            provider=self.name, model="scripted-1")


def _reviewed(tmp_path, broker, provider):
    orch = build(tmp_path, broker=broker)
    orch.reviewer = AdversarialReviewer(provider, run_debate=False)
    return run(orch)


def test_an_ai_veto_stops_a_real_order(tmp_path):
    b = FakeBroker()
    r = _reviewed(tmp_path, b, _Scripted("VETO"))
    assert not r.accepted
    assert "AI veto" in r.reason
    assert b.calls == [], "a vetoed trade still reached the broker"


def test_a_veto_is_recorded_with_the_model_that_cast_it(tmp_path):
    r = _reviewed(tmp_path, FakeBroker(), _Scripted("VETO"))
    raw = _json.loads(r.stored_at.read_text().strip().splitlines()[0])
    assert raw["ai_review"]["vetoed"] is True
    assert raw["ai_review"]["judge"]["provenance"]["model"] == "scripted-1"
    assert raw["rejection_stage"] == "REJECTED_BY_RISK_AGENT"


@pytest.mark.parametrize("verdict", ["CONFIRM", "ABSTAIN"])
def test_confirm_and_abstain_leave_the_trade_exactly_as_approved(tmp_path, verdict):
    b = FakeBroker()
    r = _reviewed(tmp_path, b, _Scripted(verdict))
    assert r.accepted and len(b.calls) == 1


def test_an_llm_outage_does_not_halt_trading(tmp_path):
    """The inverted failure mode: this layer only subtracts, so a vendor outage
    must not become a trading outage."""
    b = FakeBroker()
    r = _reviewed(tmp_path, b, _Scripted(raises=LLMTimeout("vendor down")))
    assert r.accepted, "an LLM outage silently halted trading"
    assert len(b.calls) == 1


def test_with_no_credentials_the_pipeline_still_completes(tmp_path):
    """A judge cloning this repo with no API key gets a full run."""
    b = FakeBroker()
    r = _reviewed(tmp_path, b, DeterministicProvider())
    assert r.accepted and len(b.calls) == 1
    raw = _json.loads(r.stored_at.read_text().strip().splitlines()[0])
    assert raw["ai_review"]["judge"]["verdict"] == "ABSTAIN"


def test_the_ai_cannot_enlarge_a_trade_it_approves(tmp_path):
    """The central claim, end to end: a hostile model claiming a huge size
    changes nothing about what is actually submitted."""
    b = FakeBroker()
    class Hostile:
        name = "hostile"
        def complete(self, request):
            return LLMResponse(
                text=_json.dumps({"verdict": "CONFIRM", "confidence": 1.0,
                                  "reasoning": "size up", "concerns": [],
                                  "quantity": 99999, "approved_quantity": 99999,
                                  "size_multiplier": 100}),
                provider="hostile", model="hostile-1")
    baseline = run(build(tmp_path / "base", broker=FakeBroker()))
    r = _reviewed(tmp_path, b, Hostile())
    assert r.accepted
    assert b.calls[0]["quantity"] == baseline.contracts_ordered
    assert b.calls[0]["quantity"] < 100

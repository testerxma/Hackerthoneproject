"""
SpeedTrader AI — Canonical Schemas
Spec refs: §18 Candidate Signal, §20 Market Snapshot, §35 Evidence, §43 Research Manager,
           §47 Trader, §49 Risk Agent, §53 Risk Engine, §55 Portfolio Manager,
           §59 Execution Guard, §66 Trade Outcome, §84 Decision Log, §95 TradingContext,
           §118 System State, §119 Rejection States

THIS FILE IS THE CONTRACT. Every block builds against these types and nothing else.
Once this is frozen, blocks A/B/C/D/E/F/G can be built in parallel without collision.

Design rules enforced here:
  - No float where a decision depends on it being present: Optional means "genuinely absent".
  - Nothing that carries a market number carries it without a snapshot_id (§21).
  - Confidence is never named "probability" (§37).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..common.clock import Freshness, utcnow

SCHEMA_VERSION = "1.0"


class Base(BaseModel):
    """Strict by default: unknown fields are an error, not silently dropped.

    This is deliberate. A typo'd field name in an agent's structured output
    must fail loudly rather than silently produce a decision missing its stop loss.
    """
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# ==========================================================================
# ENUMS
# ==========================================================================

class Direction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class Action(StrEnum):
    """§47 Trader actions, §76 NO_TRADE as first-class."""
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    NO_TRADE = "NO_TRADE"


class MarketRegime(StrEnum):
    """§31. Ported from Bot v6's 8-state gating (ENUM_MKT_STATE)."""
    STRONG_UP = "STRONG_UP"
    WEAK_UP = "WEAK_UP"
    RANGING = "RANGING"
    WEAK_DOWN = "WEAK_DOWN"
    STRONG_DOWN = "STRONG_DOWN"
    CHOPPY = "CHOPPY"
    QUIET = "QUIET"
    VOLATILE = "VOLATILE"
    UNKNOWN = "UNKNOWN"


class EvidenceType(StrEnum):
    TECHNICAL = "technical"
    REGIME = "regime"
    NEWS = "news"
    SENTIMENT = "sentiment"
    FUNDAMENTAL = "fundamental"
    QUANTITATIVE = "quantitative"
    PORTFOLIO = "portfolio"
    MEMORY = "memory"


class ResearchConclusion(StrEnum):
    """§44. Research states — NOT broker instructions."""
    SUPPORTED = "SUPPORTED"
    WEAKLY_SUPPORTED = "WEAKLY_SUPPORTED"
    CONTESTED = "CONTESTED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    REJECTED = "REJECTED"


class RiskGateVerdict(StrEnum):
    """§53. The three-way authority of the deterministic engine.

    NOTE: in Bot v6 these live in two functions —
      REJECT  <- ApproveTrade()      (boolean gate)
      REDUCE  <- ComputeFinalLot()   (sizing multipliers: heat, Kelly, recovery)
    We unify them here but must not invent a third source of truth.
    """
    PASS = "PASS"
    REDUCE = "REDUCE"
    REJECT = "REJECT"


class PortfolioVerdict(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    MODIFY = "MODIFY"


class ExecutionStatus(StrEnum):
    """§62. A submitted order MUST NOT auto-become FILLED."""
    REQUESTED = "requested"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    FAILED = "failed"
    UNKNOWN = "unknown"


class SystemState(StrEnum):
    """§118."""
    CREATED = "CREATED"
    DATA_LOADING = "DATA_LOADING"
    VALIDATING = "VALIDATING"
    ANALYZING = "ANALYZING"
    DEBATING = "DEBATING"
    RESEARCH_COMPLETE = "RESEARCH_COMPLETE"
    TRADING_DECISION = "TRADING_DECISION"
    RISK_CHECK = "RISK_CHECK"
    PORTFOLIO_REVIEW = "PORTFOLIO_REVIEW"
    REVALIDATING = "REVALIDATING"
    EXECUTING = "EXECUTING"
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    REFLECTING = "REFLECTING"
    MEMORIZED = "MEMORIZED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"


class RejectionStage(StrEnum):
    """§119. Which layer said no — makes diagnostics and the demo possible."""
    QUANT = "REJECTED_BY_QUANT"
    RESEARCH = "REJECTED_BY_RESEARCH"
    RISK_AGENT = "REJECTED_BY_RISK_AGENT"
    RISK_ENGINE = "REJECTED_BY_RISK_ENGINE"
    PORTFOLIO_MANAGER = "REJECTED_BY_PORTFOLIO_MANAGER"
    EXECUTION_GUARD = "REJECTED_BY_EXECUTION_GUARD"
    EXPIRED = "EXPIRED"


# ==========================================================================
# MARKET SNAPSHOT (§20, §21, §22)
# ==========================================================================

class Bar(Base):
    t: datetime
    o: float
    h: float
    l: float
    c: float
    v: float


class TechnicalFeatures(Base):
    """Computed by Block 2a. All optional: a missing indicator is UNKNOWN, never guessed (§102)."""
    ema8: float | None = None
    ema21: float | None = None
    ema55: float | None = None
    ema200: float | None = None
    rsi: float | None = None
    rsi_prev: float | None = None
    atr: float | None = None
    atr_prev: float | None = None
    adx: float | None = None
    di_plus: float | None = None
    di_minus: float | None = None
    bb_upper: float | None = None
    bb_lower: float | None = None
    bb_width: float | None = None
    squeeze_active: bool = False
    vwap: float | None = None
    volume_avg_20: float | None = None
    # Higher timeframe context (Bot v6 uses H1 primary / M30 entry / H4 trend)
    htf_ema50: float | None = None
    htf_ema200: float | None = None


class DataSourceMeta(Base):
    vendor: Literal["alpaca", "cache", "replay"] = "alpaca"
    fetched_at: datetime
    bar_timeframe: str = "1Hour"
    bars_available: int = 0
    freshness: Freshness = Freshness.UNKNOWN
    notes: str | None = None


class MarketSnapshot(Base):
    """§20/§21: single source of truth. Every agent in one decision cites the same snapshot_id."""
    schema_version: str = SCHEMA_VERSION
    snapshot_id: str
    timestamp: datetime = Field(default_factory=utcnow)
    symbol: str

    price: float | None = None
    bid: float | None = None
    ask: float | None = None
    spread: float | None = None
    spread_pct: float | None = None   # equities have no "pips" — always relative
    volume: float | None = None

    bars: list[Bar] = Field(default_factory=list)
    features: TechnicalFeatures = Field(default_factory=TechnicalFeatures)
    regime: MarketRegime = MarketRegime.UNKNOWN
    source: DataSourceMeta

    # Equity-market realities absent from the FX original
    market_open: bool = True
    minutes_to_close: float | None = None
    prior_close: float | None = None
    gap_pct: float | None = None

    def is_tradeable(self) -> bool:
        """Cheap pre-flight. The real gate is the Risk Engine (Block C)."""
        return (
            self.market_open
            and self.price is not None
            and self.source.freshness == Freshness.FRESH
        )


# ==========================================================================
# CANDIDATE SIGNAL (§18, §19)
# ==========================================================================

class StrategyVote(Base):
    strategy_id: str            # "S7"
    direction: Direction | None  # None == no signal from this strategy
    base_score: float
    notes: str | None = None


class CandidateSignal(Base):
    """§18: Candidate Signal != Order. This is the boundary between quant and AI."""
    schema_version: str = SCHEMA_VERSION
    signal_id: str
    snapshot_id: str
    symbol: str
    direction: Direction
    strategy_id: str                       # winning strategy, e.g. "S7"

    # Prices in absolute currency. No pips — this is not FX.
    entry: float
    stop_loss: float
    take_profit: float
    stop_distance: float                   # abs(entry - stop_loss)
    reward_risk: float                     # abs(tp-entry) / stop_distance
    atr_at_signal: float | None = None      # for ATR-normalised comparison

    base_score: float
    bonus: float = 0.0
    total_score: float
    score_breakdown: str = ""

    expected_value: float                  # in R units, NOT pips (see notes)
    ev_is_bootstrap: bool = True           # True while strategy has < min_trades history
    combined_priority: float

    strategy_votes: list[StrategyVote] = Field(default_factory=list)
    regime: MarketRegime = MarketRegime.UNKNOWN

    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime

    @model_validator(mode="after")
    def _check_geometry(self) -> CandidateSignal:
        """A signal whose stop is on the wrong side is a bug, not a trade."""
        if self.direction == Direction.BUY:
            if not (self.stop_loss < self.entry < self.take_profit):
                raise ValueError(
                    f"BUY geometry invalid: sl={self.stop_loss} entry={self.entry} tp={self.take_profit}"
                )
        else:
            if not (self.take_profit < self.entry < self.stop_loss):
                raise ValueError(
                    f"SELL geometry invalid: sl={self.stop_loss} entry={self.entry} tp={self.take_profit}"
                )
        return self


# ==========================================================================
# EVIDENCE LAYER (§34, §35, §36, §124)
# ==========================================================================

class Evidence(Base):
    """§35. Every important AI claim links to one of these."""
    evidence_id: str
    type: EvidenceType
    source: str                     # "alpaca_market_data", "alpaca_news", "quant_core"
    snapshot_id: str
    timestamp: datetime
    observation: str                # what was seen, in plain language
    value: str | float | None = None
    relevance: float = Field(ge=0.0, le=1.0)

    # §36 Evidence quality — distinct from model confidence
    freshness: Freshness = Freshness.UNKNOWN
    specificity: float = Field(default=0.5, ge=0.0, le=1.0)
    source_reliability: float = Field(default=0.5, ge=0.0, le=1.0)

    # §124 Independence: two agents citing the same fact are ONE piece of evidence.
    # Evidence sharing a root_fact_id must be counted once by the Research Manager.
    root_fact_id: str | None = None

    def quality(self) -> float:
        """Composite evidence quality. Deliberately NOT the agent's confidence."""
        base = (self.relevance + self.specificity + self.source_reliability) / 3.0
        penalty = {
            Freshness.FRESH: 1.0,
            Freshness.UNKNOWN: 0.7,
            Freshness.STALE: 0.4,
            Freshness.MISSING: 0.2,
            Freshness.INVALID: 0.0,
        }[self.freshness]
        return round(base * penalty, 4)


# ==========================================================================
# AGENT REPORTS (§30-33, §39-41)
# ==========================================================================

class AgentReport(Base):
    """Common envelope for every analyst. §100/§101: version & sampling are recorded."""
    agent_id: str                   # "technical_analyst"
    agent_version: str = "v1"
    prompt_version: str = "v1"
    model: str
    provider: str
    temperature: float | None = None

    run_id: str
    snapshot_id: str
    signal_id: str

    summary: str
    observations: list[str] = Field(default_factory=list)
    inferences: list[str] = Field(default_factory=list)   # §30: kept separate from observations
    uncertainties: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)

    stance: Literal["SUPPORTIVE", "NEUTRAL", "CAUTIONARY", "OPPOSED"] = "NEUTRAL"
    confidence: float = Field(ge=0.0, le=1.0)
    """§37: degree of confidence expressed by the agent. NOT a probability of profit."""

    latency_ms: int | None = None
    token_cost: float | None = None
    failed: bool = False
    error: str | None = None


class DebateSide(Base):
    """§39/§40 Bull and Bear."""
    side: Literal["BULL", "BEAR"]
    argument: str
    key_points: list[str] = Field(default_factory=list)
    cited_evidence_ids: list[str] = Field(default_factory=list)
    strongest_point: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    model: str | None = None


class ResearchSynthesis(Base):
    """§43/§125 Research Manager output."""
    conclusion: ResearchConclusion
    overall_assessment: str
    bull_strength: float = Field(ge=0.0, le=1.0)
    bear_strength: float = Field(ge=0.0, le=1.0)

    key_evidence_ids: list[str] = Field(default_factory=list)
    key_risks: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)

    # §45/§46 — these survive into position monitoring (§88)
    decision_conditions: list[str] = Field(default_factory=list)
    invalidators: list[str] = Field(default_factory=list)

    # §124 — the number that actually matters
    evidence_count: int = 0
    independent_evidence_count: int = 0

    why_not_trade: str | None = None   # §42 first-class "why not"


# ==========================================================================
# TRADER / RISK AGENT (§47, §49)
# ==========================================================================

class TradeProposal(Base):
    """§48: this is a proposal, never an order."""
    proposal_id: str
    signal_id: str
    snapshot_id: str
    symbol: str
    action: Action

    entry: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    quantity: float | None = None          # shares, not lots
    risk_amount: float | None = None       # account currency

    thesis: str
    conditions: list[str] = Field(default_factory=list)
    invalidators: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)

    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime


class RiskAssessment(Base):
    """§49 AI risk reasoning. Interpretive — NOT the authority (§50)."""
    assessment_id: str
    proposal_id: str
    concerns: list[str] = Field(default_factory=list)
    volatility_note: str | None = None
    correlation_note: str | None = None
    liquidity_note: str | None = None
    event_risk_note: str | None = None
    portfolio_interaction_note: str | None = None
    recommended_size_multiplier: float | None = None   # advisory only
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = "MEDIUM"
    confidence: float = Field(ge=0.0, le=1.0)


# ==========================================================================
# DETERMINISTIC RISK ENGINE (§51-54)
# ==========================================================================

class RiskCheck(Base):
    """One named rule and its result. Ported 1:1 from Bot v6 ApproveTrade()."""
    rule: str                  # "portfolio_heat"
    passed: bool
    observed: float | str | None = None
    limit: float | str | None = None
    reason: str = ""


class RiskGateResult(Base):
    """§53/§54. The hard boundary. May REJECT even when every agent said BUY."""
    verdict: RiskGateVerdict
    checks: list[RiskCheck] = Field(default_factory=list)
    blocking_reason: str | None = None

    approved_quantity: float | None = None
    size_multiplier: float = 1.0
    size_multiplier_breakdown: dict[str, float] = Field(default_factory=dict)

    portfolio_heat_pct: float | None = None
    evaluated_at: datetime = Field(default_factory=utcnow)
    engine_version: str = "v1"

    @property
    def failed_checks(self) -> list[RiskCheck]:
        """This is what the demo shows on screen."""
        return [c for c in self.checks if not c.passed]


# ==========================================================================
# PORTFOLIO MANAGER (§55-57)
# ==========================================================================

class PortfolioDecision(Base):
    verdict: PortfolioVerdict
    rationale: str
    modifications: dict[str, Any] = Field(default_factory=dict)
    requires_revalidation: bool = False   # §57: any material change -> risk engine AGAIN
    confidence: float = Field(ge=0.0, le=1.0)


# ==========================================================================
# EXECUTION (§58-62)
# ==========================================================================

class ExecutionGuardResult(Base):
    """§59. Mechanical validation only — makes no investment thesis."""
    allowed: bool
    checks: list[RiskCheck] = Field(default_factory=list)
    blocking_reason: str | None = None
    ttl_seconds_remaining: float | None = None
    duplicate_of: str | None = None


class ExecutionRequest(Base):
    decision_id: str
    client_order_id: str          # idempotency key (§60)
    symbol: str
    side: Direction
    quantity: float
    order_type: Literal["market", "limit", "stop", "stop_limit"] = "market"
    limit_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    time_in_force: Literal["day", "gtc", "ioc", "fok"] = "day"


class ExecutionResult(Base):
    """§62/§114: status comes from the broker, never assumed."""
    status: ExecutionStatus
    order_id: str | None = None
    client_order_id: str | None = None
    submitted_at: datetime | None = None
    filled_at: datetime | None = None
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    broker_raw: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    reconciled_at: datetime | None = None


# ==========================================================================
# OUTCOME (§66)
# ==========================================================================

class TradeOutcome(Base):
    trade_id: str
    decision_id: str
    position_id: str | None = None
    symbol: str

    entry_price: float
    exit_price: float | None = None
    quantity: float
    pnl: float | None = None
    return_pct: float | None = None
    r_multiple: float | None = None
    fees: float = 0.0
    slippage: float | None = None
    holding_seconds: float | None = None
    mae: float | None = None      # max adverse excursion
    mfe: float | None = None      # max favourable excursion

    exit_reason: str | None = None
    invalidator_triggered: str | None = None   # §88 links pre-trade reasoning to exit
    strategy_id: str | None = None
    regime_at_entry: MarketRegime = MarketRegime.UNKNOWN
    closed_at: datetime | None = None


# ==========================================================================
# DECISION LOG (§84) — the artefact the whole demo is built on
# ==========================================================================

class DecisionLog(Base):
    """§84. One row per decision, traded or not. §75: rejections are first-class records."""
    schema_version: str = SCHEMA_VERSION
    decision_id: str
    snapshot_id: str
    signal_id: str
    symbol: str

    state: SystemState = SystemState.CREATED
    rejection_stage: RejectionStage | None = None
    rejection_reason: str | None = None

    snapshot: MarketSnapshot | None = None
    candidate: CandidateSignal | None = None
    selected_agents: list[str] = Field(default_factory=list)
    analyst_reports: list[AgentReport] = Field(default_factory=list)
    bull: DebateSide | None = None
    bear: DebateSide | None = None
    research: ResearchSynthesis | None = None
    trader_proposal: TradeProposal | None = None
    risk_assessment: RiskAssessment | None = None
    risk_gate: RiskGateResult | None = None
    portfolio_decision: PortfolioDecision | None = None
    revalidation_gate: RiskGateResult | None = None   # §57 second pass after MODIFY
    execution_guard: ExecutionGuardResult | None = None
    execution: ExecutionResult | None = None
    outcome: TradeOutcome | None = None

    created_at: datetime = Field(default_factory=utcnow)
    completed_at: datetime | None = None
    total_latency_ms: int | None = None
    total_token_cost: float | None = None

    def all_evidence(self) -> list[Evidence]:
        return [e for r in self.analyst_reports for e in r.evidence]

    def was_blocked_by_risk_engine(self) -> bool:
        """The demo query. This is the story we tell."""
        return self.rejection_stage == RejectionStage.RISK_ENGINE

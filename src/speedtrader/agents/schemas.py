"""
SpeedTrader AI — Agent Schemas (import path preserved)

Re-exports the agent-facing subset of the canonical contract in data/schemas.py.
The planned import path works; there is still exactly one definition of each model.

Two schema modules would mean two definitions of AgentReport, and the failure is
silent: an agent validates against one, the orchestrator against the other, and a
field that exists in one and not the other simply vanishes mid-pipeline.
"""

from ..data.schemas import (
    AgentReport, DebateSide, Evidence, EvidenceType,
    ResearchConclusion, ResearchSynthesis, RiskAssessment, TradeProposal,
)

__all__ = [
    "AgentReport", "DebateSide", "Evidence", "EvidenceType",
    "ResearchConclusion", "ResearchSynthesis", "RiskAssessment", "TradeProposal",
]

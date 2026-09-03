"""
SpeedTrader AI — Data Freshness (import path preserved)
Spec §22.

Re-exports from common.clock. One module owns time; three modules owning time is how a
system ends up with two different answers to "now" and a decision that is fresh by one
clock and stale by another.
"""

from ..common.clock import Freshness, classify_freshness, utcnow

__all__ = ["Freshness", "classify_freshness", "utcnow"]

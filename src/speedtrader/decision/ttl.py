"""
SpeedTrader AI — Decision TTL (import path preserved)
Spec §23, §65.

Re-exports from common.clock. See data/freshness.py for the rationale.
"""

from ..common.clock import expires_at, is_expired, seconds_remaining, utcnow

__all__ = ["expires_at", "is_expired", "seconds_remaining", "utcnow"]

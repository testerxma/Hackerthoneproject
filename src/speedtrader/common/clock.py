"""
SpeedTrader AI — Clock, Freshness, TTL
Spec: §22 Data Freshness, §23 Decision TTL, §65 Stale Decision Recheck

ONE module owns time. The original plan had three (common/clock, data/freshness,
decision/ttl); three clocks is how a system ends up with two different "now".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import StrEnum


def utcnow() -> datetime:
    """The only source of "now" in the system. Always timezone-aware UTC."""
    return datetime.now(timezone.utc)


class Freshness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    MISSING = "missing"
    INVALID = "invalid"
    UNKNOWN = "unknown"


def classify_freshness(
    observed_at: datetime | None,
    max_age_seconds: float,
    now: datetime | None = None,
) -> Freshness:
    """§22. A missing timestamp is MISSING; UNKNOWN is only for "vendor gave no metadata"."""
    if observed_at is None:
        return Freshness.MISSING
    if observed_at.tzinfo is None:
        return Freshness.INVALID
    age = ((now or utcnow()) - observed_at).total_seconds()
    if age < 0:
        return Freshness.INVALID  # clock skew or bad data — never silently accept
    return Freshness.FRESH if age <= max_age_seconds else Freshness.STALE


def expires_at(created_at: datetime, ttl_seconds: float) -> datetime:
    return created_at + timedelta(seconds=ttl_seconds)


def is_expired(expiry: datetime, now: datetime | None = None) -> bool:
    """§23: never blindly execute an expired decision."""
    return (now or utcnow()) >= expiry


def seconds_remaining(expiry: datetime, now: datetime | None = None) -> float:
    return (expiry - (now or utcnow())).total_seconds()

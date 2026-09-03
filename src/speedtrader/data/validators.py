"""
SpeedTrader AI — Data Validators
Spec: §22 Data Freshness, §102 No Hallucinated Market Data, §111 Fail Closed

CANONICAL HOME FOR BAR VALIDATION.

`alpaca/market_data.validate_bars` predates this module and now delegates here.
There is one implementation. Two validators would drift, and the drift is silent:
a series rejected by one path and accepted by the other produces indicators on data
the system already decided was untrustworthy.

Every failure returns an explicit reason string. Nothing is repaired, interpolated,
or carried forward — a bar series is either fit to compute on or it is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from ..common.clock import Freshness, classify_freshness
from .schemas import Bar


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reason: str = "ok"
    code: str = "ok"
    detail: dict | None = None

    def __bool__(self) -> bool:
        return self.ok


# Reason codes. Stable strings — the decision log and the dashboard key off these.
class Code:
    OK = "ok"
    EMPTY = "empty_series"
    INSUFFICIENT = "insufficient_history"
    HIGH_BELOW_LOW = "high_below_low"
    OHLC_OUT_OF_RANGE = "ohlc_outside_high_low"
    NON_POSITIVE_PRICE = "non_positive_price"
    NEGATIVE_VOLUME = "negative_volume"
    NOT_CHRONOLOGICAL = "not_chronological"
    DUPLICATE_TIMESTAMP = "duplicate_timestamp"
    NAIVE_TIMESTAMP = "naive_timestamp"
    STALE = "stale_data"
    MISSING_TIMESTAMP = "missing_timestamp"
    NON_FINITE = "non_finite_value"


def _finite(x: float) -> bool:
    return x == x and x not in (float("inf"), float("-inf"))


def validate_series(
    bars: Sequence[Bar],
    *,
    min_required: int,
) -> ValidationResult:
    """Structural validation. Runs before any indicator touches the series.

    Catches the failure modes that otherwise produce confident nonsense:
      - too few bars for the longest indicator period
      - NaN/inf leaking in from a vendor payload
      - impossible OHLC geometry (high below low, close outside the range)
      - non-positive prices (a corporate-action artefact or a bad row)
      - out-of-order bars, which silently invert every momentum calculation
      - duplicate timestamps, which double-count a bar in every moving average
      - naive timestamps, which make freshness comparisons meaningless
    """
    if not bars:
        return ValidationResult(False, "empty bar series", Code.EMPTY)

    if len(bars) < min_required:
        return ValidationResult(
            False,
            f"insufficient history: {len(bars)} bars, need {min_required}",
            Code.INSUFFICIENT,
            {"have": len(bars), "need": min_required},
        )

    for i, b in enumerate(bars):
        for name, v in (("open", b.o), ("high", b.h), ("low", b.l),
                        ("close", b.c), ("volume", b.v)):
            if not _finite(v):
                return ValidationResult(
                    False, f"bar {i}: non-finite {name}={v}", Code.NON_FINITE, {"index": i}
                )

        if b.h < b.l:
            return ValidationResult(
                False, f"bar {i}: high {b.h} below low {b.l}",
                Code.HIGH_BELOW_LOW, {"index": i},
            )
        if not (b.l <= b.o <= b.h and b.l <= b.c <= b.h):
            return ValidationResult(
                False, f"bar {i}: open/close outside high-low range",
                Code.OHLC_OUT_OF_RANGE, {"index": i},
            )
        if b.o <= 0 or b.c <= 0 or b.h <= 0 or b.l <= 0:
            return ValidationResult(
                False, f"bar {i}: non-positive price",
                Code.NON_POSITIVE_PRICE, {"index": i},
            )
        if b.v < 0:
            return ValidationResult(
                False, f"bar {i}: negative volume {b.v}",
                Code.NEGATIVE_VOLUME, {"index": i},
            )
        if b.t.tzinfo is None:
            return ValidationResult(
                False, f"bar {i}: naive timestamp (no timezone)",
                Code.NAIVE_TIMESTAMP, {"index": i},
            )

    for i in range(1, len(bars)):
        if bars[i].t == bars[i - 1].t:
            return ValidationResult(
                False, f"duplicate timestamp at index {i}",
                Code.DUPLICATE_TIMESTAMP, {"index": i},
            )
        if bars[i].t < bars[i - 1].t:
            return ValidationResult(
                False, f"bars not chronological at index {i}",
                Code.NOT_CHRONOLOGICAL, {"index": i},
            )

    return ValidationResult(True)


def validate_freshness(
    bars: Sequence[Bar],
    *,
    max_age_seconds: float,
    now: datetime | None = None,
) -> ValidationResult:
    """§22. A stale series must not produce a tradeable snapshot."""
    if not bars:
        return ValidationResult(False, "empty bar series", Code.EMPTY)

    f = classify_freshness(bars[-1].t, max_age_seconds, now=now)
    if f is Freshness.FRESH:
        return ValidationResult(True, "fresh", Code.OK, {"freshness": f.value})
    return ValidationResult(
        False,
        f"latest bar is {f.value} (max age {max_age_seconds}s)",
        Code.STALE,
        {"freshness": f.value, "latest_bar": bars[-1].t.isoformat()},
    )


# --------------------------------------------------------------------------
# Backwards-compatible shim for alpaca/market_data.py
# --------------------------------------------------------------------------

def validate_bars(bars: Sequence[Bar], *, min_required: int) -> tuple[bool, str]:
    """Legacy tuple-returning signature. Kept so existing callers and their tests
    continue to work unchanged while there remains only one implementation."""
    r = validate_series(bars, min_required=min_required)
    return r.ok, r.reason

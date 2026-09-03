"""
SpeedTrader AI — Position Sizing (import path preserved)

The planned architecture places a position_sizing module under both quant/ and risk/.
This module exists at the planned path and the planned import works. It re-exports the
single implementation rather than duplicating it.

Reason: sizing is Bot v6's ComputeFinalLot(), which is a risk-authority function. Two
implementations of it would drift, and the one that drifts downward loses money quietly
because nothing raises an error when a position is merely smaller than intended.

    from speedtrader.quant.position_sizing import size_position   # works
    from speedtrader.risk.measures import size_position           # same object

If genuinely separate quant-side sizing is wanted later (e.g. a hypothetical size used
for EV estimation before risk ever sees the signal), add it here as a distinctly named
function — never as a second size_position.
"""

from ..risk.measures import SizingResult, kelly_multiplier, size_position

__all__ = ["size_position", "kelly_multiplier", "SizingResult"]

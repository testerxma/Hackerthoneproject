"""Options domain: deterministic contract selection, sizing and cost.

No network I/O lives here. Fetching is the adapter's job (alpaca/), so every
rule in this package is unit-testable offline and reproduces exactly in replay.
"""

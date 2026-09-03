"""
Integrity of the authoritative MQL5 reference.

docs/reference/SpeedTraderBot_v6.1.mq5 is READ-ONLY reference material and the
authoritative source for every ported Bot v6 formula. Several quant modules
record its SHA-256 in a SOURCE_HASH constant and cite line numbers against it.

Nothing previously verified that those constants still describe the file that is
actually on disk. Two failures were therefore silent:

  1. the reference is edited — every "ported from L1043" citation in the codebase
     silently starts pointing at different source lines, and the port can no
     longer be audited against anything;
  2. a module's SOURCE_HASH drifts from the file — the provenance claim becomes
     decorative.

§47 classes any change to the reference as a serious integrity issue, so it is
asserted here rather than trusted.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pytest  # noqa: E402

from speedtrader.quant import expected_value, scoring  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
REFERENCE = ROOT / "docs" / "reference" / "SpeedTraderBot_v6.1.mq5"

#: The hash this project was ported against. Changing this constant is a
#: deliberate act that must accompany a re-audit of every ported formula — it is
#: never the correct way to make this test pass.
EXPECTED_SHA256 = "c799acaa797a4f23a8c9531c3b4f14599b73736af2151d9eeb7f42209332e8d9"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_reference_source_is_present():
    assert REFERENCE.is_file(), (
        f"the authoritative MQL5 reference is missing at {REFERENCE}. "
        "Every ported formula cites it; the port is unauditable without it."
    )


def test_reference_source_is_unmodified():
    actual = _sha256(REFERENCE)
    assert actual == EXPECTED_SHA256, (
        "docs/reference/SpeedTraderBot_v6.1.mq5 has changed.\n"
        f"  expected {EXPECTED_SHA256}\n"
        f"  actual   {actual}\n"
        "This file is READ-ONLY reference material. Do not update the constant to "
        "silence this test: every ported formula and every L#### citation in the "
        "quant modules was written against the expected hash and must be re-audited."
    )


@pytest.mark.parametrize("module", [expected_value, scoring],
                         ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_module_source_hash_matches_the_reference_on_disk(module):
    """A provenance constant that disagrees with the file is worse than none."""
    assert module.SOURCE_HASH == EXPECTED_SHA256, (
        f"{module.__name__}.SOURCE_HASH claims provenance from a different "
        "revision of the MQL5 source than the one in docs/reference/."
    )


def test_reference_is_not_empty_or_truncated():
    """A zero-length or truncated file would still hash consistently if the
    constant were updated to match, so size is asserted independently."""
    size = REFERENCE.stat().st_size
    assert size > 50_000, f"reference looks truncated: {size} bytes"

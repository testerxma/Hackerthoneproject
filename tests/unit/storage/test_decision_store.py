"""DecisionStore tests. Audit-trail integrity is the property under test."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import pytest  # noqa: E402

from speedtrader.common.ids import IdKind, new_id  # noqa: E402
from speedtrader.data.schemas import (  # noqa: E402
    DecisionLog, RejectionStage, SystemState,
)
from speedtrader.storage.decision_store import (  # noqa: E402
    CorruptDecisionRecord, DecisionStore, DecisionStoreError, DuplicateDecision,
    StoreUnwritable,
)

T0 = datetime(2026, 9, 2, 14, 30, tzinfo=timezone.utc)


def log(**kw):
    d = dict(decision_id=new_id(IdKind.DECISION), snapshot_id="snap_x",
             signal_id="sig_x", symbol="TEST", created_at=T0)
    d.update(kw)
    return DecisionLog(**d)


# ============================================ write / read

def test_round_trip(tmp_path):
    s = DecisionStore(tmp_path)
    a = log()
    s.append(a)
    back = s.read(T0)
    assert len(back) == 1
    assert back[0].decision_id == a.decision_id
    assert isinstance(back[0], DecisionLog)


def test_one_file_per_utc_day(tmp_path):
    s = DecisionStore(tmp_path)
    s.append(log(created_at=T0))
    s.append(log(created_at=T0 + timedelta(days=1)))
    names = sorted(p.name for p in tmp_path.glob("*.jsonl"))
    assert names == ["decisions-2026-09-02.jsonl", "decisions-2026-09-03.jsonl"]


def test_day_boundary_uses_utc_not_local(tmp_path):
    s = DecisionStore(tmp_path)
    late = datetime(2026, 9, 2, 23, 59, tzinfo=timezone.utc)
    s.append(log(created_at=late))
    assert s.path_for(late).name == "decisions-2026-09-02.jsonl"


def test_append_only_existing_lines_never_mutated(tmp_path):
    s = DecisionStore(tmp_path)
    first = log()
    s.append(first)
    line1 = s.path_for(T0).read_text().splitlines()[0]
    for _ in range(5):
        s.append(log())
    assert s.path_for(T0).read_text().splitlines()[0] == line1
    assert s.count(T0) == 6


def test_rejected_decisions_are_persisted(tmp_path):
    """Section 75: a rejection is a first-class record, not a discard."""
    s = DecisionStore(tmp_path)
    s.append(log(state=SystemState.REJECTED,
                 rejection_stage=RejectionStage.RISK_ENGINE,
                 rejection_reason="portfolio heat 8.4%"))
    r = s.read(T0)[0]
    assert r.rejection_stage is RejectionStage.RISK_ENGINE
    assert r.rejection_reason == "portfolio heat 8.4%"
    assert r.was_blocked_by_risk_engine()


def test_no_signal_distinguishable_from_risk_rejection(tmp_path):
    s = DecisionStore(tmp_path)
    s.append(log(state=SystemState.REJECTED, rejection_stage=RejectionStage.QUANT,
                 rejection_reason="no breakout"))
    s.append(log(state=SystemState.REJECTED, rejection_stage=RejectionStage.RISK_ENGINE,
                 rejection_reason="score below min"))
    stages = [d.rejection_stage for d in s.read(T0)]
    assert stages == [RejectionStage.QUANT, RejectionStage.RISK_ENGINE]


# ============================================ corruption never skipped

def test_invalid_json_line_raises(tmp_path):
    s = DecisionStore(tmp_path)
    s.append(log())
    with open(s.path_for(T0), "a") as f:
        f.write("{not json\n")
    with pytest.raises(CorruptDecisionRecord, match="not valid JSON"):
        s.read(T0)


def test_schema_violating_line_raises(tmp_path):
    s = DecisionStore(tmp_path)
    s.append(log())
    with open(s.path_for(T0), "a") as f:
        f.write(json.dumps({"decision_id": "x"}) + "\n")   # missing required fields
    with pytest.raises(CorruptDecisionRecord, match="schema validation"):
        s.read(T0)


def test_blank_line_raises(tmp_path):
    """A blank line means a partial write. The file is untrustworthy."""
    s = DecisionStore(tmp_path)
    s.append(log())
    with open(s.path_for(T0), "a") as f:
        f.write("\n")
    with pytest.raises(CorruptDecisionRecord, match="blank"):
        s.read(T0)


def test_corruption_is_not_silently_skipped(tmp_path):
    """The valid records after a corrupt one must not be returned as if complete."""
    s = DecisionStore(tmp_path)
    s.append(log())
    with open(s.path_for(T0), "a") as f:
        f.write("garbage\n")
    s2 = DecisionStore(tmp_path)
    with pytest.raises(CorruptDecisionRecord):
        s2.read(T0)


def test_corrupt_file_also_blocks_further_appends(tmp_path):
    """A store whose file cannot be read cannot be safely appended to either."""
    s = DecisionStore(tmp_path)
    s.append(log())
    with open(s.path_for(T0), "a") as f:
        f.write("garbage\n")
    with pytest.raises(CorruptDecisionRecord):
        s.append(log())


# ============================================ duplicate / replay protection

def test_duplicate_decision_id_rejected(tmp_path):
    s = DecisionStore(tmp_path)
    d = log()
    s.append(d)
    with pytest.raises(DuplicateDecision, match=d.decision_id):
        s.append(d)


def test_stored_decision_cannot_be_read_back_and_reappended(tmp_path):
    """Accidental replay would double-count a decision downstream."""
    s = DecisionStore(tmp_path)
    s.append(log())
    replayed = s.read(T0)[0]
    with pytest.raises(DuplicateDecision):
        s.append(replayed)


# ============================================ fail closed

def test_unwritable_root_fails_at_construction(tmp_path):
    """uid-independent: a path whose parent is a FILE can never be a directory.

    The obvious version of this test (chmod 0o500) silently passes under root,
    which bypasses permission bits — it would assert nothing in CI containers.
    """
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    with pytest.raises(StoreUnwritable, match="cannot create"):
        DecisionStore(blocker / "decisions")


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission bits")
def test_unwritable_root_by_permission(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    os.chmod(ro, 0o500)
    try:
        with pytest.raises(StoreUnwritable):
            DecisionStore(ro / "decisions")
    finally:
        os.chmod(ro, 0o700)


def test_append_fails_closed_if_root_disappears(tmp_path):
    """The pipeline must not report success for a decision it could not record."""
    s = DecisionStore(tmp_path / "d")
    s.append(log())
    import shutil
    shutil.rmtree(tmp_path / "d")
    (tmp_path / "d").write_text("now a file")
    with pytest.raises(StoreUnwritable):
        s.append(log())


def test_non_decisionlog_rejected(tmp_path):
    s = DecisionStore(tmp_path)
    with pytest.raises(DecisionStoreError, match="expected DecisionLog"):
        s.append({"decision_id": "x"})


def test_all_errors_share_one_base(tmp_path):
    for exc in (StoreUnwritable, CorruptDecisionRecord, DuplicateDecision):
        assert issubclass(exc, DecisionStoreError)


# ============================================ reconstructible from disk

def test_trace_reconstructible_from_disk_alone(tmp_path):
    s = DecisionStore(tmp_path)
    d = log(state=SystemState.REJECTED, rejection_stage=RejectionStage.RISK_ENGINE,
            rejection_reason="portfolio heat 8.4%")
    s.append(d)
    fresh = DecisionStore(tmp_path).read(T0)[0]
    assert (fresh.decision_id, fresh.snapshot_id, fresh.signal_id,
            fresh.rejection_reason) == (d.decision_id, d.snapshot_id,
                                        d.signal_id, d.rejection_reason)


def test_store_has_no_execution_authority(tmp_path):
    import ast
    src = (Path(__file__).resolve().parents[3] / "src" / "speedtrader" /
           "storage" / "decision_store.py").read_text()
    names = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
    for n in names:
        assert not ({"execution", "alpaca", "llm", "risk"} & set(n.split("."))), n

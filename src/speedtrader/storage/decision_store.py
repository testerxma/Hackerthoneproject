"""
SpeedTrader AI — Decision Store
Spec: §83 Decision Trace, §84 Decision Log, §75 No-Trade Memory, §85 Explainability

Append-only JSONL. One file per UTC day under the configured root.

WHY JSONL AND NOT A DATABASE
Every decision is written once and never updated. There is no query load, no
relational structure, and no concurrent writer beyond a single pipeline process.
A database would add a schema migration surface for zero benefit. JSONL is also
directly readable during a demo, which matters: `grep REJECTED_BY_RISK_ENGINE`
on a plain file is the audit trail, visible without tooling.

THE STORE NEVER SKIPS A BAD RECORD.
A corrupt line raises. Skipping it would leave a hole in an audit trail while
reporting success, which is the one failure mode an audit trail must not have.

WRITES ARE VALIDATED, READS ARE VALIDATED.
A DecisionLog is serialised through Pydantic on write and re-validated on read, so
a file that round-trips is known to satisfy the schema in force.

DUPLICATE DECISION IDS ARE REJECTED.
The store refuses a decision_id it has already written. This is what stops a
stored decision being read back and re-appended — an accidental replay would
otherwise double-count a decision in any downstream evaluation.

THE STORE HAS NO EXECUTION AUTHORITY AND NO WAY TO ACQUIRE ONE.
It imports nothing from execution, alpaca, risk or llm. It writes files.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator

from ..data.schemas import DecisionLog


class DecisionStoreError(RuntimeError):
    """Base for store failures. All fail closed."""


class StoreUnwritable(DecisionStoreError):
    """The store root cannot be written to. The pipeline must not proceed."""


class CorruptDecisionRecord(DecisionStoreError):
    """A persisted record is unparseable or fails schema validation."""


class DuplicateDecision(DecisionStoreError):
    """A decision_id already present in the store was submitted again."""


class DecisionStore:
    """Append-only JSONL store for DecisionLog records."""

    def __init__(self, root: str | Path, *, verify_writable: bool = True):
        self.root = Path(root)
        if verify_writable:
            self._verify_writable()

    # ------------------------------------------------------------------ #
    def _verify_writable(self) -> None:
        """Fail at construction, not at the first decision.

        A pipeline that runs a full cycle and only then discovers it cannot record
        the outcome has already done the work it cannot account for.
        """
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise StoreUnwritable(f"cannot create decision store at {self.root}: {e}") from e
        if not os.access(self.root, os.W_OK):
            raise StoreUnwritable(f"decision store root is not writable: {self.root}")

    def path_for(self, when: datetime | date | None = None) -> Path:
        """One file per UTC day."""
        if when is None:
            when = datetime.now(timezone.utc)
        if isinstance(when, datetime):
            when = when.astimezone(timezone.utc).date()
        return self.root / f"decisions-{when.isoformat()}.jsonl"

    # ------------------------------------------------------------------ #
    def append(self, decision: DecisionLog) -> Path:
        """Validate, check for duplicates, then append one line. Returns the file."""
        if not isinstance(decision, DecisionLog):
            raise DecisionStoreError(
                f"expected DecisionLog, got {type(decision).__name__}"
            )

        # Re-validate rather than trusting the caller's object: a model built by
        # bypassing validation must not reach the audit trail.
        try:
            payload = DecisionLog.model_validate(
                decision.model_dump(mode="json")
            ).model_dump_json()
        except Exception as e:
            raise CorruptDecisionRecord(
                f"decision {decision.decision_id} failed validation before write: {e}"
            ) from e

        path = self.path_for(decision.created_at)
        if decision.decision_id in self._ids_in(path):
            raise DuplicateDecision(
                f"{decision.decision_id} is already stored in {path.name}. "
                "The store is append-only; a decision is written exactly once."
            )

        if "\n" in payload or "\r" in payload:
            raise CorruptDecisionRecord("serialised decision contains a newline")

        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(payload + "\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            raise StoreUnwritable(f"failed to append to {path}: {e}") from e
        return path

    # ------------------------------------------------------------------ #
    def read(self, when: datetime | date | None = None) -> list[DecisionLog]:
        """Read one day. Raises on the first corrupt record — never skips."""
        return list(self.iter_read(when))

    def iter_read(self, when: datetime | date | None = None) -> Iterator[DecisionLog]:
        path = self.path_for(when)
        if not path.exists():
            return
        with open(path, "r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    # A blank line means a partial write or manual edit. Both make
                    # the file untrustworthy; neither is silently tolerable.
                    raise CorruptDecisionRecord(f"{path.name}:{lineno} is blank")
                try:
                    yield DecisionLog.model_validate(json.loads(line))
                except json.JSONDecodeError as e:
                    raise CorruptDecisionRecord(
                        f"{path.name}:{lineno} is not valid JSON: {e}"
                    ) from e
                except Exception as e:
                    raise CorruptDecisionRecord(
                        f"{path.name}:{lineno} failed schema validation: {e}"
                    ) from e

    def _ids_in(self, path: Path) -> set[str]:
        """Decision ids already stored in one file. Corrupt lines raise here too."""
        if not path.exists():
            return set()
        ids: set[str] = set()
        with open(path, "r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    raise CorruptDecisionRecord(f"{path.name}:{lineno} is blank")
                try:
                    ids.add(json.loads(line)["decision_id"])
                except (json.JSONDecodeError, KeyError, TypeError) as e:
                    raise CorruptDecisionRecord(
                        f"{path.name}:{lineno} is unreadable: {e}"
                    ) from e
        return ids

    def count(self, when: datetime | date | None = None) -> int:
        return len(self._ids_in(self.path_for(when)))

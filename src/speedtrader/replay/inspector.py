"""
SpeedTrader AI — decision inspector

Turns one persisted decision record into an ordered, stage-by-stage account of
what happened and, crucially, WHO HAD AUTHORITY at each step.

--------------------------------------------------------------------------------
WHY THIS IS A DOMAIN MODULE AND NOT DASHBOARD CODE
--------------------------------------------------------------------------------
The dashboard is an observer; it must not be the place where "what did the
system decide" is worked out, or the answer becomes whatever the template
happens to render. The derivation lives here, is unit-tested against malformed
and truncated records, and the dashboard only formats what it returns.

--------------------------------------------------------------------------------
STAGES ARE DERIVED FROM EVIDENCE, NEVER ASSUMED
--------------------------------------------------------------------------------
A stage is reported as reached only when the record actually contains its
output. A decision that stopped early therefore renders as a journey that
stopped at a specific, named place rather than as an absence.

Three authority classes are kept strictly separate, because conflating them is
exactly the misunderstanding this project exists to prevent:

    ADVISORY       an AI opinion. Can subtract (veto), can never authorize.
    DETERMINISTIC  the sole source of execution authority.
    BROKER         external truth, observed rather than decided.

--------------------------------------------------------------------------------
STAGES THAT DO NOT EXIST ARE NOT INVENTED
--------------------------------------------------------------------------------
This system has no news, sentiment, regime or fundamental analyst, and no
research manager. Rather than render empty placeholders that imply otherwise,
those stages are simply absent, and `NOT_BUILT` exists so the UI can say so
explicitly where a reader might reasonably expect them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping


class Authority(StrEnum):
    """Who is speaking. The distinction is the whole architecture."""
    ADVISORY = "advisory"            # AI: may subtract, never authorize
    DETERMINISTIC = "deterministic"  # code: the only thing that authorizes
    BROKER = "broker"                # external truth
    DATA = "data"                    # inputs


class StageState(StrEnum):
    PASSED = "passed"
    BLOCKED = "blocked"        # this stage stopped the decision
    NOT_REACHED = "not_reached"
    NOT_BUILT = "not_built"    # honestly absent, not silently skipped
    OBSERVED = "observed"      # broker truth; nothing was decided here


@dataclass(frozen=True)
class Stage:
    key: str
    label: str
    authority: Authority
    state: StageState
    summary: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    timestamp: str = ""
    reason_code: str = ""

    @property
    def stopped_here(self) -> bool:
        return self.state is StageState.BLOCKED


@dataclass(frozen=True)
class Inspection:
    decision_id: str
    symbol: str
    stages: list[Stage]
    fingerprint: str
    ai: dict[str, Any]
    accepted: bool
    #: Exact deterministic reason code, never a prose paraphrase.
    reason_code: str = ""
    reason: str = ""

    @property
    def blocked_at(self) -> Stage | None:
        for stage in self.stages:
            if stage.stopped_here:
                return stage
        return None

    @property
    def authority_that_stopped_it(self) -> str:
        stage = self.blocked_at
        return str(stage.authority) if stage else ""


def _mapping(value: Any) -> dict[str, Any]:
    """Coerce anything that is not a mapping to an empty one.

    A persisted record is only as well-formed as the process that wrote it, and
    a crash mid-write or a hand-edited journal can leave a string where a
    section should be. Every nested section goes through this so one malformed
    field degrades that section rather than taking the whole inspection down.
    """
    return dict(value) if isinstance(value, Mapping) else {}


def _get(record: Mapping[str, Any], *path: str, default: Any = None) -> Any:
    cur: Any = record
    for key in path:
        if not isinstance(cur, Mapping):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def _accepted(record: Mapping[str, Any]) -> bool:
    return str(record.get("state", "")).upper() in {
        "EXECUTING", "EXECUTED", "SUBMITTED"}


def _checks(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = _get(record, "risk_gate", "checks", default=[]) or []
    return [c for c in raw if isinstance(c, Mapping)]


def inspect(record: Mapping[str, Any]) -> Inspection:
    """Derive the full causal chain from one persisted decision.

    Tolerant of partial records by design: a crash mid-cycle leaves one, and an
    inspector that raises on incomplete data is useless precisely when it is
    most needed.
    """
    if not isinstance(record, Mapping):
        raise TypeError(f"expected a decision record, got {type(record).__name__}")

    snapshot = _mapping(_get(record, "snapshot"))
    candidate = _mapping(_get(record, "candidate"))
    gate = _mapping(_get(record, "risk_gate"))
    options = _mapping(_get(record, "options_trace"))
    review = _mapping(_get(record, "ai_review"))
    execution = _mapping(_get(record, "execution"))
    recon = _mapping(_get(record, "reconciliation"))

    accepted = _accepted(record)
    vetoed = bool(review.get("vetoed"))
    stages: list[Stage] = []
    stopped = False

    def add(key, label, authority, state, summary="", detail=None,
            timestamp="", reason_code=""):
        nonlocal stopped
        if stopped and state is not StageState.NOT_BUILT:
            state = StageState.NOT_REACHED
            summary = ""
        stages.append(Stage(key=key, label=label, authority=authority,
                            state=state, summary=summary, detail=detail or {},
                            timestamp=timestamp, reason_code=reason_code))
        if state is StageState.BLOCKED:
            stopped = True

    # --- 1. market data -------------------------------------------------
    if snapshot:
        freshness = _get(snapshot, "source", "freshness", default="unknown")
        add("market", "Market snapshot", Authority.DATA, StageState.PASSED,
            summary=f"{snapshot.get('symbol', '?')} @ {snapshot.get('price', '?')}"
                    f" · {'open' if snapshot.get('market_open') else 'closed'}"
                    f" · {freshness}",
            detail={"price": snapshot.get("price"), "spread": snapshot.get("spread"),
                    "regime": snapshot.get("regime"),
                    "market_open": snapshot.get("market_open"),
                    "freshness": freshness,
                    "bars": len(snapshot.get("bars") or []),
                    "vendor": _get(snapshot, "source", "vendor", default="")},
            timestamp=str(snapshot.get("timestamp") or ""))
    else:
        add("market", "Market snapshot", Authority.DATA, StageState.BLOCKED,
            summary="no snapshot — a no-trade, never a trade on stale data",
            reason_code="NO_SNAPSHOT")

    # --- 2. quantitative signal -----------------------------------------
    if candidate:
        add("quant", "Quantitative signal", Authority.DETERMINISTIC,
            StageState.PASSED,
            summary=f"{candidate.get('strategy_id', '?')} "
                    f"{candidate.get('direction', '?')} "
                    f"entry {candidate.get('entry', '?')}",
            detail={k: candidate.get(k) for k in
                    ("strategy_id", "direction", "entry", "stop_loss",
                     "take_profit", "reward_risk", "total_score",
                     "expected_value")})
    else:
        add("quant", "Quantitative signal", Authority.DETERMINISTIC,
            StageState.BLOCKED,
            summary=str(record.get("rejection_reason") or "no setup found"),
            reason_code=str(record.get("rejection_stage") or "NO_SIGNAL").upper())

    # --- 3. adversarial research (Bull / Bear) --------------------------
    bull, bear = review.get("bull"), review.get("bear")
    if bull or bear:
        add("debate", "Adversarial research (Bull vs Bear)", Authority.ADVISORY,
            StageState.PASSED,
            summary=_debate_summary(bull, bear),
            detail={"bull": bull, "bear": bear})
    elif review:
        add("debate", "Adversarial research (Bull vs Bear)", Authority.ADVISORY,
            StageState.NOT_BUILT,
            summary="debate not run for this decision; the judge was consulted "
                    "directly")

    # --- 4. AI review — the one place the AI can change the outcome -----
    judge = _mapping(review.get("judge"))
    if review:
        add("ai_review", "AI review (veto layer)", Authority.ADVISORY,
            StageState.BLOCKED if vetoed else StageState.PASSED,
            summary=f"{judge.get('verdict', '?')}"
                    + (" — trade cancelled" if vetoed else " — no mechanical effect"),
            detail={"verdict": judge.get("verdict"),
                    "confidence": judge.get("confidence"),
                    "reasoning": judge.get("reasoning"),
                    "concerns": judge.get("concerns") or [],
                    "model": _get(judge, "provenance", "model", default=""),
                    "degraded": review.get("degraded"),
                    "prompt_version": review.get("prompt_version")},
            reason_code="AI_VETO" if vetoed else "")
    else:
        add("ai_review", "AI review (veto layer)", Authority.ADVISORY,
            StageState.NOT_BUILT,
            summary="no model consulted — identical outcome to an abstention, "
                    "because this layer can only subtract")

    # --- 5. options selection and sizing --------------------------------
    contract = _mapping(options.get("contract"))
    sizing = _mapping(options.get("sizing"))
    if contract:
        add("options", "Options selection & sizing", Authority.DETERMINISTIC,
            StageState.PASSED,
            summary=f"{contract.get('symbol', '?')} × {sizing.get('contracts', '?')}"
                    f" · max loss ${sizing.get('max_loss_total', '?')}",
            detail={"contract": contract, "sizing": sizing,
                    "structure": options.get("structure"),
                    "selection": options.get("selection"),
                    "fees": options.get("estimated_fees")})
    elif candidate and not vetoed:
        add("options", "Options selection & sizing", Authority.DETERMINISTIC,
            StageState.BLOCKED,
            summary=str(record.get("rejection_reason") or
                        "no contract satisfied the constraints"),
            reason_code="NO_ELIGIBLE_CONTRACT")

    # --- 6. deterministic risk — the only source of authority -----------
    checks = _checks(record)
    if gate:
        failed = [c for c in checks if not c.get("passed")]
        verdict = str(gate.get("verdict", "")).upper()
        blocked = bool(failed) or verdict not in {"PASS", "SCALED"}
        add("risk", "Deterministic risk engine", Authority.DETERMINISTIC,
            StageState.BLOCKED if blocked else StageState.PASSED,
            summary=f"{verdict or '—'} · "
                    f"{len(checks) - len(failed)}/{len(checks)} checks passed",
            detail={"verdict": verdict, "checks": checks,
                    "blocking_reason": gate.get("blocking_reason"),
                    "approved_quantity": gate.get("approved_quantity"),
                    "portfolio_heat_pct": gate.get("portfolio_heat_pct"),
                    "engine_version": gate.get("engine_version")},
            timestamp=str(gate.get("evaluated_at") or ""),
            reason_code=(str(failed[0].get("rule", "")).upper() if failed
                         else ("" if not blocked else verdict)))
    elif candidate:
        add("risk", "Deterministic risk engine", Authority.DETERMINISTIC,
            StageState.NOT_REACHED)

    # --- 7. authorization ------------------------------------------------
    coid = execution.get("client_order_id") or ""
    if execution:
        add("authorization", "Execution authorization", Authority.DETERMINISTIC,
            StageState.PASSED,
            summary="single-use licence issued, bound to this exact proposal",
            detail={"client_order_id": coid})

    # --- 8. execution ----------------------------------------------------
    if execution:
        state = str(execution.get("state", "")).lower()
        add("execution", "Broker submission", Authority.BROKER,
            StageState.OBSERVED,
            summary=f"{state or 'unknown'} — SUBMITTED is never FILLED",
            detail={"state": state,
                    "broker_order_id": execution.get("broker_order_id"),
                    "client_order_id": coid,
                    "reason": execution.get("reason")})
    elif accepted:
        add("execution", "Broker submission", Authority.BROKER,
            StageState.NOT_REACHED, summary="authorized but not submitted (dry run)")

    # --- 9. reconciliation ------------------------------------------------
    if recon:
        add("reconciliation", "Broker reconciliation", Authority.BROKER,
            StageState.OBSERVED,
            summary=f"{recon.get('state', '?')} · "
                    f"filled {recon.get('filled_quantity', 0)}"
                    f"/{recon.get('requested_quantity', 0)}",
            detail=dict(recon))

    fingerprint = ""
    try:
        from .fingerprint import ai_influence, decision_fingerprint
        fingerprint = decision_fingerprint(record)
        ai = ai_influence(record)
    except Exception:
        ai = {"consulted": bool(review), "vetoed": vetoed}

    blocking = next((s for s in stages if s.stopped_here), None)
    return Inspection(
        decision_id=str(record.get("decision_id") or ""),
        symbol=str(record.get("symbol") or ""),
        stages=stages,
        fingerprint=fingerprint,
        ai=ai,
        accepted=accepted,
        reason_code=(blocking.reason_code if blocking else ""),
        reason=str(record.get("rejection_reason") or ""),
    )


def _debate_summary(bull: Any, bear: Any) -> str:
    """What Bull and Bear actually disagreed about, in one line."""
    parts = []
    for name, side in (("bull", bull), ("bear", bear)):
        if isinstance(side, Mapping):
            n = len(side.get("concerns") or [])
            parts.append(f"{name} {side.get('verdict', '?')}"
                         f" ({side.get('confidence', '?')})"
                         + (f", {n} concern(s)" if n else ""))
    return " vs ".join(parts) if parts else "no debate recorded"


def evidence_for(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every checkable claim behind this decision, with its provenance.

    This system has NO news, sentiment or fundamental evidence layer, so none is
    invented here. What it does have is stronger for audit purposes: each
    deterministic check carries the value it actually observed, and the cost
    model carries its own source. Those are the claims, and they are verifiable
    rather than asserted.
    """
    out: list[dict[str, Any]] = []

    for i, check in enumerate(_checks(record)):
        out.append({
            "evidence_id": f"chk-{i:02d}",
            "claim": str(check.get("rule", "")),
            "source": "deterministic risk engine",
            "source_type": "computed",
            "observed": check.get("observed"),
            "supports": bool(check.get("passed")),
            "verifiable": True,
        })

    snapshot = _mapping(_get(record, "snapshot"))
    if snapshot:
        out.append({
            "evidence_id": "snap-00",
            "claim": "market data was fresh enough to price against",
            "source": _get(snapshot, "source", "vendor", default="unknown"),
            "source_type": "market_data",
            "observed": _get(snapshot, "source", "freshness", default="unknown"),
            "supports": _get(snapshot, "source", "freshness",
                             default="") == "fresh",
            "verifiable": True,
        })

    fees = _mapping(_get(record, "options_trace", "estimated_fees"))
    if fees:
        out.append({
            "evidence_id": "cost-00",
            "claim": "transaction costs behind the expected value",
            "source": fees.get("model", "cost policy"),
            "source_type": "estimate",
            "observed": fees.get("total"),
            "supports": True,
            "verifiable": True,
        })
    return out


def option_detail(record: Mapping[str, Any]) -> dict[str, Any]:
    """Everything a reader needs to judge WHY this contract was chosen.

    Derived rather than stored: DTE, mid, spread and spread-percentage are
    computable from what was persisted, so recomputing them here keeps one
    source of truth instead of a second set of numbers that can drift.

    MAX PROFIT IS DELIBERATELY NOT A NUMBER. For a long call it is unbounded,
    and printing a large figure next to an exactly-known max loss is how a
    reader is led to a false expectation. It is returned as a statement, not a
    quantity.
    """
    options = _mapping(_get(record, "options_trace"))
    contract = _mapping(options.get("contract"))
    sizing = _mapping(options.get("sizing"))
    selection = _mapping(options.get("selection"))
    fees = _mapping(options.get("estimated_fees"))
    if not contract:
        return {}

    bid = _number(contract.get("bid"))
    ask = _number(contract.get("ask"))
    mid = (bid + ask) / 2.0 if bid is not None and ask is not None else None
    spread = (ask - bid) if bid is not None and ask is not None else None
    spread_pct = (spread / mid * 100.0) if spread is not None and mid else None

    dte = _days_to_expiry(contract.get("expiration"),
                          _get(record, "snapshot", "timestamp"))

    contracts = _number(sizing.get("contracts")) or 0
    max_loss = _number(sizing.get("max_loss_total"))
    fee_total = _number(fees.get("total"))
    # The honest headline: what this position can actually lose, all in.
    max_loss_all_in = (max_loss + fee_total
                       if max_loss is not None and fee_total is not None
                       else max_loss)

    return {
        "symbol": contract.get("symbol"),
        "underlying": _get(record, "snapshot", "symbol"),
        "type": contract.get("type"),
        "direction": _get(record, "candidate", "direction"),
        "strike": contract.get("strike"),
        "expiration": contract.get("expiration"),
        "dte": dte,
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "spread": spread,
        "spread_pct": spread_pct,
        "multiplier": contract.get("multiplier"),
        "open_interest": contract.get("open_interest"),
        "premium_per_contract": sizing.get("premium_per_contract"),
        "contracts": int(contracts),
        "max_loss_per_contract": sizing.get("max_loss_per_contract"),
        "max_loss_total": max_loss,
        "estimated_fees": fee_total,
        "max_loss_all_in": max_loss_all_in,
        "risk_budget": sizing.get("risk_budget"),
        "budget_used_pct": (max_loss / _number(sizing.get("risk_budget")) * 100.0
                            if max_loss is not None
                            and _number(sizing.get("risk_budget")) else None),
        "caps_applied": list(sizing.get("caps_applied") or []),
        "sizing_reason": sizing.get("reason"),
        "selection_reason": selection.get("reason"),
        "considered": selection.get("considered"),
        # Priced at the ASK, never the mid: the mid is not a price anyone will
        # sell to you at, and sizing against it understates what the position
        # actually costs — which would make the max-loss figure untrue.
        "priced_at": "ask",
        "max_profit": ("unbounded for a long call — deliberately not quantified, "
                       "because a large number beside an exact max loss invites "
                       "a false expectation")
        if str(contract.get("type", "")).lower() == "call"
        else ("bounded by the strike falling to zero"
              if str(contract.get("type", "")).lower() == "put" else "unknown"),
    }


def _number(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _days_to_expiry(expiration: Any, asof: Any) -> int | None:
    """Calendar days from the snapshot to expiry. None if either is unreadable."""
    from datetime import date, datetime

    def as_date(v: Any) -> date | None:
        if isinstance(v, date) and not isinstance(v, datetime):
            return v
        if isinstance(v, datetime):
            return v.date()
        if isinstance(v, str) and len(v) >= 10:
            try:
                return date.fromisoformat(v[:10])
            except ValueError:
                return None
        return None

    exp, now = as_date(expiration), as_date(asof)
    if exp is None or now is None:
        return None
    return (exp - now).days

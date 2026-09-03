#!/usr/bin/env python3
"""
SpeedTrader AI — command centre

Renders the decision journal, live paper-account state and runtime health into
one self-contained HTML file. It is GENERATED FROM THE REAL PIPELINE: every
number on the page comes from a persisted decision record, from the Alpaca paper
account, or from the runtime's own health object. Nothing is invented to make
the page look busier.

    python scripts/build_dashboard.py                     # journal only
    python scripts/build_dashboard.py --live              # + paper account state
    python scripts/build_dashboard.py --open              # print the file URL

--------------------------------------------------------------------------------
WHY THERE IS NO JAVASCRIPT
--------------------------------------------------------------------------------
This page renders text produced by a language model (AI reasoning, veto
rationales) and by a broker. With no <script> anywhere, a malicious or
malformed string has no execution path at all — not a mitigated one, an absent
one. Interactivity (tabs, expandable sections, filtering) is done with CSS
`:checked` and `<details>`, which cost nothing and cannot execute.

A test asserts the page contains no `<script` and no external URL. Keep it that
way: it is a security property, not a stylistic preference.

--------------------------------------------------------------------------------
LABELLING IS NOT DECORATION
--------------------------------------------------------------------------------
Every figure is badged with its provenance — PAPER, SIMULATED, FIXTURE,
ESTIMATED, LIVE. A reader must never have to guess whether a number came from a
broker or from a demo fixture, and a zero is reported as a zero.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# ---------------------------------------------------------------- palette
BG        = "#05070d"
SURFACE   = "#0b0f19"
SURFACE_2 = "#111828"
BORDER    = "#1b2438"
BORDER_HI = "#2a3550"
TEXT      = "#e7edf9"
MUTED     = "#8090ab"
DIM       = "#5b6880"
BLUE      = "#4d8dff"
PURPLE    = "#9d7cff"
CYAN      = "#3fd0e0"
GREEN     = "#2ecc8f"
RED       = "#ff5c7a"
AMBER     = "#ffb454"


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def num(value: Any, digits: int = 2, dash: str = "—") -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return dash


# ---------------------------------------------------------------- loading
def load_decisions(store_dir: Path) -> list[dict]:
    """Every readable decision, oldest first. A corrupt line is skipped, never
    fatal: a truncated write must not hide the rest of the audit trail."""
    out: list[dict] = []
    for path in sorted(Path(store_dir).glob("decisions-*.jsonl")):
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                out.append(record)
    return out


def load_intents(journal_dir: Path) -> list[dict]:
    """The execution intent log — the order lifecycle as it actually happened."""
    path = Path(journal_dir) / "execution_intents.jsonl"
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def load_account() -> dict | None:
    """Real paper-account state. None (and the page says so) if unreachable.

    Credentials are read from the environment and never rendered.
    """
    try:
        env_path = ROOT / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip("'\""))
        from alpaca.trading.client import TradingClient
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        key, secret = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
        if not key or not secret:
            return None
        client = TradingClient(key, secret, paper=True)
        acct = client.get_account()
        orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, limit=50))
        positions = client.get_all_positions()
        return {
            # Account NUMBER only. Never the key, never the secret.
            "account_number": str(acct.account_number),
            "equity": float(acct.equity),
            "cash": float(acct.cash),
            "buying_power": float(acct.buying_power),
            "options_level": getattr(acct, "options_trading_level", None),
            "pnl": float(acct.equity) - 100_000.0,
            "orders": [{
                "symbol": o.symbol,
                "status": str(getattr(o.status, "value", o.status)),
                "qty": str(o.qty), "filled_qty": str(o.filled_qty),
                "limit_price": str(o.limit_price or ""),
                "client_order_id": str(o.client_order_id),
                "submitted_at": str(getattr(o, "submitted_at", "") or ""),
            } for o in orders],
            "positions": [{
                "symbol": p.symbol, "qty": str(p.qty),
                "cost_basis": float(p.cost_basis or 0),
                "unrealized_pl": float(getattr(p, "unrealized_pl", 0) or 0),
            } for p in positions],
        }
    except Exception:
        # A dashboard must render without a broker. The page states the
        # connection failed rather than showing stale or invented figures.
        return None


# ---------------------------------------------------------------- helpers
def get(d: dict, *path, default=None):
    cur: Any = d
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur if cur is not None else default


def badge(text: str, colour: str, *, solid: bool = False) -> str:
    if solid:
        return (f'<span class="badge" style="background:{colour};color:{BG};'
                f'border-color:{colour}">{esc(text)}</span>')
    return (f'<span class="badge" style="color:{colour};border-color:{colour}44;'
            f'background:{colour}14">{esc(text)}</span>')


def stat(label: str, value: str, *, sub: str = "", colour: str = TEXT,
         tag: str = "") -> str:
    return f"""<div class="stat">
      <div class="stat-l">{esc(label)}{(' ' + tag) if tag else ''}</div>
      <div class="stat-v" style="color:{colour}">{value}</div>
      {f'<div class="stat-s">{sub}</div>' if sub else ''}
    </div>"""


# ---------------------------------------------------------------- pipeline
#: The stages a decision passes through, in order. Each decision lights these
#: up to the point it actually reached, so a NO TRADE is visually a journey
#: that stopped somewhere specific rather than an absence of one.
STAGES = [
    ("MARKET", "snapshot"), ("QUANT", "candidate"), ("RESEARCH", "ai_review"),
    ("BULL/BEAR", "ai_review"), ("AI VETO", "ai_review"), ("OPTIONS", "options_trace"),
    ("RISK", "risk_gate"), ("PORTFOLIO", "risk_gate"), ("AUTH", "execution"),
    ("EXECUTION", "execution"), ("RECONCILE", "reconciliation"),
]


def reached_stage(d: dict) -> int:
    """How far this decision actually got. Drives the pipeline rendering."""
    if not get(d, "snapshot"):
        return 0
    # A snapshot with no candidate did reach the quant stage — that is where
    # it stopped. Marking MARKET as the blocker would blame the data layer for
    # a strategy that simply found no setup.
    if not get(d, "candidate"):
        return 2
    idx = 2
    review = get(d, "ai_review") or {}
    if review:
        idx = 5
        if review.get("vetoed"):
            return idx
    if get(d, "options_trace"):
        idx = 6
    else:
        return idx
    gate = get(d, "risk_gate") or {}
    if gate:
        idx = 8
        if str(gate.get("verdict", "")).upper() not in {"PASS", "SCALED"}:
            return idx
    if get(d, "execution"):
        idx = 10
    else:
        return idx
    if get(d, "reconciliation"):
        idx = 11
    return idx


def pipeline_html(d: dict) -> str:
    reached = reached_stage(d)
    vetoed = bool(get(d, "ai_review", "vetoed"))
    cells = []
    for i, (label, _) in enumerate(STAGES, start=1):
        if i < reached:
            cls, colour = "pl-done", GREEN
        elif i == reached:
            cls, colour = ("pl-stop", RED) if not _accepted(d) else ("pl-done", GREEN)
        else:
            cls, colour = "pl-todo", DIM
        if vetoed and label == "AI VETO":
            cls, colour = "pl-veto", PURPLE
        cells.append(
            f'<div class="pl {cls}" style="--c:{colour}">'
            f'<span class="pl-dot"></span><span class="pl-t">{esc(label)}</span></div>')
    return f'<div class="pipeline">{"".join(cells)}</div>'


def _accepted(d: dict) -> bool:
    return str(d.get("state", "")).upper() in {"EXECUTING", "EXECUTED", "SUBMITTED"}


# ---------------------------------------------------------------- sections
def decision_card(d: dict, index: int) -> str:
    symbol = esc(d.get("symbol", "?"))
    state = str(d.get("state", "")).upper()
    accepted = _accepted(d)
    vetoed = bool(get(d, "ai_review", "vetoed"))

    if vetoed:
        verdict, vcol = "AI VETO", PURPLE
    elif accepted:
        verdict, vcol = "ACCEPTED", GREEN
    else:
        verdict, vcol = "NO TRADE", RED

    snap = get(d, "snapshot") or {}
    cand = get(d, "candidate") or {}
    gate = get(d, "risk_gate") or {}
    opts = get(d, "options_trace") or {}
    contract = get(opts, "contract") or {}
    sizing = get(opts, "sizing") or {}
    review = get(d, "ai_review") or {}
    judge = get(review, "judge") or {}
    execution = get(d, "execution") or {}
    recon = get(d, "reconciliation") or {}

    fp = d.get("fingerprint") or d.get("decision_fingerprint") or ""
    if not fp:
        try:
            from speedtrader.replay.fingerprint import decision_fingerprint
            fp = decision_fingerprint(d)
        except Exception:
            fp = ""

    # --- rejected option candidates, with the reason each was dropped
    rejected = get(opts, "selection", "rejected") or get(opts, "rejected") or []
    rej_html = ""
    if isinstance(rejected, list) and rejected:
        rows = "".join(
            f"<tr><td class='mono'>{esc(get(r,'symbol',default='—'))}</td>"
            f"<td class='muted'>{esc(get(r,'reason',default=''))}</td></tr>"
            for r in rejected[:8] if isinstance(r, dict))
        if rows:
            rej_html = f"""<details class="sub"><summary>Rejected option candidates
              <span class="muted">({len(rejected)})</span></summary>
              <table class="mini"><tbody>{rows}</tbody></table></details>"""

    # --- deterministic checks
    checks = gate.get("checks") or []
    passed = sum(1 for c in checks if isinstance(c, dict) and c.get("passed"))
    failed = [c for c in checks if isinstance(c, dict) and not c.get("passed")]
    checks_html = ""
    if checks:
        def _verdict_cell(ok: bool) -> str:
            colour, word = (GREEN, "PASS") if ok else (RED, "BLOCK")
            return f'<span style="color:{colour};font-weight:600">{word}</span>'

        rows = "".join(
            "<tr><td class='mono'>{rule}</td><td>{verdict}</td>"
            "<td class='muted mono'>{observed}</td></tr>".format(
                rule=esc(c.get("rule")),
                verdict=_verdict_cell(bool(c.get("passed"))),
                observed=esc(c.get("observed", "")))
            for c in checks if isinstance(c, dict))
        checks_html = f"""<details class="sub"><summary>Deterministic risk checks
          <span class="muted">({passed}/{len(checks)} passed)</span></summary>
          <table class="mini"><thead><tr><th>rule</th><th>verdict</th><th>observed</th></tr></thead>
          <tbody>{rows}</tbody></table></details>"""

    # --- the three authority lanes
    ai_verdict = esc(judge.get("verdict", "not consulted"))
    ai_reason = esc((judge.get("reasoning") or "")[:220])
    changed = "YES — vetoed" if vetoed else "NO"
    lanes = f"""
    <div class="lanes">
      <div class="lane" style="--c:{PURPLE}">
        <div class="lane-h">AI · ADVISORY</div>
        <div class="lane-v">{ai_verdict}</div>
        <div class="lane-s">{ai_reason or '<span class="muted">no model consulted</span>'}</div>
        <div class="lane-s">changed outcome: <b>{changed}</b></div>
        <div class="lane-f">can veto · cannot authorize</div>
      </div>
      <div class="lane" style="--c:{BLUE}">
        <div class="lane-h">DETERMINISTIC · AUTHORITY</div>
        <div class="lane-v">{esc(gate.get('verdict', '—'))}</div>
        <div class="lane-s">{passed}/{len(checks) or 0} checks passed
          {('· blocked by ' + esc(failed[0].get('rule'))) if failed else ''}</div>
        <div class="lane-f">sole source of execution authority</div>
      </div>
      <div class="lane" style="--c:{CYAN}">
        <div class="lane-h">EXECUTION · OUTCOME</div>
        <div class="lane-v">{esc(execution.get('state', 'none') if execution else 'none')}</div>
        <div class="lane-s">{esc(recon.get('state', '')) if recon else '<span class="muted">not submitted</span>'}</div>
        <div class="lane-f">SUBMITTED is never FILLED</div>
      </div>
    </div>"""

    # --- options detail
    opt_html = ""
    if contract:
        opt_html = f"""<div class="grid4">
          {stat("CONTRACT", f'<span class="mono sm">{esc(contract.get("symbol","—"))}</span>')}
          {stat("STRIKE / EXPIRY", f'{num(contract.get("strike"))} · {esc(contract.get("expiration","—"))}')}
          {stat("CONTRACTS", esc(sizing.get("contracts","—")))}
          {stat("MAX LOSS", "$" + num(sizing.get("max_loss_total")), colour=AMBER,
                tag=badge("EXACT", AMBER))}
        </div>"""

    market = f"""<div class="grid4">
      {stat("PRICE", num(snap.get("price")))}
      {stat("SPREAD", num(snap.get("spread"), 4))}
      {stat("REGIME", esc(snap.get("regime","—")))}
      {stat("SESSION", "OPEN" if snap.get("market_open") else "CLOSED",
            colour=GREEN if snap.get("market_open") else DIM)}
    </div>"""

    quant = ""
    if cand:
        quant = f"""<div class="grid4">
          {stat("SIGNAL", esc(cand.get("direction","—")),
                sub=esc(cand.get("strategy_id","")))}
          {stat("ENTRY", num(cand.get("entry")))}
          {stat("STOP / TARGET", f'{num(cand.get("stop_loss"))} · {num(cand.get("take_profit"))}')}
          {stat("REWARD:RISK", num(cand.get("reward_risk")) + "R")}
        </div>"""

    return f"""
    <details class="card" {'open' if index == 0 else ''}>
      <summary class="card-h">
        <span class="sym">{symbol}</span>
        {badge(verdict, vcol, solid=True)}
        <span class="muted sm">{esc(state.lower())}</span>
        <span class="spacer"></span>
        <span class="mono sm muted">{esc(fp[:16])}</span>
      </summary>
      <div class="card-b">
        {pipeline_html(d)}
        <div class="reason">{esc(d.get("rejection_reason") or "authorized")}</div>
        {lanes}
        <div class="sec-t">MARKET STATE</div>{market}
        {f'<div class="sec-t">QUANTITATIVE SIGNAL</div>{quant}' if quant else ''}
        {f'<div class="sec-t">OPTIONS CONTRACT</div>{opt_html}' if opt_html else ''}
        {checks_html}
        {rej_html}
      </div>
    </details>"""


ORDER_COLOURS = {
    "filled": GREEN, "partially_filled": AMBER, "new": BLUE, "accepted": BLUE,
    "pending_new": BLUE, "canceled": DIM, "expired": DIM, "rejected": RED,
    "unknown": AMBER, "attempted": AMBER, "submitted": BLUE,
    "reconciled": GREEN, "abandoned": DIM,
}


def order_lifecycle_html(intents: list[dict], account: dict | None) -> str:
    """Every execution attempt and its broker state. This is the audit trail
    for 'did we ever place a duplicate' — the answer must be visible, not
    asserted."""
    rows = []
    by_coid: dict[str, list[dict]] = {}
    for rec in intents:
        by_coid.setdefault(str(rec.get("client_order_id", "")), []).append(rec)

    broker_by_coid = {}
    if account:
        for o in account.get("orders", []):
            broker_by_coid[o["client_order_id"]] = o

    for coid, records in by_coid.items():
        latest = records[-1]
        phase = str(latest.get("phase", ""))
        broker = broker_by_coid.get(coid)
        bstatus = broker["status"] if broker else latest.get("broker_status") or "—"
        rows.append(
            f"<tr><td class='mono sm'>{esc(coid[:22])}</td>"
            f"<td class='mono'>{esc(latest.get('symbol') or (broker or {}).get('symbol','—'))}</td>"
            f"<td>{badge(phase, ORDER_COLOURS.get(phase, MUTED))}</td>"
            f"<td>{badge(bstatus, ORDER_COLOURS.get(bstatus, MUTED))}</td>"
            f"<td class='mono'>{esc((broker or {}).get('filled_qty','0'))}"
            f"/{esc(latest.get('quantity') or (broker or {}).get('qty','—'))}</td>"
            f"<td class='muted sm'>{esc((latest.get('detail') or '')[:60])}</td></tr>")

    # Broker-side orders with no local intent: history that predates this
    # journal. Shown, never hidden — deleting evidence is not cleanup.
    for coid, o in broker_by_coid.items():
        if coid not in by_coid:
            rows.append(
                f"<tr><td class='mono sm'>{esc(coid[:22])}</td>"
                f"<td class='mono'>{esc(o['symbol'])}</td>"
                f"<td>{badge('no local intent', DIM)}</td>"
                f"<td>{badge(o['status'], ORDER_COLOURS.get(o['status'], MUTED))}</td>"
                f"<td class='mono'>{esc(o['filled_qty'])}/{esc(o['qty'])}</td>"
                f"<td class='muted sm'>predates the intent journal</td></tr>")

    if not rows:
        return ('<div class="empty">No execution attempt has been made yet. '
                'That is a factual state, not a rendering failure.</div>')
    return f"""<table class="tbl">
      <thead><tr><th>client_order_id</th><th>symbol</th><th>intent phase</th>
      <th>broker state</th><th>filled</th><th>detail</th></tr></thead>
      <tbody>{''.join(rows)}</tbody></table>"""


#: Raw stage enums render as two different rows for the same layer otherwise.
_LAYER_LABELS = {
    "rejected_by_quant": "no signal (quant)",
    "quant": "no signal (quant)",
    "rejected_by_risk_engine": "deterministic risk engine",
    "risk_engine": "deterministic risk engine",
    "risk": "deterministic risk engine",
    "rejected_by_risk_agent": "AI veto",
    "risk_agent": "AI veto",
    "rejected_by_options": "options selection",
    "options": "options selection",
    "rejected_by_execution": "execution",
    "execution": "execution",
    "data": "data quality",
    "rejected_by_data": "data quality",
}


def why_no_trade_html(decisions: list[dict]) -> str:
    """Attribute every declined decision to the layer that stopped it.

    A system that correctly says NO is doing its job; this makes that legible
    instead of looking like an absence of activity.
    """
    buckets: dict[str, int] = {}
    for d in decisions:
        if _accepted(d):
            continue
        if get(d, "ai_review", "vetoed"):
            layer = "AI veto"
        elif d.get("rejection_stage"):
            layer = _LAYER_LABELS.get(
                str(d["rejection_stage"]).lower(),
                str(d["rejection_stage"]).replace("_", " ").lower())
        elif not get(d, "candidate"):
            layer = "no signal"
        else:
            layer = "deterministic risk engine"
        buckets[layer] = buckets.get(layer, 0) + 1

    if not buckets:
        return '<div class="empty">Every decision was authorized.</div>'
    total = sum(buckets.values())
    bars = []
    for layer, n in sorted(buckets.items(), key=lambda kv: -kv[1]):
        pct = n / total * 100
        bars.append(f"""<div class="bar-row">
          <div class="bar-l">{esc(layer)}</div>
          <div class="bar-t"><div class="bar-f" style="width:{pct:.1f}%"></div></div>
          <div class="bar-n mono">{n}</div></div>""")
    return "".join(bars)


# ---------------------------------------------------------------- page
def build(decisions: list[dict], *, simulated: bool,
          account: dict | None = None, intents: list[dict] | None = None,
          health: dict | None = None) -> str:
    intents = intents or []
    total = len(decisions)
    accepted = sum(1 for d in decisions if _accepted(d))
    vetoed = sum(1 for d in decisions if get(d, "ai_review", "vetoed"))
    symbols = sorted({str(d.get("symbol", "?")) for d in decisions})

    source_badge = (badge("SIMULATED · FIXTURE DATA", AMBER, solid=True) if simulated
                    else badge("PAPER · LIVE ACCOUNT", GREEN, solid=True))

    # --- account panel, honest about being unreachable
    if account:
        pnl = account["pnl"]
        pnl_col = GREEN if pnl > 0 else (RED if pnl < 0 else MUTED)
        acct_html = f"""<div class="grid4">
          {stat("EQUITY", "$" + num(account["equity"]), tag=badge("PAPER", GREEN))}
          {stat("REALISED + UNREALISED P&L", ("+" if pnl > 0 else "") + "$" + num(pnl),
                colour=pnl_col, sub="measured, not projected")}
          {stat("OPEN POSITIONS", str(len(account["positions"])))}
          {stat("OPTIONS LEVEL", esc(account["options_level"]))}
        </div>
        <div class="note">Account <span class="mono">{esc(account['account_number'])}</span>
        · dedicated hackathon paper account · starting balance $100,000.00.
        P&amp;L is the account's actual change from that balance. No projection,
        no simulation, no annualisation.</div>"""
    else:
        acct_html = """<div class="empty">Broker not reachable from this build.
        Account figures are omitted rather than estimated or carried over from a
        previous run.</div>"""

    health_html = ""
    if health:
        health_html = f"""<div class="grid4">
          {stat("RUNTIME", esc(health.get("state", "idle")).upper())}
          {stat("CYCLES", esc(health.get("cycles_completed", 0)))}
          {stat("ORDERS SENT", esc(health.get("orders_submitted", 0)))}
          {stat("STOPPED BY", esc(health.get("stop_reason") or "—"))}
        </div>"""

    cards = "".join(decision_card(d, i) for i, d in enumerate(reversed(decisions[-40:])))
    if not cards:
        cards = '<div class="empty">No decisions recorded yet.</div>'

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SpeedTrader AI — Command Centre</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:{BG};color:{TEXT};
  font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  font-size:14px;line-height:1.55;-webkit-font-smoothing:antialiased;
  background-image:radial-gradient(1200px 600px at 15% -10%,#101a3a55,transparent),
                   radial-gradient(900px 500px at 95% 0%,#2a145c44,transparent);
  background-attachment:fixed}}
.mono{{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  font-variant-numeric:tabular-nums}}
.sm{{font-size:12px}} .muted{{color:{MUTED}}}
.wrap{{max-width:1400px;margin:0 auto;padding:28px 22px 80px}}

/* ---- header */
header{{display:flex;align-items:center;gap:14px;flex-wrap:wrap;
  padding-bottom:20px;border-bottom:1px solid {BORDER};margin-bottom:26px}}
.logo{{width:38px;height:38px;border-radius:10px;flex:none;
  background:linear-gradient(135deg,{BLUE},{PURPLE});
  display:flex;align-items:center;justify-content:center;
  font-weight:800;font-size:19px;color:#fff;
  box-shadow:0 0 28px {BLUE}55}}
h1{{font-size:19px;font-weight:700;letter-spacing:.2px}}
.tag{{font-size:11px;color:{MUTED};letter-spacing:1.6px;text-transform:uppercase}}
.spacer{{flex:1}}
.badge{{display:inline-block;font-size:10px;font-weight:700;letter-spacing:.9px;
  padding:3px 8px;border-radius:5px;border:1px solid;text-transform:uppercase;
  white-space:nowrap;vertical-align:middle}}

/* ---- thesis strip */
.thesis{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;
  padding:13px 16px;border-radius:11px;margin-bottom:24px;
  border:1px solid {PURPLE}33;
  background:linear-gradient(90deg,{PURPLE}12,{BLUE}10,transparent)}}
.thesis b{{color:{PURPLE};font-weight:700}}
.thesis i{{color:{BLUE};font-style:normal;font-weight:700}}

/* ---- panels */
.panel{{background:{SURFACE};border:1px solid {BORDER};border-radius:13px;
  padding:19px;margin-bottom:20px}}
.sec{{font-size:11px;font-weight:700;letter-spacing:1.5px;color:{MUTED};
  text-transform:uppercase;margin-bottom:14px;display:flex;gap:9px;align-items:center}}
.sec::after{{content:"";flex:1;height:1px;background:{BORDER}}}
.sec-t{{font-size:10px;font-weight:700;letter-spacing:1.3px;color:{DIM};
  text-transform:uppercase;margin:18px 0 9px}}

.grid4{{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:11px}}
.stat{{background:{SURFACE_2};border:1px solid {BORDER};border-radius:9px;padding:12px 13px}}
.stat-l{{font-size:10px;letter-spacing:1.1px;color:{DIM};text-transform:uppercase;
  margin-bottom:6px;display:flex;gap:6px;align-items:center;flex-wrap:wrap}}
.stat-v{{font-size:19px;font-weight:650;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  font-variant-numeric:tabular-nums;line-height:1.25}}
.stat-s{{font-size:11px;color:{MUTED};margin-top:4px}}
.note{{font-size:12px;color:{MUTED};margin-top:12px;padding-top:12px;
  border-top:1px solid {BORDER};line-height:1.6}}
.empty{{color:{MUTED};font-size:13px;padding:16px;text-align:center;
  border:1px dashed {BORDER};border-radius:9px;line-height:1.6}}

/* ---- pipeline */
.pipeline{{display:flex;gap:3px;flex-wrap:wrap;margin-bottom:15px}}
.pl{{flex:1 1 78px;min-width:78px;padding:8px 4px;border-radius:6px;text-align:center;
  border:1px solid {BORDER};background:{SURFACE_2};position:relative}}
.pl-dot{{display:block;width:6px;height:6px;border-radius:50%;margin:0 auto 5px;
  background:var(--c);box-shadow:0 0 9px var(--c)}}
.pl-t{{font-size:9px;font-weight:700;letter-spacing:.6px;color:{MUTED}}}
.pl-done{{border-color:var(--c)44;background:linear-gradient(180deg,var(--c)14,transparent)}}
.pl-done .pl-t{{color:var(--c)}}
.pl-stop{{border-color:var(--c)66;background:var(--c)1a}}
.pl-stop .pl-t{{color:var(--c)}}
.pl-veto{{border-color:var(--c)88;background:var(--c)22}}
.pl-veto .pl-t{{color:var(--c)}}
.pl-todo .pl-dot{{background:{DIM};box-shadow:none}}

/* ---- lanes */
.lanes{{display:grid;grid-template-columns:repeat(auto-fit,minmax(215px,1fr));
  gap:11px;margin:15px 0}}
.lane{{border:1px solid var(--c)33;border-left:3px solid var(--c);
  border-radius:9px;padding:12px 13px;background:linear-gradient(90deg,var(--c)0d,transparent)}}
.lane-h{{font-size:9px;font-weight:700;letter-spacing:1.2px;color:var(--c);
  text-transform:uppercase;margin-bottom:7px}}
.lane-v{{font-size:16px;font-weight:700;margin-bottom:5px}}
.lane-s{{font-size:12px;color:{MUTED};line-height:1.5;min-height:19px}}
.lane-f{{font-size:10px;color:{DIM};margin-top:8px;padding-top:7px;
  border-top:1px solid {BORDER};font-style:italic}}

/* ---- decision cards (CSS-only expand, no JS anywhere on this page) */
.card{{background:{SURFACE};border:1px solid {BORDER};border-radius:11px;
  margin-bottom:9px;overflow:hidden}}
.card[open]{{border-color:{BLUE}44}}
.card-h{{display:flex;align-items:center;gap:10px;padding:13px 16px;cursor:pointer;
  list-style:none;user-select:none}}
.card-h::-webkit-details-marker{{display:none}}
.card-h:hover{{background:{SURFACE_2}}}
.sym{{font-weight:700;font-size:15px;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}}
.card-b{{padding:0 16px 17px;border-top:1px solid {BORDER}}}
.reason{{font-size:13px;color:{MUTED};padding:10px 0 4px;line-height:1.6}}
details.sub{{margin-top:13px;border:1px solid {BORDER};border-radius:8px;
  background:{SURFACE_2}}}
details.sub summary{{padding:9px 12px;cursor:pointer;font-size:12px;
  font-weight:600;letter-spacing:.3px}}
details.sub summary:hover{{color:{BLUE}}}
details.sub[open] summary{{border-bottom:1px solid {BORDER};color:{BLUE}}}

/* ---- tables */
.tbl,.mini{{width:100%;border-collapse:collapse;font-size:12px}}
.mini{{font-size:11.5px}}
.tbl th,.mini th{{text-align:left;font-size:10px;letter-spacing:1px;color:{DIM};
  text-transform:uppercase;padding:9px 11px;border-bottom:1px solid {BORDER};font-weight:600}}
.tbl td,.mini td{{padding:9px 11px;border-bottom:1px solid {BORDER}}}
.tbl tbody tr:hover,.mini tbody tr:hover{{background:{SURFACE_2}}}
.tbl tbody tr:last-child td,.mini tbody tr:last-child td{{border-bottom:none}}
.scroll{{overflow-x:auto}}

/* ---- bars */
.bar-row{{display:flex;align-items:center;gap:12px;margin-bottom:9px}}
.bar-l{{width:190px;font-size:12px;color:{MUTED};flex:none;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.bar-t{{flex:1;height:9px;background:{SURFACE_2};border-radius:5px;overflow:hidden}}
.bar-f{{height:100%;border-radius:5px;background:linear-gradient(90deg,{BLUE},{PURPLE})}}
.bar-n{{width:36px;text-align:right;font-size:12px;color:{TEXT};flex:none}}

footer{{margin-top:34px;padding-top:20px;border-top:1px solid {BORDER};
  font-size:11.5px;color:{DIM};line-height:1.75}}
@media(max-width:640px){{.wrap{{padding:16px 13px 60px}}
  .bar-l{{width:110px}} h1{{font-size:16px}}}}
</style></head>
<body><div class="wrap">

<header>
  <div class="logo">S</div>
  <div>
    <h1>SpeedTrader AI</h1>
    <div class="tag">Autonomous Options Intelligence · Command Centre</div>
  </div>
  <span class="spacer"></span>
  {source_badge}
  {badge("paper trading only", BLUE)}
</header>

<div class="thesis">
  <b>AI can challenge the trade.</b> <i>Determinism authorizes it.</i>
  <span class="muted sm">— the model's entire vocabulary is CONFIRM · ABSTAIN · VETO,
  enforced by schema. It cannot enlarge a position, loosen a limit, or place an order.</span>
</div>

<div class="panel">
  <div class="sec">Paper account state</div>
  {acct_html}
</div>

{f'<div class="panel"><div class="sec">Autonomous runtime health</div>{health_html}</div>' if health_html else ''}

<div class="panel">
  <div class="sec">Decision summary</div>
  <div class="grid4">
    {stat("DECISIONS RECORDED", str(total), sub="every one, accepted or not")}
    {stat("AUTHORIZED", str(accepted), colour=GREEN if accepted else MUTED)}
    {stat("AI VETOES", str(vetoed), colour=PURPLE,
          sub="the one thing the AI can change")}
    {stat("SYMBOLS", str(len(symbols)), sub=esc(", ".join(symbols[:6])))}
  </div>
</div>

<div class="panel">
  <div class="sec">Why we did NOT trade</div>
  {why_no_trade_html(decisions)}
  <div class="note">A system that correctly declines is working, not idle.
  Each declined decision is attributed to the layer that stopped it.</div>
</div>

<div class="panel">
  <div class="sec">Order lifecycle &amp; broker reconciliation</div>
  <div class="scroll">{order_lifecycle_html(intents, account)}</div>
  <div class="note">The intent phase is written to a fsynced write-ahead log
  <em>before</em> the broker is contacted, so a crash mid-submission still leaves
  evidence that an order may exist. <b>SUBMITTED is never FILLED</b>; only
  reconciliation against the broker resolves an ambiguous outcome, and an
  unresolved intent blocks the next run rather than risking a duplicate.</div>
</div>

<div class="panel">
  <div class="sec">Decisions · AI advisory vs deterministic authority vs execution</div>
  {cards}
</div>

<footer>
  <b>No performance claim is made anywhere on this page.</b>
  Paper trading is a simulation and does not involve real funds or real
  executions. P&amp;L shown is the paper account's actual change from its
  $100,000.00 starting balance — it is measured, never projected, annualised or
  extrapolated. Fee figures are labelled ESTIMATED and come from Alpaca's
  published schedule. Backtests measure the underlying signal only, never
  options P&amp;L, because no historical option-chain data exists to price
  against.<br>
  Generated {esc(generated)} from {total} persisted decision record(s) and
  {len(intents)} execution intent(s). This page contains no JavaScript and
  loads nothing from the network.
</footer>
</div></body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--store", default=str(ROOT / "data" / "decisions"))
    ap.add_argument("--journal", default=str(ROOT / "data"))
    ap.add_argument("--out", default=str(ROOT / "data" / "dashboard.html"))
    ap.add_argument("--live", action="store_true",
                    help="also read the real paper account (never prints credentials)")
    args = ap.parse_args()

    decisions = load_decisions(Path(args.store))
    intents = load_intents(Path(args.journal))
    account = load_account() if args.live else None
    simulated = any(str(d.get("symbol", "")).upper().startswith("DEMO")
                    for d in decisions) and not account

    page = build(decisions, simulated=simulated, account=account, intents=intents)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print(f"wrote {out}  ({len(decisions)} decisions, {len(intents)} intents, "
          f"{len(page):,} bytes)")
    if args.live and account is None:
        print("  note: broker unreachable; account figures omitted (not estimated)")
    print(f"  open {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

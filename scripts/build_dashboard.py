#!/usr/bin/env python3
"""
SpeedTrader AI — command centre generator.

    python scripts/build_dashboard.py            # writes data/dashboard.html

Reads REAL decisions from the DecisionStore and emits one self-contained HTML
file. No server, no CDN, no build step, no network: a judge opens the file.

DESIGN INTENT
The hero is not a P&L number — it is the SEPARATION OF AUTHORITY. Three lanes,
visually distinct, so the central idea is legible without reading the source:

    AI RECOMMENDATION   advisory   ·  may only CONFIRM / ABSTAIN / VETO
    DETERMINISTIC       authority  ·  decides IF and HOW MUCH
    EXECUTION           outcome    ·  what the broker actually did

Everything derived from a simulation is labelled as such. Charts are hand-built
inline SVG so the file works offline and forever.

Colours are the validated dark steps from the data-viz reference palette
(blue #3987e5, aqua #199e70, violet #9085e9) — all six checks pass against the
#12141a surface. Status colours are the reserved fixed four and always ship with
a text label, never colour alone.
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from speedtrader.replay.fingerprint import (  # noqa: E402
    ai_influence, decision_fingerprint,
)

SURFACE = "#12141a"
S_BLUE, S_AQUA, S_VIOLET = "#3987e5", "#199e70", "#9085e9"
GOOD, WARNING, SERIOUS, CRITICAL = "#0ca30c", "#fab219", "#ec835a", "#d03b3b"


def esc(value) -> str:
    return html.escape(str(value if value is not None else "—"))


def load_decisions(store_dir: Path) -> list[dict]:
    out: list[dict] = []
    for path in sorted(store_dir.glob("decisions-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    # A corrupt line is reported by the store, not silently
                    # rendered. The dashboard shows what it could read.
                    continue
    return out


# ---------------------------------------------------------------- charts
def sparkline(values: list[float], *, w=560, h=120, colour=S_BLUE,
              label="") -> str:
    """A line with a crosshair-free hover title per point.

    One series, so no legend box — the title names it, per the palette rules.
    """
    if len(values) < 2:
        return (f'<div class="empty">not enough data to plot {esc(label)}</div>')
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    step = w / (len(values) - 1)
    pts = [(i * step, h - 8 - ((v - lo) / span) * (h - 24))
           for i, v in enumerate(values)]
    path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}"
                    for i, (x, y) in enumerate(pts))
    zero_y = h - 8 - ((0 - lo) / span) * (h - 24) if lo <= 0 <= hi else None
    dots = "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{colour}" '
        f'fill-opacity="0" class="pt"><title>{esc(label)} #{i}: '
        f'{values[i]:+.2f}R</title></circle>'
        for i, (x, y) in enumerate(pts))
    base = (f'<line x1="0" y1="{zero_y:.1f}" x2="{w}" y2="{zero_y:.1f}" '
            f'stroke="#3a3f4b" stroke-width="1" stroke-dasharray="3 3"/>'
            if zero_y is not None else "")
    return (
        f'<svg viewBox="0 0 {w} {h}" class="chart" role="img" '
        f'aria-label="{esc(label)}">{base}'
        f'<path d="{path}" fill="none" stroke="{colour}" stroke-width="2" '
        f'stroke-linejoin="round" stroke-linecap="round"/>{dots}</svg>')


def bars(pairs: list[tuple[str, float]], *, w=560, h=150,
         colours: dict[str, str] | None = None) -> str:
    """Horizontal category bars, 4px rounded ends, 2px gaps, direct labels."""
    if not pairs:
        return '<div class="empty">no decisions recorded yet</div>'
    colours = colours or {}
    top = max(v for _, v in pairs) or 1
    row = h / len(pairs)
    out = []
    for i, (name, value) in enumerate(pairs):
        y = i * row + 3
        bh = row - 8
        bw = max(2.0, (value / top) * (w - 190))
        c = colours.get(name, S_BLUE)
        out.append(
            f'<rect x="150" y="{y:.1f}" width="{bw:.1f}" height="{bh:.1f}" '
            f'rx="4" fill="{c}"><title>{esc(name)}: {value:g}</title></rect>'
            f'<text x="142" y="{y + bh * 0.72:.1f}" class="cat" '
            f'text-anchor="end">{esc(name)}</text>'
            f'<text x="{150 + bw + 8:.1f}" y="{y + bh * 0.72:.1f}" '
            f'class="val">{value:g}</text>')
    return (f'<svg viewBox="0 0 {w} {h}" class="chart" role="img">'
            + "".join(out) + "</svg>")


# ---------------------------------------------------------------- render
def pipeline(d: dict) -> str:
    """The decision chain. Each stage shows reached / blocked / not-reached."""
    review = d.get("ai_review") or {}
    gate = d.get("risk_gate") or {}
    trace = d.get("options_trace") or {}
    state = d.get("state", "")
    stage_rej = (d.get("rejection_stage") or "")

    reached_quant = bool(d.get("candidate"))
    reached_risk = bool(gate)
    risk_ok = gate.get("verdict") in ("PASS", "REDUCE")
    reached_ai = bool(review)
    vetoed = bool(review.get("vetoed"))
    reached_opt = bool(trace)
    executed = state in ("EXECUTING", "OPEN", "CLOSED")

    stages = [
        ("DATA", bool(d.get("snapshot")), "snapshot built"),
        ("QUANT", reached_quant,
         (d.get("candidate") or {}).get("strategy_id") or "no signal"),
        ("RISK", reached_risk and risk_ok, gate.get("verdict") or "not reached"),
        ("AI", reached_ai and not vetoed,
         (review.get("judge") or {}).get("verdict") or "not consulted"),
        ("OPTIONS", reached_opt, (trace.get("contract") or {}).get("symbol")
         or "not reached"),
        ("AUTH", executed, "licence minted" if executed else "not minted"),
        ("EXEC", executed, state),
    ]
    cells = []
    for name, ok, detail in stages:
        cls = "ok" if ok else ("blocked" if name in stage_rej or
                               (name == "AI" and vetoed) else "idle")
        cells.append(
            f'<div class="stage {cls}"><b>{esc(name)}</b>'
            f'<span>{esc(detail)}</span></div>')
    return '<div class="pipe">' + '<i>→</i>'.join(cells) + "</div>"


def decision_card(d: dict) -> str:
    c = d.get("candidate") or {}
    gate = d.get("risk_gate") or {}
    trace = d.get("options_trace") or {}
    sizing = trace.get("sizing") or {}
    contract = trace.get("contract") or {}
    review = d.get("ai_review") or {}
    judge = review.get("judge") or {}
    inf = ai_influence(d)
    fp = decision_fingerprint(d)

    checks = gate.get("checks") or []
    passed = sum(1 for x in checks if x.get("passed"))
    failed = [x.get("rule") for x in checks if not x.get("passed")]

    ai_state = judge.get("verdict") or "NOT CONSULTED"
    ai_cls = {"VETO": "bad", "CONFIRM": "good", "ABSTAIN": "muted"}.get(
        ai_state, "muted")
    det_cls = {"PASS": "good", "REDUCE": "warn", "REJECT": "bad"}.get(
        gate.get("verdict"), "muted")
    exec_state = d.get("state", "—")
    exec_cls = "good" if exec_state in ("EXECUTING", "OPEN") else "muted"

    return f"""
<article class="card">
  <header>
    <span class="fp" title="deterministic fingerprint — excludes the AI review">
      ⛓ {esc(fp)}</span>
    <span class="sym">{esc(d.get('symbol'))}</span>
    <span class="when">{esc((d.get('created_at') or '')[:19])}</span>
  </header>
  {pipeline(d)}
  <div class="lanes">
    <div class="lane advisory">
      <h4>AI · advisory</h4>
      <p class="verdict {ai_cls}">{esc(ai_state)}</p>
      <p class="note">{esc((judge.get('reasoning') or '—')[:150])}</p>
      <p class="meta">model {esc(inf.get('model') or 'none')} ·
         changed outcome: <b>{'YES' if inf.get('changed_outcome') else 'NO'}</b></p>
    </div>
    <div class="lane authority">
      <h4>DETERMINISTIC · authority</h4>
      <p class="verdict {det_cls}">{esc(gate.get('verdict') or '—')}</p>
      <p class="note">{passed}/{len(checks)} checks passed
        {('· blocked on ' + esc(failed[0])) if failed else ''}</p>
      <p class="meta">score {esc(c.get('total_score'))} ·
         EV {esc(round(c.get('expected_value', 0), 3))}R</p>
    </div>
    <div class="lane outcome">
      <h4>EXECUTION · outcome</h4>
      <p class="verdict {exec_cls}">{esc(exec_state)}</p>
      <p class="note">{esc(contract.get('symbol') or 'no contract')}</p>
      <p class="meta">{esc(sizing.get('contracts') or 0)} contract(s) ·
        max loss ${esc(sizing.get('max_loss_total') or 0)} of
        ${esc(sizing.get('risk_budget') or 0)} budget</p>
    </div>
  </div>
  <p class="why">{esc(d.get('rejection_reason') or 'accepted by every layer')}</p>
</article>"""


def build(decisions: list[dict], *, simulated: bool) -> str:
    total = len(decisions)
    accepted = [d for d in decisions if d.get("state") in
                ("EXECUTING", "OPEN", "CLOSED")]
    vetoed = [d for d in decisions if (d.get("ai_review") or {}).get("vetoed")]
    stages = Counter(d.get("rejection_stage") or "accepted" for d in decisions)

    # Why we did NOT trade — the analysis competitors do not show.
    not_traded = [(k.replace("REJECTED_BY_", "").title(), v)
                  for k, v in stages.most_common() if k != "accepted"]

    fingerprints = [decision_fingerprint(d) for d in decisions]
    unique_fp = len(set(fingerprints))

    not_traded_svg = (
        bars(not_traded, colours={name: CRITICAL for name, _ in not_traded})
        if not_traded else
        '<div class="empty">every recorded decision reached execution</div>')

    banner = ("SIMULATED / PAPER DATA — generated from the offline demo. "
              "No real capital." if simulated else
              "PAPER TRADING — Alpaca paper account. Not real capital.")

    cards = "".join(decision_card(d) for d in decisions[-8:][::-1]) or \
        '<div class="empty">no decisions yet — run scripts/run_options_demo.py</div>'

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SpeedTrader AI — Command Centre</title>
<style>
  :root {{ color-scheme: dark;
    --surface: {SURFACE}; --panel:#181b23; --line:#262b36;
    --ink:#f2f4f8; --ink2:#a7aebd; --ink3:#6d7488;
    --blue:{S_BLUE}; --aqua:{S_AQUA}; --violet:{S_VIOLET};
    --good:{GOOD}; --warn:{WARNING}; --serious:{SERIOUS}; --bad:{CRITICAL}; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--surface); color:var(--ink);
    font:14px/1.5 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif; }}
  .wrap {{ max-width:1180px; margin:0 auto; padding:20px; }}
  .banner {{ background:#3a2a12; border:1px solid var(--warn);
    color:#ffd98a; padding:9px 14px; border-radius:8px; font-weight:600;
    letter-spacing:.02em; margin-bottom:18px; }}
  h1 {{ font-size:22px; margin:0 0 2px; letter-spacing:-.01em; }}
  .sub {{ color:var(--ink2); margin:0 0 18px; }}
  .grid {{ display:grid; gap:14px; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); }}
  .tile {{ background:var(--panel); border:1px solid var(--line);
    border-radius:10px; padding:14px 16px; }}
  .tile .k {{ color:var(--ink3); font-size:11px; text-transform:uppercase;
    letter-spacing:.08em; }}
  .tile .v {{ font-size:26px; font-weight:650; margin-top:4px;
    font-variant-numeric:tabular-nums; }}
  .tile .s {{ color:var(--ink2); font-size:12px; }}
  section {{ margin-top:26px; }}
  h2 {{ font-size:13px; text-transform:uppercase; letter-spacing:.1em;
    color:var(--ink3); border-bottom:1px solid var(--line);
    padding-bottom:7px; margin:0 0 14px; }}
  .card {{ background:var(--panel); border:1px solid var(--line);
    border-radius:10px; padding:14px 16px; margin-bottom:14px; }}
  .card header {{ display:flex; gap:12px; align-items:center;
    color:var(--ink2); font-size:12px; margin-bottom:12px; }}
  .fp {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
    color:var(--violet); background:#1e1b2e; padding:2px 8px;
    border-radius:5px; }}
  .sym {{ font-weight:700; color:var(--ink); font-size:14px; }}
  .when {{ margin-left:auto; }}
  .pipe {{ display:flex; align-items:stretch; gap:2px; flex-wrap:wrap;
    margin-bottom:14px; }}
  .pipe i {{ color:var(--ink3); align-self:center; font-style:normal; }}
  .stage {{ flex:1; min-width:78px; background:#11141b; border-radius:6px;
    padding:7px 8px; border-top:3px solid var(--ink3); }}
  .stage b {{ display:block; font-size:10px; letter-spacing:.09em;
    color:var(--ink2); }}
  .stage span {{ font-size:10px; color:var(--ink3);
    display:block; overflow:hidden; text-overflow:ellipsis; }}
  .stage.ok {{ border-top-color:var(--good); }}
  .stage.ok b {{ color:var(--ink); }}
  .stage.blocked {{ border-top-color:var(--bad); }}
  .lanes {{ display:grid; gap:10px; grid-template-columns:repeat(3,1fr); }}
  .lane {{ border-radius:8px; padding:11px 12px; border:1px solid var(--line);
    background:#11141b; }}
  .lane h4 {{ margin:0 0 7px; font-size:10px; letter-spacing:.09em;
    color:var(--ink3); }}
  .lane.advisory {{ border-left:3px solid var(--violet); }}
  .lane.authority {{ border-left:3px solid var(--blue); }}
  .lane.outcome {{ border-left:3px solid var(--aqua); }}
  .verdict {{ margin:0; font-size:17px; font-weight:700; }}
  .verdict.good {{ color:var(--good); }} .verdict.bad {{ color:var(--bad); }}
  .verdict.warn {{ color:var(--warn); }} .verdict.muted {{ color:var(--ink2); }}
  .note {{ margin:5px 0 0; font-size:12px; color:var(--ink2); }}
  .meta {{ margin:5px 0 0; font-size:11px; color:var(--ink3); }}
  .why {{ margin:11px 0 0; padding-top:10px; border-top:1px solid var(--line);
    font-size:12px; color:var(--ink2); }}
  .chart {{ width:100%; height:auto; }}
  .cat {{ fill:var(--ink2); font-size:11px; }}
  .val {{ fill:var(--ink); font-size:11px; font-variant-numeric:tabular-nums; }}
  .pt:hover {{ fill-opacity:1; }}
  .empty {{ color:var(--ink3); padding:22px; text-align:center;
    border:1px dashed var(--line); border-radius:8px; }}
  footer {{ margin-top:30px; padding-top:14px; border-top:1px solid var(--line);
    color:var(--ink3); font-size:12px; }}
  @media (max-width:760px) {{ .lanes {{ grid-template-columns:1fr; }} }}
</style></head><body><div class="wrap">

<div class="banner">⚠ {esc(banner)}</div>

<h1>SpeedTrader AI — Command Centre</h1>
<p class="sub">The AI can challenge a trade. Only deterministic systems authorise one.</p>

<div class="grid">
  <div class="tile"><div class="k">Decisions recorded</div>
    <div class="v">{total}</div><div class="s">every outcome, not just trades</div></div>
  <div class="tile"><div class="k">Reached execution</div>
    <div class="v" style="color:var(--aqua)">{len(accepted)}</div>
    <div class="s">passed every deterministic gate</div></div>
  <div class="tile"><div class="k">AI vetoes</div>
    <div class="v" style="color:var(--violet)">{len(vetoed)}</div>
    <div class="s">the AI's only power</div></div>
  <div class="tile"><div class="k">Distinct fingerprints</div>
    <div class="v">{unique_fp}<span style="font-size:14px;color:var(--ink3)">/{total}</span></div>
    <div class="s">identical states hash identically</div></div>
</div>

<section>
  <h2>Why we did NOT trade</h2>
  {not_traded_svg}
  <p class="sub" style="margin-top:10px;font-size:12px">
    Most systems only show the trades they took. Each bar is a decision the
    system declined, attributed to the layer that stopped it.</p>
</section>

<section>
  <h2>Recent decisions — AI advisory vs deterministic authority vs execution</h2>
  {cards}
</section>

<footer>
  Generated {esc(datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'))} UTC ·
  self-contained, no network required ·
  fingerprints exclude the AI review by construction, so the same market state
  hashes identically whether the model confirmed, abstained or never ran.
  <br>Paper trading only. Nothing here is investment advice and no claim is made
  that any strategy is profitable.
</footer>
</div></body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--store", default=str(ROOT / "data" / "decisions"))
    ap.add_argument("--out", default=str(ROOT / "data" / "dashboard.html"))
    ap.add_argument("--live", action="store_true",
                    help="label as paper-account data rather than demo output")
    args = ap.parse_args()

    decisions = load_decisions(Path(args.store))
    html_text = build(decisions, simulated=not args.live)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_text, encoding="utf-8")
    print(f"wrote {out}  ({len(decisions)} decisions, {len(html_text):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

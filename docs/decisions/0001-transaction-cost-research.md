# 0001 — Transaction cost research (§46 evidence)

**Status:** research complete; ONE owner decision outstanding
**Date:** 2026-09-02
**Scope:** evidence only. No configuration value was changed by this document.

---

## Why this exists

§46 left four questions open and instructed that no production cost value be
invented until they were resolved. Three of the four are answerable from a
primary source. This records that source and what it actually says, so the
remaining decision is a narrow factual attestation rather than a guess.

---

## Source

| | |
|---|---|
| Document | Alpaca Securities LLC — Brokerage Fee Schedule |
| URL | https://files.alpaca.markets/disclosures/library/BrokFeeSched.pdf |
| Revision stated in document | **Revised on September 1, 2026** (page 6; also in the PDF title metadata) |
| Retrieved | 2026-09-02 |
| Confidence | **High** — figures read from the PDF text directly |

> **Methodology note.** The document was first summarised by an automated reader,
> which reported the SEC fee as "$0.000119 **per share**". That is wrong: the SEC
> transaction fee is definitionally a rate applied to *trade value*, not a
> per-share charge. The summary was discarded and every figure below was read
> from the extracted PDF text instead. This is why summarised financial data is
> not treated as a source here.

---

## Verified: regulatory fees (page 3, "Equities")

| Fee | Applies | Rate | Matches `execution_config.yaml`? |
|---|---|---|---|
| SEC Transaction Fee | **sells only** | `$0.0000206 × Trade Value` | ✅ `sec_rate_of_notional: 0.0000206` |
| FINRA TAF | **sells only** | `$0.000195 per share`, max **$9.79/trade** (cap at 50,205 shares) | ✅ `taf_per_share: 0.000195` |
| FINRA CAT | **buys and sells** | `$0.000003 per executed equivalent share` (NMS: 1 share = 1 equivalent share) | ✅ `cat_per_share: 0.000003` |

**All three configured regulatory rates are confirmed correct against the primary
source, including the sell-side-only applicability, the TAF cap and its share
threshold, and the buys-and-sells applicability of CAT.**

This also independently validates the cost model's shape: the SEC component is a
rate on notional and the other two are per-share, which is exactly why a single
flat per-share constant cannot represent all three.

### §46 Q4 — regulatory representation: **RESOLVED, no change needed.**

---

## Verified: commission (page 1, "Transaction Commissions")

The schedule states:

> "Alpaca Securities does not charge commissions, **except as described below**.
> Commissions apply to index options trades, use of the **Elite Smart Router**
> under the Alpaca Elite offering, and **order flow determined to be non-retail**
> in nature.
>
> Certain arrangements with **authorized business partners** may also preclude
> commission-free trades…"

So for US equities, commission is **$0.00** unless one of three conditions holds:

1. the order is routed through the **Elite Smart Router** (Alpaca Elite);
2. the order flow is determined to be **non-retail**;
3. the account was established via an **authorized business partner**.

Where the Elite Smart Router *does* apply, equity commissions are published
(page 2): all-in fixed **$0.0040/share** up to 200,000 monthly shares, or
**$0.0025/share** fixed-plus-tiered, decreasing with monthly volume.

### Correction to an existing comment

`configs/execution_config.yaml` currently states that the schedule quotes
commissions as a *"RANGE (0%–3% per transaction)"*. **No such range appears in
the current schedule.** That comment appears to derive from an older or misread
revision and should not be relied on. The schedule is not a range — it is
"free, except in three named cases".

### §46 Q2 — commission: **NOT resolvable from any document.**

Which of the three conditions applies is a property of *this account*, not of
the schedule. No document can answer it. This is the one outstanding decision.

---

## §46 Q1 — paper vs live-equivalent economics

Already decided and implemented: EV models **live-equivalent execution
economics**, not what the paper simulator debits. Rationale (recorded in
`execution_config.yaml`): zeroing costs because the simulator does not bill them
would make the positive-EV gate measure the simulator rather than the strategy.

Recommended follow-up: promote this from a YAML comment to a machine-readable
`cost_basis` field so it is persisted onto every decision and is auditable.
Currently a reader of the decision record cannot tell which basis was used.

## §46 Q3 — slippage

Unchanged and correctly encoded: `per_share: 0.0`, `source: operator_assumption`,
with an assumption string stating explicitly that this is *not* a claim that real
slippage is zero, only that none has been measured yet. EV is optimistic by
exactly the unmeasured slippage until execution history exists.

---

## Incidental finding — short borrow

Page 5: "Easy to Borrow Securities — Free (no short borrow fees on Easy to
Borrow securities)"; Hard to Borrow is "Not currently available". The
`short_borrow_cost` entry in `cost_policy.EXCLUSIONS` is therefore an omission of
something that is currently **zero** for tradeable shorts, not a hidden cost.
Kept in the exclusions list because it is a real modelling gap if Alpaca later
enables hard-to-borrow.

---

## Rate drift

`rates_effective_date` in the config is `"2026-07-20"`; the retrieved schedule
states `September 1, 2026`. **The rates themselves are identical**, so nothing is
mispriced — but the provenance date no longer matches the document it cites.

Correcting it touches `configs/execution_config.yaml`,
`tests/unit/quant/test_cost_policy.py` (which pins both the date and the
`reference` string), and is therefore bundled with the commission decision below
rather than applied piecemeal.

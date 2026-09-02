# SpeedTrader AI

Alpaca-native multi-agent quantitative trading intelligence system.
Built for the Alpaca AI Trading Agents Hackathon (28 Aug – 4 Sep 2026).

## Attribution

This project builds on **[TradingAgents](https://github.com/TauricResearch/TradingAgents)**
by Tauric Research, used under the Apache-2.0 licence, for the multi-agent
reasoning layer (analysts, bull/bear debate, research manager, trader).

The quantitative core, deterministic risk engine, execution guard, Alpaca
integration and decision-trace layer are original work.

## What we add that TradingAgents does not have

| | TradingAgents | SpeedTrader AI |
|---|---|---|
| Market data | Yahoo Finance / Alpha Vantage | **Alpaca** |
| Signal origin | user names a ticker | **quant core generates candidates** |
| Risk control | LLM risk discussion | **+ deterministic hard gate** |
| Execution | internal simulated exchange | **Alpaca paper trading** |
| Decision record | markdown log | **structured, replayable JSON trace** |

TradingAgents documents that its output is not reproducible run-to-run — expected
for an LLM research tool. We add the layer that makes the *financial authorisation*
reproducible even when the reasoning is not.

## Golden rule

    AI proposes. Deterministic controls authorise.

The risk engine may REJECT even when every agent said BUY.

## Status

- [x] Block 0 — schemas, IDs, TTL, config layer
- [x] Block C — deterministic risk engine
- [x] Block A.1 — Alpaca client + market data (protocol, 2 implementations)
- [ ] Block A.2 — account, positions, orders, news, reconciliation
- [ ] Block B — quant core, S1–S14
- [ ] Block D — execution guard
- [ ] Block E — Alpaca paper execution
- [ ] Block F — position monitor & outcomes
- [ ] Block G — decision log store & UI

## Not financial advice. Paper trading is a simulation, not proof of live profitability.

## Quick start

    pip install -e ".[dev]"
    pytest -q
    python scripts/demo_risk_gate.py

## Layout

    src/speedtrader/
      common/    clock, ids            (one source of "now", one of identity)
      data/      schemas.py            THE contract — every block builds against this
      quant/     engine, strategies/   S1-S14
      agents/    registry, analysts    thin wrappers over the TradingAgents graph
      bridge/    tradingagents.py      the integration boundary
      risk/      engine, measures      deterministic gate + the ONLY sizing function
      portfolio/ manager, revalidation
      execution/ guard, adapter        the only component with execution rights
      monitoring/ memory/ replay/ evaluation/ storage/

Full planned tree. Where the plan placed the same responsibility in two modules
(position_sizing under quant/ and risk/, schemas under agents/ and data/, time under
common/ data/ and decision/), both import paths exist and both work — the second is a
re-export of the first. One implementation, no drift.

The data layer is a Protocol with two implementations: `AlpacaMarketData` (live) and
`FixtureMarketData` (deterministic). The second is what makes §89 Decision Replay a
configuration change rather than a rewrite.

# Trading Platform

Intraday equity trading platform: news + sentiment + technical indicators + ES futures wall scanner + risk-validated execution + EOD journaling.

This repo is the **shared infrastructure base**. Strategy-specific
research and refinement happens in dedicated fork repos:

- [`trading-model-gap-and-go`](https://github.com/NZ1979/trading-model-gap-and-go) — gap-and-go strategy on Russell 2000 small/mid-caps
- [`trading-model-llm`](https://github.com/NZ1979/trading-model-llm) — LLM-driven signal generation (Qwen 72B / Claude)

Codified 2026-05-07: changes that go in **this** repo are
infrastructure-grade — bug fixes for production stability, generic
data-feed improvements, deploy-procedure refinements, cross-cutting
refactors usable by any strategy fork. Strategy-specific tuning
(universe selection, regime filters, signal-selectivity heuristics,
prompt engineering) belongs in the fork that uses it.

Forks pull from this base via `git fetch upstream && git merge
upstream/main`. Bug fixes here flow into all forks; strategy choices
in forks stay isolated.

## Stack (all already decided, do not change)

| Component | Service | Cost |
|---|---|---|
| Equity broker | Alpaca paper | $0 |
| Equity real-time bars | Alpaca SIP (Algo Trader Plus) | $99/mo |
| Equity historical | Polygon Stocks Starter | $29/mo |
| News | Alpaca News WebSocket (Benzinga) | $0 |
| Sentiment | Claude Haiku 4.5 | ~$2-4/day |
| ~~Futures order book~~ | ~~Databento CME Standard~~ | ~~$179/mo~~ — **CANCELED 2026-04-28** (Standard tier excludes live MBP-10; Plus tier $1,500/mo is too expensive). Code dormant; `futures.enabled: false` in config. |
| Compute | Hetzner CPX21 Ashburn VA | $8/mo |
| Storage | SQLite (local file) | $0 |

**Total: ~$200-260/mo** (sentiment is variable).

## Required environment variables

```bash
ALPACA_API_KEY
ALPACA_API_SECRET
ANTHROPIC_API_KEY
POLYGON_API_KEY
DATABENTO_API_KEY
```

## Quickstart (local test, requires Python 3.12+)

```bash
pip install -r requirements.txt

# set the 5 env vars above

python main.py config/settings.yaml
```

## Project structure

```
trading_platform/
├── config/settings.yaml           # all tunable params
├── main.py                        # asyncio orchestrator
├── requirements.txt
├── data/
│   ├── bar_types.py               # MinuteBar / FiveMinBar dataclasses
│   ├── news_feed.py               # Alpaca news WebSocket
│   ├── news_pipeline.py           # batches headlines + persists scores
│   ├── alpaca_market_data.py      # SIP bars WebSocket
│   ├── bar_aggregator.py          # 1-min → 5-min rollup
│   ├── polygon_feed.py            # historical (REST only)
│   └── databento_feed.py          # ES MBP-10 live
├── analysis/
│   ├── sentiment.py               # Claude Haiku batch scoring
│   ├── indicators.py              # SMA, RSI, MACD, Bollinger, ADX, VWAP, gap-and-go logic
│   └── futures_walls.py           # detect_walls + persistent anti-spoofing scanner
├── strategy/
│   ├── signal_engine.py           # evaluate_trade() combining all 3 signals
│   └── risk.py                    # validate_order(), size_from_risk()
├── execution/
│   └── alpaca_orders.py           # bracket orders + 30s equity cache
├── journal/
│   └── eod_report.py              # daily markdown report generator
├── scripts/
│   └── test_databento_connection.py   # standalone Databento Live connection test (currently irrelevant — Databento canceled)
├── tests/
│   ├── test_bug_a_fix.py              # regression: gap-and-go reachability
│   ├── test_bug_b_fix.py              # regression: SymbolState dedup defaults
│   └── test_bugs_d_and_e.py           # regression: HTTP error capture + multi-status parsing
└── docs/
    ├── audits/                        # historical empirical audits of the codebase
    └── patches/                       # per-deployment bug-fix records
```

## Running the tests

```bash
pip install -r requirements.txt
cd <repo root>
python -m pytest tests/   # or run files directly: python tests/test_bug_a_fix.py
```

Tests import production modules unmodified and exercise them with mocked I/O. They're regression tests for the five bugs fixed in 2026-04-29 / 04-30 / 05-02 (see `docs/patches/` for per-deploy detail).

## Daily timeline (ET)

- **08:30** — Polygon REST backfill: daily bars + 20-day pre-market volume baselines
- **09:00** — Alpaca SIP + Databento Live connect; 1-min bars start streaming
- **09:30** — Pre-market context computed per symbol
- **09:35** — Signal engine begins evaluating; orders placed when signals fire
- **15:55** — Flatten all positions (cancel orders first, then close)
- **16:30** — EOD journal markdown report written to `journals/<date>.md`

## What's logged

- `decisions` table in SQLite: every Buy/Sell/Hold decision with reasons
- `orders` table in SQLite: every submitted/rejected order
- `journals/<YYYY-MM-DD>.md` files: human-readable daily summary

## VPS deployment

See the trading-platform-vps-deploy skill or the README in your skills bundle.

# Trading Platform — Narrative Overview

## Goal

The platform's job is to identify and execute short-duration intraday equity trades on the S&P 500, using a combination of news sentiment, price-action indicators, and (originally) futures market structure as confirming evidence. The trading approach is paper-only for now, deliberately, while it accumulates a clean track record before any consideration of going live.

A "successful trade" here means catching either of two well-defined setups during regular trading hours: a gap-and-go momentum continuation in the first 25 minutes after the open, or a pullback reversal during a trending session. Each trade carries a hard 2% stop loss and is sized for 0.5% portfolio risk per entry, with a 20% per-position cap and a 90% total-exposure cap. Every position is flattened at 15:55 ET regardless of outcome — no overnight holds, no swing trades, no exceptions. A daily journal captures every decision the engine made and every order the broker accepted or rejected, written to disk at 16:30 ET as a markdown file.

## Data and sources

Real-time market data flows in over Alpaca's SIP-feed WebSocket (Algo Trader Plus tier, $99/month), which streams 1-minute bars for all 503 watchlist symbols continuously from pre-market through after-hours. Historical bars come from Polygon's Stocks Starter tier ($29/month) over REST: daily bars for regime context, plus 20 days of pre-market minute bars for computing relative-volume baselines. Polygon's data is 15 minutes delayed, so it is used only for backfill, never for real-time decisions.

News headlines stream over Alpaca's News WebSocket, which surfaces the Benzinga firehose at no additional cost. Each headline mentioning a watchlist ticker gets queued for sentiment scoring. Every 60 seconds the queue is flushed in a single batched call to Anthropic's Claude Haiku 4.5 model, which returns an integer sentiment score on a -10 to +10 scale per headline. The platform burns approximately $5-15 per day in Anthropic credits at the 503-watchlist scale.

Account state (positions, equity, buying power) is polled from Alpaca's paper trading REST endpoint. Equity is cached for 30 seconds to avoid burning rate limits on the per-tick risk validator. Orders are submitted via the same REST endpoint as one-triggers-other (OTO) bracket orders: an entry limit order paired with a child stop-loss leg on Alpaca's books.

A futures-walls subsystem was originally part of the design, fed by Databento's ES MBP-10 stream, but Databento was canceled on 2026-04-28 because the Standard tier ($179/month) does not include live MBP-10 access and the Plus tier ($1,500/month) was too expensive for a paper-trading prototype. The wall-detection code is dormant but still in the repo; the per-stock options-walls successor (Polygon Options Starter) is deferred until the platform has a clean week of paper data.

## Signal engine

Bar handling is the most active part of the pipeline. Each 1-minute bar arriving over the WebSocket is forwarded to a per-symbol bar aggregator that rolls 5 one-minute bars into a single 5-minute bar at every multiple-of-5 minute boundary. As each new 5-minute bar is emitted, the orchestrator decides whether it falls inside RTH (9:30 to 16:00 ET) or pre-market. Pre-market bars accumulate in a buffer used to compute the per-symbol pre-market context at 9:30 ET. RTH bars feed the indicator pipeline directly.

The indicator pipeline runs on the combined PM-plus-RTH dataframe but filters down to RTH bars before computing anything (pre-market volume distorts most short-period indicators). Once 50 RTH bars have accumulated and the indicators have warmed up, the signal engine becomes eligible to fire on every subsequent bar.

Signal evaluation has two paths and they are checked in priority order. The gap-and-go path looks for a meaningful overnight gap (≥1%) backed by unusual pre-market volume (RVOL ≥ 5x the 20-day baseline) where the price has held above the pre-market low through the open. It only fires within the 9:35 to 10:00 ET window because the strategy is specifically a momentum-continuation bet that the gap holds. Sentiment must be at least +3 (or below -3 for a short) for the trade to actually fire. The pullback path looks for a daily-trend stock that has briefly fallen into oversold territory (RSI < 35) while staying above its intraday SMA-20 and VWAP, with the MACD histogram crossing back from negative to positive in the last three bars. Sentiment requirement is stricter at ±5 because pullback entries fight current price action. Both paths skip the first 5 minutes of RTH (9:30-9:35 ET) as too volatile.

When a Buy or Sell signal fires, the decision combiner in `evaluate_trade` gates it through sentiment thresholds, futures-wall alignment (currently confirming-only since walls are disabled), and the configured risk validator, which sizes the order based on account equity and the 2% stop distance. If approved, an OTO bracket order is submitted to Alpaca with the entry as a limit order at the latest 5-minute close and the stop loss as a child order at 2% adverse to entry.

## Indicators in use

Two scopes of indicators run separately. At the daily timeframe, computed once per ticker per day from Polygon's daily bars during the 8:30 ET backfill, the engine derives a regime classification (bull if close is more than 0.5% above SMA200, bear if more than 0.5% below, neutral in between) and a trending flag (true when daily ADX-14 is above 20). These are the gating filters for the pullback path, which only fires on bull-trending stocks for buys and bear-trending stocks for sells.

At the intraday 5-minute timeframe, on every new RTH bar, the engine computes SMA-20 and SMA-50, EMA-9, RSI-14, MACD with the standard 12/26/9 parameters, Bollinger Bands at 20-period and 2 standard deviations, ADX-DMI on a 14-period, and session-anchored VWAP. The pullback path uses RSI for overbought/oversold detection, MACD histogram for the cross signal, SMA-20 as the trend filter, ADX and DMI for confidence boosting (additional points when ADX > 20 and the trend-aligned DMI is dominant), volume against the trailing 20-bar average for a volume-spike bonus, and VWAP as the price floor for buys (price must be at or above 99.7% of VWAP).

The gap-and-go path is more streamlined and uses fewer intraday indicators. It reads only the latest close against the pre-market high and low and the gap-percentage figure derived from the prior daily close. The path explicitly does not depend on SMA-50 or the longer-warmup indicators, which is what allows it to fire from the very first 5-minute bar after 9:35 ET.

## API endpoints and connections

The platform talks to four external services. Alpaca is the primary integration, accessed across three different surfaces: the market-data REST API at `data.alpaca.markets` for snapshot and historical bar queries, the market-data WebSocket at `wss://stream.data.alpaca.markets/v2/sip` for real-time 1-minute bar streaming, and the paper-trading REST API at `paper-api.alpaca.markets` for account, positions, and orders. All three use the same APCA-API-KEY-ID and APCA-API-SECRET-KEY pair stored in `/etc/trading-platform/env` on the VPS.

News flows over a fourth Alpaca surface, the news WebSocket at `wss://stream.data.alpaca.markets/v1beta1/news`, which pushes Benzinga-sourced headlines tagged with the symbols they reference. Sentiment scoring sends batched headlines to the Anthropic Messages API at `api.anthropic.com` using the `claude-haiku-4-5-20251001` model, with prompt caching enabled to reduce per-headline cost. Polygon's REST API at `api.polygon.io` serves historical daily bars and 1-minute bar windows for the 8:30 ET backfill and the 20-day pre-market volume baseline computation.

Databento is currently disabled, but the wiring remains: when `futures.enabled` in `settings.yaml` is true and a `DATABENTO_API_KEY` is set, the platform connects to Databento's Live API for ES futures MBP-10 data. The current production config has it disabled, and the cost analysis confirmed that re-enabling it requires the Plus tier at $1,500/month, which was rejected.

The platform's outbound traffic is therefore limited and predictable: continuous WebSocket connections to two Alpaca streams (bars and news), periodic REST polls to Alpaca for account state, scheduled REST polls to Polygon at 8:30 ET, and burst-style POST calls to Anthropic when news arrives. All four services use API key authentication; there are no OAuth flows, no refresh tokens, no broker password storage. The keys live in a 0600-mode env file owned by root on the VPS, loaded into the systemd unit via `EnvironmentFile`.

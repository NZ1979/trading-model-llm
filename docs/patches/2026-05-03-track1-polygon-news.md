# Track 1 — Polygon News supplementary feed — 2026-05-03

New code, no bug fix. First major non-bug-fix change since migration to Trading.Base.1. Adds a second sentiment source running alongside the Alpaca/Benzinga + Claude Haiku pipeline.

## Why

The 2026-05-01 RCA found that for several big-mover tickers (ORCL, PM, CLX) the Benzinga news feed never delivered headlines that would have generated sentiment. Sentiment-gated signals (gap-and-go, pullback) silently no-fired even though those tickers had real news. Polygon's news endpoint covers those gaps and includes pre-scored sentiment per ticker, so it can populate the same `sentiment` SQLite table without paying for additional Claude Haiku scoring on the supplementary stream.

## What

### New file: `data/polygon_news.py` (233 lines)

`PolygonNewsPipeline` polls Polygon's `/v2/reference/news` every 5 min, maps Polygon's categorical sentiment to a conservative integer scale, and persists rows to the same `sentiment` table consumed by `latest_sentiment()`.

**Sentiment mapping** (intentionally conservative):
- `positive` → +4
- `negative` → -4
- `neutral` → 0

+4 clears the gap-and-go ±3 threshold but NOT the pullback ±5 threshold. Polygon's three-class output is less granular than Haiku's -10..+10 scale, so it's treated as moderately confident — strong enough for momentum continuation, not strong enough for mean-reversion entries that fight current price action.

**News ID dedup** (against Alpaca):
Alpaca's news_ids are positive integers. Polygon's article IDs are UUID-like strings. We hash to a negative 63-bit integer using Blake2b-8byte to avoid collision in the shared `sentiment.news_id` PRIMARY KEY column.

**Multi-ticker article handling**:
Polygon articles can contain multiple `insights[]` entries (one per ticker). The hash key is `(polygon_id, ticker)` rather than just `polygon_id`, so a single article mentioning ORCL and CRWV produces two distinct news_id rows instead of colliding on the PRIMARY KEY.

**Idempotent replay**:
`_persist` uses `INSERT OR IGNORE`, so re-fetching the same article window across poll cycles silently drops duplicates. Sandbox-tested: 5/5 cases pass including idempotent replay.

### Modified file: `main.py`

Four surgical edits, all in `TradingPlatform.boot()` and `_task_supervisor()`:

1. New import: `from data.polygon_news import PolygonNewsPipeline`
2. New attribute on `TradingPlatform`: `polygon_news_pipeline: PolygonNewsPipeline | None`
3. New task creation in `boot()` after `NewsSentimentPipeline`, named `"PolygonNewsPipeline"`
4. New restart branch in `_task_supervisor()` mirroring the existing `NewsPipeline` restart logic

### Configuration — none

`config/settings.yaml` unchanged. The new pipeline reads the existing `POLYGON_API_KEY` env var (already required for daily/PM bar backfill) and the existing `watchlist`. No new tunables.

## Sandbox tests

5/5 passed (test in `outputs/test_polygon_news.py` — not committed; ephemeral):

1. `_polygon_id_to_int` is deterministic, returns negative 63-bit ints, varies by both `(polygon_id, ticker)`.
2. `SENTIMENT_MAP` matches the expected `{positive:+4, negative:-4, neutral:0}`.
3. `_process_articles` filters insights to watchlist tickers only.
4. Multi-ticker article (ORCL + CRWV in one Polygon article) writes 3 distinct rows; idempotent replay adds 0 rows.
5. Full poll cycle with mocked aiohttp ClientSession persists row correctly to SQLite.

## Field validation (production)

Deployed 2026-05-03 02:37 UTC. After 11.5 hours of continuous operation:

- 138 polls fired at 5-minute intervals, no gaps, no errors
- Cursor advances roughly hourly (matches Polygon's documented update cadence)
- ~64 sentiment rows persisted across the period
- Multi-ticker articles confirmed working: e.g., 06:02 UTC poll fetched 7 articles → wrote 12 sentiment rows (5 articles had multiple ticker insights). Without the multi-ticker fix this would have collided to 7 rows max.
- "1 articles, 0 rows" non-burst polls confirm `INSERT OR IGNORE` dedup is working as designed (cursor article re-fetched, already in DB, dropped silently)

## Files changed (this deploy)

```
data/polygon_news.py     # NEW (233 lines)
main.py                  # MODIFIED (4 edits in 2 methods)
```

VPS sync via `paste.rs` + `curl` (OneDrive FUSE writeback was unreliable for direct scp at the time of deploy; details in session notes).

## Operational notes from this deploy

The deploy itself surfaced enough operational issues to warrant new rules in `CLAUDE_PREFLIGHT.md`:

- **Rule 16** — Always state where a command/script runs. The deploy alternated between local PowerShell and the in-browser VPS console without consistent labeling, costing several round-trips on "where do I type this?" questions.
- **Rule 17** — User cannot create PDF files. Defaulting to `.docx` for any user-authored capture.
- **Rule 18** — Fail loud, never fake. Codifies the failure-visibility approach already practiced in this codebase.
- **Rule 19** — Stop on incomplete input. The Finnhub Track 2 research initially compiled a reference from screenshots with 16 OCR-blank pages; the resulting reference doc was contaminated and had to be discarded. Rule 19 prevents repeating that pattern.

## Related work in flight

- **Track 2 (Finnhub)**: $50/mo Fundamental-1 plan subscribed. Complete API reference compiled from clean HTML/DOCX sources at `docs/finnhub_api_compiled.md`. Integration plan not yet drafted.
- **Phase B (dynamic watchlist)**: Pending. S&P 500 + NASDAQ + DJI top 500 by 30-day ADV.
- **Phase C (per-ticker PM RVOL threshold)**: Pending. Sandbox sweep planned.

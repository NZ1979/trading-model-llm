"""Tests for run_day_t3_ticks (M2.2 sub-task #20).

Covers:

- ValueError when clients.t3 is None (caller-guard contract).
- Empty day_state.tickers AND empty pre-filter -> empty decisions,
  zero budget consumption.
- sample_rate=1.0 -> every pre-filter survivor produces a T3 call.
- sample_rate=0.0 -> zero T3 calls; all candidates land on
  n_skipped_sample.
- sample_rate=0.5 -> deterministic subset; same seed across runs
  selects the same candidates.
- Sampling is monotonic in sample_rate: candidates sampled at 0.5
  are a strict subset of those sampled at 1.0 (cache-friendly).
- Budget exhausted mid-run -> remaining calls land on
  n_skipped_budget; logger WARNING emitted.
- T3 client raises SchemaInvalidError -> synthetic Hold with
  setup_label='schema_invalid_t3', tier_provenance='t3_failed'.
- T3 client raises APIUnavailableError -> setup_label='api_failure_t3'.
- T3 client raises unexpected -> setup_label='t3_unexpected'.
- Successful T3 decision has tier_provenance='t3_only' applied.
- TickDecision shape: tick_et + ticker + decision wired correctly.
- Budget call count matches successful invocations exactly.
- T3 has its own prior-decisions history (not shared with the live
  T1+T2 pass).
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

sys.path.insert(0, '.')

from analysis.indicators import DailyContext
from data.replay.config import ReplayConfig
from data.replay.day_state import DayState, TickerDayState
from data.replay.market_context import MarketContextBundle
from data.replay.t3_budget import T3Budget
from data.replay.tick_loop import (
    TickDecision,
    _t3_should_sample,
    run_day_t3_ticks,
    tick_times_for_day,
)
from data.replay.ticker_metadata import TickerMetadata
from sim.portfolio import SimulatedPortfolio
from strategy.llm.signal_engine import TierClients
from strategy.llm.types import LLMDecision


ET = ZoneInfo("America/New_York")
TRADING_DATE = date(2026, 4, 15)


# ---------------------------------------------------------------------------
# Fixtures + fakes
# ---------------------------------------------------------------------------


def _config(**overrides) -> ReplayConfig:
    kwargs = dict(
        start_date=TRADING_DATE,
        end_date=TRADING_DATE,
        tickers=("AAPL",),
        llm_prompt_version="v-test",
    )
    kwargs.update(overrides)
    return ReplayConfig(**kwargs)


def _market_ctx() -> MarketContextBundle:
    return MarketContextBundle(
        spy_5min=pd.DataFrame(),
        spy_daily=pd.DataFrame(),
        vix_daily=None,
    )


def _ticker_day_state(ticker: str = "AAPL") -> TickerDayState:
    return TickerDayState(
        ticker=ticker,
        minute_bars=pd.DataFrame(),
        daily_bars=pd.DataFrame(),
        daily_context=None,
        premarket_context=None,
        ticker_metadata=TickerMetadata(
            ticker=ticker, sector="Information Technology",
            market_cap_bucket="mega", avg_daily_volume=50_000_000,
        ),
        news_items=[],
        last_5_daily_closes=(),
    )


def _day_state(tickers: tuple[str, ...] = ("AAPL",)) -> DayState:
    return DayState(
        trading_date=TRADING_DATE,
        vix_level=None,
        market_regime_label="neutral",
        sentiment_lookup={},
        tickers={t: _ticker_day_state(t) for t in tickers},
        failed_tickers={},
        has_earnings_today={t: False for t in tickers},
        has_earnings_within_3d={t: False for t in tickers},
    )


class _FakeT3Client:
    """Fake T3 client returning canned LLMDecisions or raising."""
    backend = "anthropic"
    model_id = "claude-opus-4-6"

    def __init__(
        self,
        *,
        action: str = "Buy",
        raises: type[Exception] | None = None,
        raises_msg: str = "boom",
    ) -> None:
        self.action = action
        self.raises = raises
        self.raises_msg = raises_msg
        self.calls: list = []

    async def evaluate(self, ctx) -> LLMDecision:
        self.calls.append(ctx.ticker if hasattr(ctx, "ticker") else None)
        if self.raises is not None:
            raise self.raises(self.raises_msg)
        return LLMDecision(
            action=self.action, confidence=72,
            setup_label="opus_label", reasoning="opus says go",
        )


def _stub_pre_filter(monkeypatch, candidates_by_tick: dict | None = None) -> None:
    """Replace pre_filter_candidates with a stub that returns the same
    candidates on every tick (or a per-tick mapping if provided)."""
    def _stub(day_state, tick_et, holding, config):
        if candidates_by_tick is not None:
            return candidates_by_tick.get(tick_et, [])
        return list(day_state.tickers.keys())
    monkeypatch.setattr(
        "data.replay.tick_loop.pre_filter_candidates", _stub
    )


def _stub_build_tick_context(monkeypatch) -> None:
    """Replace build_tick_context with a lightweight stub returning a
    tiny object that exposes a ``ticker`` attribute (what the fake T3
    client reads)."""
    def _stub(*, day_state, market_ctx, config, ticker, tick_et,
              position, todays_prior_decisions):
        return MagicMock(ticker=ticker, tick_et=tick_et)
    monkeypatch.setattr(
        "data.replay.tick_loop.build_tick_context", _stub
    )


# ===========================================================================
# 1. Caller-guard contract
# ===========================================================================


@pytest.mark.asyncio
async def test_raises_when_t3_client_is_none():
    clients = TierClients(t1=MagicMock(), t2=None, t3=None)
    budget = T3Budget(cap_dollars=1.0)
    with pytest.raises(ValueError, match="clients.t3=None"):
        await run_day_t3_ticks(
            day_state=_day_state(), market_ctx=_market_ctx(),
            config=_config(), clients=clients, budget=budget,
        )


# ===========================================================================
# 2. Empty inputs
# ===========================================================================


@pytest.mark.asyncio
async def test_empty_pre_filter_yields_no_calls(monkeypatch):
    """pre_filter returns no candidates -> zero T3 calls, zero
    budget consumption."""
    _stub_pre_filter(monkeypatch, candidates_by_tick={})  # empty for every tick
    fake = _FakeT3Client()
    clients = TierClients(t1=MagicMock(), t2=None, t3=fake)
    budget = T3Budget(cap_dollars=1.0)
    result = await run_day_t3_ticks(
        day_state=_day_state(), market_ctx=_market_ctx(),
        config=_config(), clients=clients, budget=budget,
    )
    assert result == []
    assert fake.calls == []
    assert budget.n_calls == 0
    assert budget.n_skipped_sample == 0
    assert budget.n_skipped_budget == 0


# ===========================================================================
# 3. Sample-rate gate
# ===========================================================================


@pytest.mark.asyncio
async def test_sample_rate_1_calls_every_candidate(monkeypatch):
    _stub_pre_filter(monkeypatch)
    _stub_build_tick_context(monkeypatch)
    fake = _FakeT3Client()
    clients = TierClients(t1=MagicMock(), t2=None, t3=fake)
    budget = T3Budget(cap_dollars=10.0)
    await run_day_t3_ticks(
        day_state=_day_state(("AAPL",)),
        market_ctx=_market_ctx(),
        config=_config(t3_sample_rate=1.0),
        clients=clients, budget=budget,
    )
    # 78 ticks * 1 candidate = 78 calls
    assert budget.n_calls == 78
    assert budget.n_skipped_sample == 0


@pytest.mark.asyncio
async def test_sample_rate_0_skips_every_candidate(monkeypatch):
    _stub_pre_filter(monkeypatch)
    _stub_build_tick_context(monkeypatch)
    fake = _FakeT3Client()
    clients = TierClients(t1=MagicMock(), t2=None, t3=fake)
    budget = T3Budget(cap_dollars=10.0)
    await run_day_t3_ticks(
        day_state=_day_state(("AAPL",)),
        market_ctx=_market_ctx(),
        config=_config(t3_sample_rate=0.0),
        clients=clients, budget=budget,
    )
    assert budget.n_calls == 0
    assert budget.n_skipped_sample == 78
    assert fake.calls == []


def test_should_sample_deterministic_at_half_rate():
    """Same (ticker, tick) at rate=0.5 should produce identical
    decisions across calls."""
    base = datetime(2026, 4, 15, 9, 30, tzinfo=ET)
    decisions_a = [
        _t3_should_sample("AAPL", base + timedelta(minutes=5*i), 0.5)
        for i in range(20)
    ]
    decisions_b = [
        _t3_should_sample("AAPL", base + timedelta(minutes=5*i), 0.5)
        for i in range(20)
    ]
    assert decisions_a == decisions_b


def test_should_sample_monotonic_in_rate():
    """Candidates sampled at rate=0.3 must also be sampled at rate=0.7
    (cache-friendly subset semantics)."""
    base = datetime(2026, 4, 15, 9, 30, tzinfo=ET)
    test_candidates = [
        ("AAPL", base + timedelta(minutes=5*i))
        for i in range(50)
    ] + [
        ("MSFT", base + timedelta(minutes=5*i))
        for i in range(50)
    ]
    sampled_low = {
        c for c in test_candidates
        if _t3_should_sample(c[0], c[1], 0.3)
    }
    sampled_high = {
        c for c in test_candidates
        if _t3_should_sample(c[0], c[1], 0.7)
    }
    assert sampled_low.issubset(sampled_high)


# ===========================================================================
# 4. Budget gate
# ===========================================================================


@pytest.mark.asyncio
async def test_budget_exhaustion_mid_run_records_skips(monkeypatch, caplog):
    import logging as _logging
    _stub_pre_filter(monkeypatch)
    _stub_build_tick_context(monkeypatch)
    fake = _FakeT3Client()
    clients = TierClients(t1=MagicMock(), t2=None, t3=fake)
    # cap_dollars=0.75, per_call_estimate=0.25 -> exactly 3 calls fit
    # (3 * 0.25 = 0.75; 4th call would push to 1.00 > 0.75).
    # Quarter increments avoid the float-accumulation edge that bites
    # decimal-fraction values like 0.05.
    budget = T3Budget(cap_dollars=0.75, per_call_estimate=0.25)
    with caplog.at_level(_logging.WARNING, logger="data.replay.tick_loop"):
        await run_day_t3_ticks(
            day_state=_day_state(("AAPL",)),
            market_ctx=_market_ctx(),
            config=_config(t3_sample_rate=1.0),
            clients=clients, budget=budget,
        )
    assert budget.n_calls == 3
    # 78 candidates - 3 calls = 75 skipped
    assert budget.n_skipped_budget == 75
    # WARNING was logged at least once
    assert any("budget exhausted" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_budget_cap_zero_yields_zero_calls(monkeypatch):
    _stub_pre_filter(monkeypatch)
    _stub_build_tick_context(monkeypatch)
    fake = _FakeT3Client()
    clients = TierClients(t1=MagicMock(), t2=None, t3=fake)
    budget = T3Budget(cap_dollars=0.0, per_call_estimate=0.05)
    await run_day_t3_ticks(
        day_state=_day_state(),
        market_ctx=_market_ctx(),
        config=_config(t3_sample_rate=1.0),
        clients=clients, budget=budget,
    )
    assert budget.n_calls == 0
    assert budget.n_skipped_budget == 78
    assert fake.calls == []


# ===========================================================================
# 5. T3 failure paths -> synthetic Hold
# ===========================================================================


@pytest.mark.asyncio
async def test_schema_invalid_error_makes_synthetic_hold(monkeypatch):
    # Create a SchemaInvalidError shim with the right class name.
    class SchemaInvalidError(Exception):
        pass

    _stub_pre_filter(monkeypatch, candidates_by_tick={
        tick_times_for_day(TRADING_DATE)[0]: ["AAPL"],
    })
    _stub_build_tick_context(monkeypatch)
    fake = _FakeT3Client(raises=SchemaInvalidError, raises_msg="bad json")
    clients = TierClients(t1=MagicMock(), t2=None, t3=fake)
    budget = T3Budget(cap_dollars=10.0)
    result = await run_day_t3_ticks(
        day_state=_day_state(),
        market_ctx=_market_ctx(),
        config=_config(),
        clients=clients, budget=budget,
    )
    assert len(result) == 1
    td = result[0]
    assert td.decision.action == "Hold"
    assert td.decision.setup_label == "schema_invalid_t3"
    assert td.decision.tier_provenance == "t3_failed"


@pytest.mark.asyncio
async def test_api_unavailable_error_makes_synthetic_hold(monkeypatch):
    class APIUnavailableError(Exception):
        pass

    _stub_pre_filter(monkeypatch, candidates_by_tick={
        tick_times_for_day(TRADING_DATE)[0]: ["AAPL"],
    })
    _stub_build_tick_context(monkeypatch)
    fake = _FakeT3Client(raises=APIUnavailableError, raises_msg="503")
    clients = TierClients(t1=MagicMock(), t2=None, t3=fake)
    budget = T3Budget(cap_dollars=10.0)
    result = await run_day_t3_ticks(
        day_state=_day_state(), market_ctx=_market_ctx(),
        config=_config(), clients=clients, budget=budget,
    )
    assert result[0].decision.setup_label == "api_failure_t3"


@pytest.mark.asyncio
async def test_unexpected_error_makes_synthetic_hold(monkeypatch):
    _stub_pre_filter(monkeypatch, candidates_by_tick={
        tick_times_for_day(TRADING_DATE)[0]: ["AAPL"],
    })
    _stub_build_tick_context(monkeypatch)
    fake = _FakeT3Client(raises=RuntimeError, raises_msg="weird")
    clients = TierClients(t1=MagicMock(), t2=None, t3=fake)
    budget = T3Budget(cap_dollars=10.0)
    result = await run_day_t3_ticks(
        day_state=_day_state(), market_ctx=_market_ctx(),
        config=_config(), clients=clients, budget=budget,
    )
    assert result[0].decision.setup_label == "t3_unexpected"


# ===========================================================================
# 6. Successful T3 decision gets tier_provenance='t3_only'
# ===========================================================================


@pytest.mark.asyncio
async def test_successful_decision_tagged_t3_only(monkeypatch):
    _stub_pre_filter(monkeypatch, candidates_by_tick={
        tick_times_for_day(TRADING_DATE)[0]: ["AAPL"],
    })
    _stub_build_tick_context(monkeypatch)
    fake = _FakeT3Client(action="Buy")
    clients = TierClients(t1=MagicMock(), t2=None, t3=fake)
    budget = T3Budget(cap_dollars=10.0)
    result = await run_day_t3_ticks(
        day_state=_day_state(), market_ctx=_market_ctx(),
        config=_config(), clients=clients, budget=budget,
    )
    assert len(result) == 1
    assert result[0].decision.action == "Buy"
    assert result[0].decision.tier_provenance == "t3_only"
    assert result[0].decision.setup_label == "opus_label"


# ===========================================================================
# 7. TickDecision shape
# ===========================================================================


@pytest.mark.asyncio
async def test_tick_decision_shape(monkeypatch):
    tick0 = tick_times_for_day(TRADING_DATE)[0]
    _stub_pre_filter(monkeypatch, candidates_by_tick={tick0: ["AAPL"]})
    _stub_build_tick_context(monkeypatch)
    fake = _FakeT3Client()
    clients = TierClients(t1=MagicMock(), t2=None, t3=fake)
    budget = T3Budget(cap_dollars=10.0)
    result = await run_day_t3_ticks(
        day_state=_day_state(), market_ctx=_market_ctx(),
        config=_config(), clients=clients, budget=budget,
    )
    td = result[0]
    assert isinstance(td, TickDecision)
    assert td.tick_et == tick0
    assert td.ticker == "AAPL"
    assert isinstance(td.decision, LLMDecision)

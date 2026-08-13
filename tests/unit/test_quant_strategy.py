from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pa_agent.data.base import KlineBar
from pa_agent.trading.quant import (
    Hs300DailyPullbackStrategy,
    SignalStatus,
    StrategyContext,
)


def _bars(count: int = 80) -> tuple[KlineBar, ...]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    values: list[KlineBar] = []
    for index in range(count):
        close = 100 + index * 0.4
        values.append(KlineBar(
            seq=count - index,
            ts_open=(start + timedelta(days=index)).timestamp() * 1000,
            open=close - 0.2,
            high=close + 0.8,
            low=close - 0.8,
            close=close,
            volume=1_000_000,
            amount=close * 1_000_000,
            closed=True,
        ))
    return tuple(values)


def test_strategy_is_deterministic_and_rejects_incomplete_market_context() -> None:
    context = StrategyContext(
        symbol="600519",
        bars=_bars(),
        index_bars=_bars(),
        market_breadth_pct=40,
        pool_version="hs300-2026-08",
        signal_time="2026-08-12T15:00:00+08:00",
    )
    strategy = Hs300DailyPullbackStrategy()
    first = strategy.evaluate(context)
    second = strategy.evaluate(context)
    assert first == second
    assert first.status is SignalStatus.REJECT
    assert "market_breadth_ok" in first.reasons
    assert "trade_confidence" not in first.model_dump_json()
    assert "estimated_win_rate" not in first.model_dump_json()
    market = first.condition_snapshot["market_index"]
    assert market["symbol"] == "000300"
    assert market["close"] == context.index_bars[-1].close
    assert market["ma20_change_5"] > 0


def test_ineligible_instrument_is_fail_closed() -> None:
    context = StrategyContext(
        symbol="600519",
        bars=_bars(),
        index_bars=_bars(),
        market_breadth_pct=80,
        pool_version="hs300-2026-08",
        signal_time="2026-08-12T15:00:00+08:00",
        eligible=False,
        eligibility_reasons=("st_or_suspended",),
    )
    result = Hs300DailyPullbackStrategy().evaluate(context)
    assert result.status is SignalStatus.REJECT
    assert "st_or_suspended" in result.reasons

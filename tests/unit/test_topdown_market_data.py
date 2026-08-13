from datetime import UTC, datetime, timedelta, timezone

import pytest

from pa_agent.trading.broker_models import (
    BrokerConnectionStatus,
    BrokerQuote,
    BrokerSnapshot,
    ConnectionState,
)
from pa_agent.trading.quant import SignalDecision, SignalStatus
from pa_agent.trading.topdown import TopDownScoreStatus, TopDownScoring
from pa_agent.trading.topdown_market_data import TopDownMarketDataService

NOW = datetime(2026, 8, 12, 10, 0, tzinfo=timezone(timedelta(hours=8)))


def _rows(count: int, *, minutes: int = 0) -> list[dict]:
    result = []
    for index in range(count):
        when = NOW - timedelta(minutes=minutes * (count - 1 - index), days=(count - 1 - index if not minutes else 0))
        price = 100 + index * 0.1
        result.append({
            "time": when.replace(tzinfo=None), "open": price - 0.1, "high": price + 0.2,
            "low": price - 0.2, "close": price, "volume": 1000 + index,
        })
    return result


def _signal() -> SignalDecision:
    return SignalDecision(
        status=SignalStatus.ALLOW,
        strategy_id="hs300_daily_pullback_v1",
        parameter_version="1.0.0",
        pool_version="hs300-2026-08",
        symbol="600519",
        signal_time="daily-signal-1",
        trigger_price=101,
        max_entry_price=110,
        initial_stop=95,
        condition_snapshot={"stop_distance_atr": 1.5},
    )


def test_market_data_service_uses_closed_bars_and_fails_closed_for_missing_sentiment_theme() -> None:
    service = TopDownMarketDataService(
        TopDownScoring(),
        fetch_index_daily_fn=lambda *_args, **_kwargs: _rows(70),
        fetch_minute_fn=lambda *_args, **_kwargs: _rows(25, minutes=15),
    )
    broker = BrokerSnapshot(
        connection=ConnectionState(
            status=BrokerConnectionStatus.CONNECTED, checked_at=NOW.isoformat(),
        ),
        quote=BrokerQuote(
            symbol="600519", name="贵州茅台", last_price=102,
            captured_at=NOW.isoformat(),
        ),
        captured_at=NOW.isoformat(), complete=True,
    )

    result = service.build_context(
        symbol="600519", daily_signal=_signal(),
        pool_snapshot={"version": "hs300-2026-08"}, broker=broker,
        hotspot=None, captured_at=NOW,
    )
    score = service.scoring.evaluate(result.context)

    assert len(result.context.indexes) == 4
    assert datetime.fromisoformat(result.context.bar_closed_at).astimezone(UTC) == NOW
    assert result.context.sentiment is None
    assert result.context.theme is None
    assert result.closed_stock_bar is not None
    assert result.closed_stock_bar.open == pytest.approx(102.3)
    assert result.closed_stock_bar.close == pytest.approx(102.4)
    assert result.closed_stock_bar.closed
    assert score.status is TopDownScoreStatus.DATA_INCOMPLETE
    assert any("missing_trusted_sentiment_snapshot" in item for item in result.data_gaps)

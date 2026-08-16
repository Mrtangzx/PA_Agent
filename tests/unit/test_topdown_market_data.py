from datetime import UTC, datetime, timedelta, timezone

import pytest

from pa_agent.trading.broker_models import (
    BrokerConnectionStatus,
    BrokerQuote,
    BrokerSnapshot,
    ConnectionState,
)
from pa_agent.trading.hotspots import HOTSPOT_RULE_VERSION
from pa_agent.trading.quant import SignalDecision, SignalStatus
from pa_agent.trading.topdown import (
    HotspotSnapshot,
    SentimentScoreInput,
    TopDownScoreStatus,
    TopDownScoring,
)
from pa_agent.trading.topdown_market_data import (
    TopDownMarketDataService,
    expected_oos_market_close,
    expected_topdown_bar_close,
)

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


def _sentiment() -> SentimentScoreInput:
    return SentimentScoreInput(
        advancing_pct=65,
        hs300_above_ma20_pct=70,
        limit_up_count=40,
        limit_down_count=2,
        seal_success_pct=85,
        blast_board_pct=12,
        new_high_count=120,
        new_low_count=20,
        turnover_vs_ma20=1.1,
        broad_index_positive=True,
        captured_at=NOW.isoformat(),
    )


def _hotspot(*, rule_version: str = HOTSPOT_RULE_VERSION) -> HotspotSnapshot:
    return HotspotSnapshot(
        symbol="600519",
        captured_at=NOW.isoformat(),
        frozen_at=NOW.isoformat(),
        industries=["白酒"],
        board_strength={"market_verified": True},
        rule_version=rule_version,
    ).with_source_hash()


THEME_METRICS = {
    "relative_strength_percentile": 90,
    "advancing_pct": 80,
    "main_net_inflow_pct": 2.5,
    "turnover_vs_recent": 1.4,
    "persistence_days": 4,
}


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


@pytest.mark.parametrize(
    ("clock", "expected"),
    [
        ("2026-08-12T09:30:01+08:00", None),
        ("2026-08-12T09:45:01+08:00", "2026-08-12T09:45:00+08:00"),
        ("2026-08-12T10:00:01+08:00", "2026-08-12T10:00:00+08:00"),
        ("2026-08-12T11:30:04+08:00", "2026-08-12T11:30:00+08:00"),
        ("2026-08-12T13:00:01+08:00", None),
        ("2026-08-12T13:15:01+08:00", "2026-08-12T13:15:00+08:00"),
        ("2026-08-12T14:15:05+08:00", "2026-08-12T14:15:00+08:00"),
        ("2026-08-12T14:20:00+08:00", None),
        ("2026-08-15T10:00:01+08:00", None),
    ],
)
def test_live_scoring_schedule_records_first_bar_but_two_bars_are_still_required(
    clock: str, expected: str | None
) -> None:
    result = expected_topdown_bar_close(datetime.fromisoformat(clock))
    assert (result.isoformat() if result else None) == expected


@pytest.mark.parametrize(
    ("clock", "expected"),
    [
        ("2026-08-12T15:04:59+08:00", "2026-08-12T15:00:00+08:00"),
        ("2026-08-12T15:05:00+08:00", "2026-08-12T15:00:00+08:00"),
        ("2026-08-12T15:14:59+08:00", "2026-08-12T15:00:00+08:00"),
        ("2026-08-12T15:15:00+08:00", None),
        ("2026-08-15T15:10:00+08:00", None),
    ],
)
def test_oos_schedule_has_daily_only_recovery_until_15_minutes(
    clock: str,
    expected: str | None,
) -> None:
    result = expected_oos_market_close(datetime.fromisoformat(clock))
    assert (result.isoformat() if result else None) == expected


def test_misaligned_component_bar_fails_closed_but_keeps_stock_bar_for_exit_management() -> None:
    def minute_rows(symbol, *_args, **_kwargs):
        rows = _rows(25, minutes=15)
        if symbol == "399006":
            return rows[:-1]
        return rows

    service = TopDownMarketDataService(
        TopDownScoring(),
        fetch_index_daily_fn=lambda *_args, **_kwargs: _rows(70),
        fetch_minute_fn=minute_rows,
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
        hotspot=_hotspot(), sentiment=_sentiment(), theme_metrics=THEME_METRICS,
        captured_at=NOW,
    )

    assert any("bar_time_mismatch:index_399006" in gap for gap in result.data_gaps)
    # The combined entry score is incomplete, while the independently aligned
    # stock bar remains usable for stop/trailing/time-exit management.
    assert result.closed_stock_bar is not None
    assert service.scoring.evaluate(result.context).total_score is not None
    score = service.evaluate(result)
    assert score.status is TopDownScoreStatus.DATA_INCOMPLETE
    assert score.total_score is None
    assert score.consecutive_pass_count == 0


def test_hotspot_rule_version_mismatch_fails_closed() -> None:
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
        symbol="600519",
        daily_signal=_signal(),
        pool_snapshot={"version": "hs300-2026-08"},
        broker=broker,
        hotspot=_hotspot(rule_version="obsolete_hotspot_rule"),
        sentiment=_sentiment(),
        theme_metrics=THEME_METRICS,
        captured_at=NOW,
    )
    score = service.evaluate(result)

    assert any("hotspot_rule_version_mismatch" in gap for gap in score.data_gaps)
    assert score.status is TopDownScoreStatus.DATA_INCOMPLETE
    assert score.total_score is None

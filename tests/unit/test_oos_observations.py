from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pa_agent.data.base import KlineBar
from pa_agent.trading.market_sentiment import MarketSentimentSnapshot
from pa_agent.trading.oos_observations import (
    OosMarketObservationService,
    OosObservationRecorder,
    pool_monitor_strategy_version,
)
from pa_agent.trading.store import TradeStore
from pa_agent.trading.topdown import TOPDOWN_STRATEGY_ID, SentimentScoreInput
from pa_agent.trading.universe import (
    CLOUD_AI_AUTHORIZATION_SYMBOLS,
    CLOUD_AI_SYMBOLS,
)

TZ8 = timezone(timedelta(hours=8))


def test_store_oos_observation_is_idempotent_and_rejects_future_source(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    values = dict(
        strategy_version=TOPDOWN_STRATEGY_ID,
        kind="intraday_15m",
        symbol="688158",
        effective_at="2026-08-14T10:00:00+08:00",
        source_published_at="2026-08-14T10:00:00+08:00",
        source_kind="eastmoney_market",
        source_url="https://push2his.eastmoney.com/",
        payload={"close": 10.0},
    )
    first = store.add_oos_observation(**values)
    second = store.add_oos_observation(**values)
    assert first == second
    assert store.oos_observation_coverage(strategy_version=TOPDOWN_STRATEGY_ID)[
        "intraday_15m"
    ]["record_count"] == 1

    with pytest.raises(ValueError, match="future"):
        store.add_oos_observation(**{
            **values,
            "source_published_at": "2026-08-14T10:01:00+08:00",
        })


def test_store_freezes_first_payload_for_same_symbol_and_effective_time(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    base = dict(
        strategy_version=TOPDOWN_STRATEGY_ID,
        kind="intraday_15m",
        symbol="688158",
        effective_at="2026-08-14T09:45:00+08:00",
        source_published_at="2026-08-14T09:45:00+08:00",
        source_kind="eastmoney_market",
        source_url="https://push2his.eastmoney.com/",
    )
    first = store.add_oos_observation(**base, payload={"close": 10.0})
    retry = store.add_oos_observation(**base, payload={"close": 99.0})

    rows = store.list_oos_observations(
        strategy_version=TOPDOWN_STRATEGY_ID,
        kind="intraday_15m",
        symbol="688158",
    )
    assert retry == first
    assert len(rows) == 1
    assert rows[0]["payload"]["close"] == 10.0


def test_recorder_keeps_only_post_freeze_closed_intraday_bars(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    recorder = OosObservationRecorder(store)
    at = datetime(2026, 8, 17, 10, 0, tzinfo=TZ8)
    bar = KlineBar(
        seq=1, ts_open=at.timestamp() * 1000, open=10, high=11, low=9,
        close=10.5, volume=100, amount=1_000, closed=True,
    )
    assert recorder.record_intraday_bar("688158", bar)
    assert recorder.record_intraday_bar("688158", bar)
    rows = store.list_oos_observations(
        strategy_version=TOPDOWN_STRATEGY_ID, kind="intraday_15m"
    )
    assert len(rows) == 1
    assert rows[0]["payload"]["effective_at"] == at.isoformat()

    forming = KlineBar(**{**bar.__dict__, "closed": False})
    assert recorder.record_intraday_bar("688158", forming) is None
    assert recorder.record_intraday_bar("600519", bar) is None


def test_recorder_accepts_only_post_freeze_exact_fixed_universe(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    recorder = OosObservationRecorder(store)
    snapshot = {
        "as_of": "2026-08-15",
        "source_as_of": "2026-08-14",
        "symbols": list(CLOUD_AI_SYMBOLS),
    }
    assert recorder.record_universe(snapshot)
    assert recorder.record_universe(snapshot)
    assert len(store.list_oos_observations(
        strategy_version=TOPDOWN_STRATEGY_ID,
        kind="historical_constituents",
    )) == 1

    assert recorder.record_universe({**snapshot, "symbols": ["600519"]}) is None

    definition = store.list_oos_observations(
        strategy_version=TOPDOWN_STRATEGY_ID,
        kind="historical_constituents",
    )[0]
    assert definition["effective_at"] == "2026-08-14T15:55:01+08:00"
    assert store.oos_observation_coverage(strategy_version=TOPDOWN_STRATEGY_ID)[
        "historical_constituents"
    ]["symbols"] == len(CLOUD_AI_SYMBOLS)


def test_market_service_collects_full_fixed_pool_and_indexes_without_signals(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    recorder = OosObservationRecorder(store)
    now = datetime(2026, 8, 17, 10, 0, 1, tzinfo=TZ8)
    close = now.replace(second=0)

    def minute_loader(symbol, **_kwargs):
        return [{
            "time": close, "open": 10, "high": 11, "low": 9,
            "close": 10.5, "volume": 100, "amount": 1_000,
        }]

    def daily_loader(_symbol, **_kwargs):
        return [{
            "time": close - timedelta(days=1),
            "open": 10, "high": 10, "low": 10, "close": 10,
            "volume": 100, "amount": 1_000,
        }]

    service = OosMarketObservationService(
        recorder,
        minute_loader=minute_loader,
        daily_loader=daily_loader,
        index_daily_loader=lambda *_args, **_kwargs: [],
        profile_loader=lambda symbol: {
            "symbol": symbol, "name": symbol, "listing_date": "20200101",
        },
        clock=lambda: now,
    )
    report = service.capture(captured_at=now)

    assert report["status"] == "complete"
    assert report["captured"] == len(CLOUD_AI_AUTHORIZATION_SYMBOLS) + 4
    coverage = store.oos_observation_coverage(strategy_version=TOPDOWN_STRATEGY_ID)
    assert coverage["intraday_15m"]["record_count"] == len(
        CLOUD_AI_AUTHORIZATION_SYMBOLS
    ) + 4
    assert "839494" not in {
        row["symbol"]
        for row in store.list_oos_observations(
            strategy_version=TOPDOWN_STRATEGY_ID,
            kind="intraday_15m",
        )
    }
    stock_row = next(
        row for row in store.list_oos_observations(
            strategy_version=TOPDOWN_STRATEGY_ID,
            kind="intraday_15m",
        )
        if row["symbol"] in CLOUD_AI_AUTHORIZATION_SYMBOLS
    )
    assert stock_row["payload"]["suspended"] is False
    assert stock_row["payload"]["limit_locked"] is False


def test_market_service_monitors_dynamic_pool_without_polluting_frozen_oos(
    tmp_path,
) -> None:
    store = TradeStore(tmp_path / "trades.db")
    recorder = OosObservationRecorder(store)
    now = datetime(2026, 8, 17, 10, 0, 1, tzinfo=TZ8)
    close = now.replace(second=0)

    def minute_loader(_symbol, **_kwargs):
        return [{
            "time": close, "open": 10, "high": 11, "low": 9,
            "close": 10.5, "volume": 100, "amount": 1_000,
        }]

    def daily_loader(_symbol, **_kwargs):
        return [{
            "time": close - timedelta(days=1),
            "open": 10, "high": 10, "low": 10, "close": 10,
            "volume": 100, "amount": 1_000,
        }]

    service = OosMarketObservationService(
        recorder,
        minute_loader=minute_loader,
        daily_loader=daily_loader,
        index_daily_loader=lambda *_args, **_kwargs: [],
        profile_loader=lambda symbol: {
            "symbol": symbol, "name": symbol, "listing_date": "20200101",
        },
        clock=lambda: now,
    )
    pool_version = "ashare_private_pool-20260817-001"
    report = service.capture(
        captured_at=now,
        monitor_universe={
            "version": pool_version,
            "monitor_symbols": ["600519"],
        },
    )

    assert report["status"] == "complete"
    assert report["captured"] == len(CLOUD_AI_AUTHORIZATION_SYMBOLS) + 4
    assert report["monitor_status"] == "complete"
    assert report["monitor_captured"] == 1
    assert report["monitor_required"] == 1
    assert not any(
        row["symbol"] == "600519"
        for row in store.list_oos_observations(
            strategy_version=TOPDOWN_STRATEGY_ID,
            kind="intraday_15m",
        )
    )
    monitor_rows = store.list_oos_observations(
        strategy_version=pool_monitor_strategy_version(pool_version),
        kind="intraday_15m",
    )
    assert len(monitor_rows) == 1
    assert monitor_rows[0]["symbol"] == "600519"
    assert monitor_rows[0]["payload"]["pool_version"] == pool_version


def test_market_service_reports_partial_slot_and_required_count(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    recorder = OosObservationRecorder(store)
    now = datetime(2026, 8, 17, 9, 45, 1, tzinfo=TZ8)
    close = now.replace(second=0)

    def minute_loader(symbol, **_kwargs):
        if symbol == "399006":
            return []
        return [{
            "time": close, "open": 10, "high": 11, "low": 9,
            "close": 10.5, "volume": 100, "amount": 1_000,
        }]

    def daily_loader(_symbol, **_kwargs):
        return [{
            "time": close - timedelta(days=1),
            "open": 10, "high": 10, "low": 10, "close": 10,
            "volume": 100, "amount": 1_000,
        }]

    report = OosMarketObservationService(
        recorder,
        minute_loader=minute_loader,
        daily_loader=daily_loader,
        index_daily_loader=lambda *_args, **_kwargs: [],
        profile_loader=lambda symbol: {
            "symbol": symbol, "name": symbol, "listing_date": "20200101",
        },
        clock=lambda: now,
    ).capture(captured_at=now)

    assert report["status"] == "data_incomplete"
    assert report["required"] == len(CLOUD_AI_AUTHORIZATION_SYMBOLS) + 4
    assert report["captured"] == report["required"] - 1
    assert "intraday_399006_expected_bar_missing" in report["failures"]


def test_market_service_persists_daily_eligibility_metadata_at_close(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    recorder = OosObservationRecorder(store)
    now = datetime(2026, 8, 17, 15, 0, 1, tzinfo=TZ8)
    close = now.replace(second=0)
    previous = close - timedelta(days=1)

    def minute_loader(_symbol, **_kwargs):
        return [{
            "time": close, "open": 10, "high": 10.5, "low": 9.8,
            "close": 10.2, "volume": 100, "amount": 1_000,
        }]

    def daily_loader(_symbol, **_kwargs):
        return [
            {
                "time": previous,
                "open": 10, "high": 10, "low": 10, "close": 10,
                "volume": 100, "amount": 1_000,
            },
            {
                "time": close,
                "open": 10, "high": 10.5, "low": 9.8, "close": 10.2,
                "volume": 100, "amount": 1_000,
            },
        ]

    report = OosMarketObservationService(
        recorder,
        minute_loader=minute_loader,
        daily_loader=daily_loader,
        index_daily_loader=lambda _symbol, **_kwargs: [{
            "time": close, "open": 10, "high": 10.5, "low": 9.8,
            "close": 10.2, "volume": 100, "amount": 1_000,
        }],
        profile_loader=lambda symbol: {
            "symbol": symbol,
            "name": symbol,
            "industry": "vendor industry",
            "listing_date": "20200101",
        },
        clock=lambda: now,
    ).capture(captured_at=now)

    assert report["status"] == "complete"
    daily = store.list_oos_observations(
        strategy_version=TOPDOWN_STRATEGY_ID,
        kind="daily_bars",
    )
    stock_row = next(
        row for row in daily if row["symbol"] in CLOUD_AI_AUTHORIZATION_SYMBOLS
    )
    assert stock_row["payload"]["suspended"] is False
    assert stock_row["payload"]["limit_locked"] is False
    assert stock_row["payload"]["is_st"] is False
    assert stock_row["payload"]["delisting"] is False
    assert stock_row["payload"]["listed_days"] > 120
    assert stock_row["payload"]["industry"] == "云算力主题"


def test_market_service_recovers_daily_only_until_fifteen_minutes_after_close(
    tmp_path,
) -> None:
    store = TradeStore(tmp_path / "trades.db")
    recorder = OosObservationRecorder(store)
    now = datetime(2026, 8, 17, 15, 10, tzinfo=TZ8)
    close = now.replace(minute=0, second=0)
    previous = close - timedelta(days=1)
    minute_calls: list[str] = []

    def minute_loader(symbol, **_kwargs):
        minute_calls.append(symbol)
        raise AssertionError("late daily recovery must not fetch intraday bars")

    def daily_loader(_symbol, **_kwargs):
        return [
            {
                "time": previous,
                "open": 10,
                "high": 10,
                "low": 10,
                "close": 10,
                "volume": 100,
                "amount": 1_000,
            },
            {
                "time": close,
                "open": 10,
                "high": 10.5,
                "low": 9.8,
                "close": 10.2,
                "volume": 100,
                "amount": 1_000,
            },
        ]

    service = OosMarketObservationService(
        recorder,
        minute_loader=minute_loader,
        daily_loader=daily_loader,
        index_daily_loader=lambda _symbol, **_kwargs: [{
            "time": close,
            "open": 10,
            "high": 10.5,
            "low": 9.8,
            "close": 10.2,
            "volume": 100,
            "amount": 1_000,
        }],
        profile_loader=lambda symbol: {
            "symbol": symbol,
            "name": symbol,
            "industry": "fixture",
            "listing_date": "20200101",
        },
        clock=lambda: now,
    )

    report = service.capture(
        captured_at=now,
        monitor_universe={
            "version": "ashare_private_pool-fixture",
            "monitor_symbols": ["600519"],
        },
    )

    expected_count = len(CLOUD_AI_AUTHORIZATION_SYMBOLS) + len(service.INDEXES)
    assert report["status"] == "complete"
    assert report["daily_recovery_only"] is True
    assert report["captured"] == expected_count
    assert report["required"] == expected_count
    assert report["monitor_status"] == "not_requested"
    assert minute_calls == []
    assert len(store.list_oos_observations(
        strategy_version=TOPDOWN_STRATEGY_ID,
        kind="daily_bars",
        limit=1000,
    )) == expected_count
    assert store.list_oos_observations(
        strategy_version=TOPDOWN_STRATEGY_ID,
        kind="intraday_15m",
        limit=1000,
    ) == []


def test_daily_recovery_window_classifies_loader_failure_as_daily(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    recorder = OosObservationRecorder(store)
    now = datetime(2026, 8, 17, 15, 10, tzinfo=TZ8)

    def daily_loader(*_args, **_kwargs):
        raise RuntimeError("fixture daily failure")

    service = OosMarketObservationService(
        recorder,
        minute_loader=lambda *_args, **_kwargs: pytest.fail(
            "late daily recovery must not fetch intraday bars"
        ),
        daily_loader=daily_loader,
        index_daily_loader=lambda *_args, **_kwargs: [],
        profile_loader=lambda symbol: {
            "symbol": symbol,
            "name": symbol,
            "industry": "fixture",
            "listing_date": "20200101",
        },
        clock=lambda: now,
    )

    report = service.capture(captured_at=now)

    assert report["daily_recovery_only"] is True
    assert any(item.startswith("daily_") for item in report["failures"])
    assert not any(item.startswith("intraday_") for item in report["failures"])


def test_hotspot_observation_persists_deterministic_theme_metrics(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    recorder = OosObservationRecorder(store)
    frozen = "2026-08-17T10:00:00+08:00"
    snapshot = {
        "symbol": "688158",
        "captured_at": frozen,
        "frozen_at": frozen,
        "industries": ["云计算"],
        "concepts": [],
        "items": [],
        "board_strength": {"flows": [{
            "pct_chg": 1.2,
            "main_net_pct": 2.3,
            "advancing_pct": 70,
            "turnover_vs_recent": 1.4,
            "persistence_days": 3,
            "relative_strength_percentile": 85,
        }]},
        "positive_score": 0,
        "negative_blocks": [],
        "data_gaps": [],
        "rule_version": "hotspot_time_window_v2",
        "effective_windows_days": {"announcement": 30, "news": 3},
    }

    assert recorder.record_hotspot(snapshot)
    row = store.list_oos_observations(
        strategy_version=TOPDOWN_STRATEGY_ID, kind="hotspots"
    )[0]

    assert row["payload"]["theme_metrics"] == {
        "relative_strength_percentile": 85.0,
        "advancing_pct": 70.0,
        "main_net_inflow_pct": 2.3,
        "turnover_vs_recent": 1.4,
        "persistence_days": 3,
        "positive_board_share": 100.0,
    }
    assert row["payload"]["observed_at"] == frozen


def test_dynamic_pool_hotspot_does_not_pollute_frozen_cloud_oos(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    recorder = OosObservationRecorder(store)
    frozen = "2026-08-17T10:00:00+08:00"
    snapshot = {
        "symbol": "600519",
        "captured_at": frozen,
        "frozen_at": frozen,
        "industries": ["白酒"],
        "concepts": [],
        "items": [],
        "board_strength": {"flows": [{
            "pct_chg": 1.2,
            "main_net_pct": 2.3,
            "advancing_pct": 70,
            "turnover_vs_recent": 1.4,
            "persistence_days": 3,
            "relative_strength_percentile": 85,
        }]},
        "rule_version": "hotspot_time_window_v2",
    }

    assert recorder.record_hotspot(snapshot) is None
    assert store.list_oos_observations(
        strategy_version=TOPDOWN_STRATEGY_ID,
        kind="hotspots",
        symbol="600519",
    ) == []


def test_sentiment_oos_recorder_rejects_observation_after_five_minutes(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    recorder = OosObservationRecorder(store)
    slot = datetime(2026, 8, 17, 10, 0, tzinfo=TZ8)
    input_value = SentimentScoreInput(
        advancing_pct=60, hs300_above_ma20_pct=60,
        limit_up_count=20, limit_down_count=2,
        seal_success_pct=80, blast_board_pct=10,
        new_high_count=100, new_low_count=20,
        turnover_vs_ma20=1.1, broad_index_positive=True,
        captured_at=(slot + timedelta(minutes=5, seconds=1)).isoformat(),
    )
    snapshot = MarketSentimentSnapshot(
        captured_at=slot.isoformat(),
        observed_at=(slot + timedelta(minutes=5, seconds=1)).isoformat(),
        source_as_of=slot.date().strftime("%Y%m%d"),
        input=input_value,
    )

    assert recorder.record_sentiment(snapshot) is None
    assert store.list_oos_observations(
        strategy_version=TOPDOWN_STRATEGY_ID, kind="market_sentiment"
    ) == []

from datetime import datetime, timedelta, timezone

from pa_agent.trading.market_history import MarketHistoryBackfillService
from pa_agent.trading.store import TradeStore

TZ8 = timezone(timedelta(hours=8))


def _sessions(_symbol, *, start_date, end_date):
    del start_date, end_date
    return [
        {"time": datetime(2026, 7, day, tzinfo=TZ8), "close": 3000 + day}
        for day in range(1, 22)
    ]


def test_market_history_backfill_persists_only_real_bar_dates_and_resumes(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    calls: list[str] = []

    def universe():
        return [{"code": f"{index:06d}"} for index in range(3000)]

    def daily(symbol, *, n, adjust):
        assert n >= 31
        assert adjust == "none"
        calls.append(symbol)
        return [
            {"time": datetime(2026, 7, day, tzinfo=TZ8), "close": 10 + day}
            for day in range(1, 22)
        ]

    service = MarketHistoryBackfillService(
        universe_loader=universe,
        daily_loader=daily,
        session_loader=_sessions,
        priority_symbol_loader=lambda: set(),
        request_pause_seconds=0,
        max_symbols_per_run=None,
    )
    report = service.backfill(
        store=store,
        captured_at=datetime(2026, 7, 22, 16, tzinfo=TZ8),
    )

    assert report.status == "complete"
    assert report.completed_symbols == 3000
    assert set(report.coverage_by_date.values()) == {3000}
    assert len(calls) == 3000

    calls.clear()
    second = service.backfill(
        store=store,
        captured_at=datetime(2026, 7, 22, 16, tzinfo=TZ8),
    )
    assert second.status == "complete"
    assert second.completed_symbols == 3000
    assert calls == []


def test_market_history_backfill_fails_closed_on_missing_real_bars(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")

    def universe():
        return [{"code": f"{index:06d}"} for index in range(3000)]

    def daily(symbol, *, n, adjust):
        del symbol, n, adjust
        return [
            {"time": datetime(2026, 7, day, tzinfo=TZ8), "close": 10 + day}
            for day in range(1, 21)
        ]

    report = MarketHistoryBackfillService(
        universe_loader=universe,
        daily_loader=daily,
        session_loader=_sessions,
        priority_symbol_loader=lambda: set(),
        request_pause_seconds=0,
        max_symbols_per_run=None,
    ).backfill(
        store=store,
        captured_at=datetime(2026, 7, 22, 16, tzinfo=TZ8),
    )

    assert report.status == "data_incomplete"
    assert report.completed_symbols == 0
    assert report.coverage_by_date["2026-07-21"] == 0
    assert any("market_history_coverage_2026-07-21" in gap for gap in report.data_gaps)


def test_market_history_backfill_uses_resumable_batches(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    calls: list[str] = []
    universe_calls = 0

    def universe():
        nonlocal universe_calls
        universe_calls += 1
        return [{"code": f"{index:06d}"} for index in range(10)]

    def daily(symbol, *, n, adjust):
        del n, adjust
        calls.append(symbol)
        return [
            {"time": datetime(2026, 7, day, tzinfo=TZ8), "close": 10 + day}
            for day in range(1, 22)
        ]

    service = MarketHistoryBackfillService(
        universe_loader=universe,
        daily_loader=daily,
        session_loader=_sessions,
        priority_symbol_loader=lambda: set(),
        request_pause_seconds=0,
        max_symbols_per_run=4,
    )
    first = service.backfill(
        store=store,
        captured_at=datetime(2026, 7, 22, 16, tzinfo=TZ8),
    )
    store.add_validation_run(first, dataset="market_history_backfill")
    second = service.backfill(
        store=store,
        captured_at=datetime(2026, 7, 22, 16, tzinfo=TZ8),
    )

    assert first.processed_symbols == 4
    assert first.completed_symbols == 4
    assert first.newly_completed_symbols == 4
    assert first.remaining_symbols == 6
    assert second.processed_symbols == 4
    assert second.completed_symbols == 8
    assert second.newly_completed_symbols == 4
    assert calls == [f"{index:06d}" for index in range(8)]
    assert universe_calls == 1


def test_market_history_prioritizes_incomplete_hs300_members(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    calls: list[str] = []

    def daily(symbol, *, n, adjust):
        del n, adjust
        calls.append(symbol)
        return [
            {"time": datetime(2026, 7, day, tzinfo=TZ8), "close": 10 + day}
            for day in range(1, 22)
        ]

    service = MarketHistoryBackfillService(
        universe_loader=lambda: [
            {"code": symbol} for symbol in ("000001", "000002", "600519", "601728")
        ],
        daily_loader=daily,
        session_loader=_sessions,
        priority_symbol_loader=lambda: {"600519", "601728"},
        request_pause_seconds=0,
        max_symbols_per_run=2,
    )

    report = service.backfill(
        store=store, captured_at=datetime(2026, 7, 22, 16, tzinfo=TZ8)
    )

    assert report.processed_symbols == 2
    assert calls == ["600519", "601728"]
    assert report.priority_symbol_count == 2
    assert report.priority_completed_symbols == 2


def test_market_history_stays_incomplete_until_priority_dependencies_are_complete(
    tmp_path,
) -> None:
    store = TradeStore(tmp_path / "trades.db")
    session_dates = [f"2026-07-{day:02d}" for day in range(1, 22)]
    store.upsert_market_daily_price_rows(
        [
            {"as_of": as_of, "symbol": f"{symbol:06d}", "price": 10.0}
            for as_of in session_dates
            for symbol in range(3000)
        ],
        captured_at=datetime(2026, 7, 22, 16, tzinfo=TZ8).isoformat(),
    )

    def daily(symbol, *, n, adjust):
        del n, adjust
        if symbol == "600519":
            return [
                {"time": datetime(2026, 7, day, tzinfo=TZ8), "close": 10 + day}
                for day in range(1, 21)
            ]
        return [
            {"time": datetime(2026, 7, day, tzinfo=TZ8), "close": 10 + day}
            for day in range(1, 22)
        ]

    report = MarketHistoryBackfillService(
        universe_loader=lambda: [{"code": f"{index:06d}"} for index in range(3000)],
        daily_loader=daily,
        session_loader=_sessions,
        priority_symbol_loader=lambda: {"600519"},
        request_pause_seconds=0,
        max_symbols_per_run=1,
    ).backfill(
        store=store,
        captured_at=datetime(2026, 7, 22, 16, tzinfo=TZ8),
    )

    assert min(report.coverage_by_date.values()) == 3000
    assert report.status == "data_incomplete"
    assert report.priority_symbol_count == 1
    assert report.priority_completed_symbols == 0
    assert report.priority_missing_symbols == ["600519"]
    assert "hs300_priority_history_0_of_1" in report.data_gaps

from datetime import date, timedelta

from pa_agent.trading.store import TradeStore


def _rows(day: int, count: int = 3000) -> list[dict]:
    return [
        {"code": f"{index:06d}", "price": 10 + index / 1000 + day / 100}
        for index in range(count)
    ]


def test_market_high_low_counts_fail_closed_then_use_20_prior_sessions(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    start = date(2026, 7, 1)
    for offset in range(20):
        new_high, new_low, details = store.update_market_daily_prices_and_high_low(
            _rows(offset),
            as_of=(start + timedelta(days=offset)).isoformat(),
            captured_at=f"{start + timedelta(days=offset)}T15:00:00+08:00",
        )
        assert new_high is None
        assert new_low is None
        assert details["prior_sessions"] == offset

    new_high, new_low, details = store.update_market_daily_prices_and_high_low(
        _rows(20),
        as_of=(start + timedelta(days=20)).isoformat(),
        captured_at=f"{start + timedelta(days=20)}T15:00:00+08:00",
    )

    assert new_high == 3000
    assert new_low == 0
    assert details["comparable_count"] == 3000


def test_market_high_low_counts_require_sufficient_symbol_coverage(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    start = date(2026, 7, 1)
    for offset in range(20):
        store.update_market_daily_prices_and_high_low(
            _rows(offset, count=200),
            as_of=(start + timedelta(days=offset)).isoformat(),
            captured_at=f"{start + timedelta(days=offset)}T15:00:00+08:00",
        )

    high, low, details = store.update_market_daily_prices_and_high_low(
        _rows(20, count=200),
        as_of=(start + timedelta(days=20)).isoformat(),
        captured_at=f"{start + timedelta(days=20)}T15:00:00+08:00",
    )

    assert high is None and low is None
    assert details["reason"] == "market_price_history_coverage_below_3000"


def test_market_daily_price_dates_returns_distinct_latest_dates(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    for day in (1, 2, 3):
        as_of = f"2026-08-{day:02d}"
        store.update_market_daily_prices_and_high_low(
            [
                {"code": "600519", "price": 1400 + day},
                {"code": "000858", "price": 120 + day},
            ],
            as_of=as_of,
            captured_at=f"{as_of}T15:00:00+08:00",
        )

    assert store.market_daily_price_dates(limit=2) == [
        "2026-08-03",
        "2026-08-02",
    ]

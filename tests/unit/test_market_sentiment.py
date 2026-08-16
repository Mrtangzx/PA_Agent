from datetime import datetime, timedelta, timezone

from pa_agent.trading.market_sentiment import MarketSentimentService
from pa_agent.trading.store import TradeStore
from pa_agent.trading.universe import OfficialConstituent, OfficialConstituentFile

NOW = datetime(2026, 8, 13, 10, 0, tzinfo=timezone(timedelta(hours=8)))


def _universe() -> list[dict]:
    return [{
        "code": f"{index:06d}",
        "price": 10 + index / 1000,
        "pct_chg": 1 if index < 3600 else -1,
        "amount": 1_000_000,
    }
            for index in range(5000)]


def _pool(**_) -> dict:
    return {
        "source_as_of": "20260813",
        "limit_up_count": 40,
        "limit_down_count": 5,
        "blast_count": 10,
        "limit_up": [{}] * 40,
        "limit_down": [{}] * 5,
        "blast": [{}] * 10,
    }


def test_market_sentiment_is_complete_only_with_all_versioned_inputs() -> None:
    service = MarketSentimentService(universe_loader=_universe, limit_pool_loader=_pool)
    result = service.capture(
        hs300_breadth_pct=62,
        captured_at=NOW,
        historical_turnover_vs_ma20=1.1,
        new_high_count=100,
        new_low_count=20,
    )
    assert result.data_complete
    assert result.input is not None
    assert result.input.advancing_pct == 72
    assert result.input.seal_success_pct == 80
    assert result.input.blast_board_pct == 20
    assert result.source_details["a_share_turnover"] == 5_000_000_000
    assert result.source_hash


def test_market_sentiment_does_not_invent_missing_history() -> None:
    service = MarketSentimentService(universe_loader=_universe, limit_pool_loader=_pool)
    result = service.capture(hs300_breadth_pct=62, captured_at=NOW)
    assert not result.data_complete
    assert result.input is None
    assert "turnover_ma20_history_missing" in result.data_gaps
    assert "new_high_low_counts_missing" in result.data_gaps
    assert result.source_details["advancing_pct"] == 72


def test_market_sentiment_derives_worsening_flags_from_previous_frozen_inputs() -> None:
    service = MarketSentimentService(universe_loader=_universe, limit_pool_loader=_pool)
    first = service.capture(
        hs300_breadth_pct=62,
        captured_at=NOW - timedelta(minutes=15),
        historical_turnover_vs_ma20=1.0,
        new_high_count=100,
        new_low_count=20,
    )
    assert first.input is not None
    prior = first.input.model_copy(update={
        "limit_down_count": 2,
        "blast_board_pct": 5,
        "retreat_or_panic_bars": 1,
        "advancing_pct": 35,
    })

    second = service.capture(
        hs300_breadth_pct=35,
        captured_at=NOW,
        historical_turnover_vs_ma20=1.3,
        new_high_count=20,
        new_low_count=100,
        previous_inputs=[prior],
        broad_index_positive=False,
    )

    assert second.input is not None
    assert second.input.retreat_or_panic_bars == 2
    assert second.input.limit_down_and_blast_worsening
    assert second.input.systemic_volume_selloff


def test_store_capture_uses_dedicated_hs300_breadth_not_external_pool_value(
    tmp_path, monkeypatch
) -> None:
    from pa_agent.trading.store import TradeStore

    calls = []
    service = MarketSentimentService(
        universe_loader=_universe,
        limit_pool_loader=_pool,
        now_provider=lambda: NOW + timedelta(seconds=20),
        hs300_breadth_loader=lambda at: (
            calls.append(at) or 61.5,
            {"member_count": 300, "valid_member_count": 300},
        ),
    )
    monkeypatch.setattr(
        service, "turnover_vs_ma20",
        lambda **_: (1.1, {"broad_index_positive": True}),
    )
    store = TradeStore(tmp_path / "trades.db")
    # Seed 20 distinct closed-market baselines so new-high/new-low inputs exist.
    for index in range(20):
        store.update_market_daily_prices_and_high_low(
            _universe(), as_of=f"2026-07-{index + 1:02d}", captured_at=NOW.isoformat()
        )

    result = service.capture_for_store(store=store, captured_at=NOW)

    assert len(calls) == 1
    assert result.input is not None
    assert result.input.hs300_above_ma20_pct == 61.5
    assert result.source_details["hs300_breadth"]["member_count"] == 300
    assert result.observed_at == (NOW + timedelta(seconds=20)).isoformat()
    assert result.source_details["capture_delay_seconds"] == 20


def test_store_breadth_uses_19_closed_sessions_and_one_live_snapshot(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    members = [f"{index:06d}" for index in range(300)]
    for day in range(1, 20):
        store.upsert_market_daily_price_rows(
            [
                {"as_of": f"2026-07-{day:02d}", "symbol": symbol, "price": 10.0}
                for symbol in members
            ],
            captured_at=NOW.isoformat(),
        )
    official = OfficialConstituentFile(
        source_as_of=NOW.date(),
        source_url="https://www.csindex.com.cn/fixture.xls",
        source_hash="a" * 64,
        constituents=[
            OfficialConstituent(symbol=symbol, name=symbol) for symbol in members
        ],
    )
    service = MarketSentimentService(
        universe_loader=lambda: [
            {"code": symbol, "price": 11.0, "pct_chg": 1, "amount": 1_000_000}
            for symbol in members
        ],
        limit_pool_loader=_pool,
        hs300_member_loader=lambda: official,
    )

    breadth, details = service._hs300_breadth_from_store(
        store=store,
        market_rows=service.universe_loader(),
        captured_at=NOW,
    )

    assert breadth == 100
    assert details["valid_member_count"] == 300
    assert details["history_sessions_required"] == 19


def test_store_breadth_batch_fills_members_missing_from_all_a_page(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    members = [f"{index:06d}" for index in range(300)]
    for day in range(1, 20):
        store.upsert_market_daily_price_rows(
            [{"as_of": f"2026-07-{day:02d}", "symbol": symbol, "price": 10.0}
             for symbol in members],
            captured_at=NOW.isoformat(),
        )
    official = OfficialConstituentFile(
        source_as_of=NOW.date(), source_url="fixture", source_hash="b" * 64,
        constituents=[OfficialConstituent(symbol=s, name=s) for s in members],
    )
    missing = members[-36:]
    service = MarketSentimentService(
        hs300_member_loader=lambda: official,
        hs300_spot_loader=lambda symbols: [
            {"code": symbol, "price": 11.0} for symbol in symbols
        ],
    )
    market_rows = [
        {"code": symbol, "price": 11.0} for symbol in members if symbol not in missing
    ]

    breadth, details = service._hs300_breadth_from_store(
        store=store, market_rows=market_rows, captured_at=NOW
    )

    assert breadth == 100
    assert details["valid_member_count"] == 300
    assert details["spot_fallback_requested"] == 36
    assert details["spot_fallback_resolved"] == 36


def test_sentiment_capture_over_five_minutes_fails_closed(tmp_path, monkeypatch) -> None:
    store = TradeStore(tmp_path / "trades.db")
    service = MarketSentimentService(
        universe_loader=_universe,
        limit_pool_loader=_pool,
        hs300_breadth_loader=lambda _at: (61.5, {"valid_member_count": 300}),
        now_provider=lambda: NOW + timedelta(minutes=5, seconds=1),
    )
    monkeypatch.setattr(
        service, "turnover_vs_ma20", lambda **_: (1.1, {"broad_index_positive": True})
    )
    for index in range(20):
        store.update_market_daily_prices_and_high_low(
            _universe(), as_of=f"2026-07-{index + 1:02d}", captured_at=NOW.isoformat()
        )

    result = service.capture_for_store(store=store, captured_at=NOW)

    assert not result.data_complete
    assert result.input is None
    assert "sentiment_capture_delay_exceeded_300s" in result.data_gaps

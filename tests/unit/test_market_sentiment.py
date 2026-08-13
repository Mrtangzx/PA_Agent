from datetime import datetime, timedelta, timezone

from pa_agent.trading.market_sentiment import MarketSentimentService

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

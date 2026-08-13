from __future__ import annotations

from pa_agent.trading.quant import SignalDecision, SignalStatus
from pa_agent.trading.topdown import (
    HotspotItem,
    HotspotSnapshot,
    IndexScoreInput,
    SentimentScoreInput,
    StockScoreInput,
    ThemeScoreInput,
    TopDownScoringContext,
)
from pa_agent.trading.topdown_replay import TopDownReplayEngine, TopDownReplayFrame

SIGNAL_AT = "2026-08-11T15:00:00+08:00"
FIRST = "2026-08-12T09:45:00+08:00"
SECOND = "2026-08-12T10:00:00+08:00"


def _signal() -> SignalDecision:
    return SignalDecision(
        status=SignalStatus.ALLOW,
        strategy_id="hs300_daily_pullback_v1",
        parameter_version="1.0.0",
        pool_version="hs300-2026-08",
        symbol="600519",
        signal_time=SIGNAL_AT,
        trigger_price=100,
        max_entry_price=105,
        initial_stop=95,
        condition_snapshot={"stop_distance_atr": 1.5},
    )


def _context(at: str, *, missing_theme: bool = False) -> TopDownScoringContext:
    indexes = [
        IndexScoreInput(
            code=code,
            close_above_ma60=True,
            ma20_above_ma60=True,
            ma20_slope_positive=True,
            intraday_above_vwap_and_ma20_rising=True,
            captured_at=at,
        )
        for code in ("000300", "000001", "000852", "399006")
    ]
    sentiment = SentimentScoreInput(
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
        captured_at=at,
    )
    hotspot = HotspotSnapshot(
        symbol="600519",
        captured_at=at,
        frozen_at=at,
        industries=["白酒"],
        items=[HotspotItem(
            item_id="official-1",
            title="行业信息",
            source="交易所公告",
            published_at=at,
            official=True,
            verified=True,
            positive=True,
            related_themes=["白酒"],
        )],
        positive_score=3,
    ).with_source_hash()
    theme = ThemeScoreInput(
        relative_strength_percentile=90,
        advancing_pct=80,
        main_net_inflow_pct=2.5,
        turnover_vs_recent=1.4,
        persistence_days=4,
        hotspot=hotspot,
        captured_at=at,
    )
    stock = StockScoreInput(
        daily_candidate_passed=True,
        in_trigger_zone=True,
        below_max_entry_price=True,
        breakout_confirmed_on_closed_bar=True,
        above_vwap=True,
        volume_confirmed=True,
        no_intraday_reversal=True,
        tradable=True,
        stop_distance_atr=1.5,
        quote_age_seconds=2,
        quote_deviation_pct=0.1,
        captured_at=at,
    )
    return TopDownScoringContext(
        symbol="600519",
        bar_closed_at=at,
        indexes=indexes,
        sentiment=sentiment,
        theme=None if missing_theme else theme,
        stock=stock,
        pool_version="hs300-2026-08",
        daily_signal_id=SIGNAL_AT,
        required_source_timestamps={
            "index": at,
            "sentiment": at,
            "theme": at,
            "quote": at,
        },
    )


def _frame(at: str, **updates) -> TopDownReplayFrame:
    values = {
        "context": _context(at),
        "pool_members": {"600519"},
        "pool_effective_at": "2026-08-01T00:00:00+08:00",
        "pool_source_published_at": "2026-07-31T18:00:00+08:00",
    }
    values.update(updates)
    return TopDownReplayFrame(**values)


def test_replay_requires_two_adjacent_closed_bars_and_is_deterministic() -> None:
    engine = TopDownReplayEngine()
    frames = [_frame(FIRST), _frame(SECOND)]

    first = engine.run(daily_signal=_signal(), frames=frames)
    second = engine.run(daily_signal=_signal(), frames=frames)

    assert first.model_dump() == second.model_dump()
    assert first.status == "complete"
    assert first.frame_count == 2
    assert first.eligible_count == 1
    assert first.first_eligible_at == SECOND
    assert [item.consecutive_pass_count for item in first.scores] == [1, 2]
    assert first.input_hashes == second.input_hashes


def test_replay_rejects_future_source_and_historical_pool_mismatch() -> None:
    future = _context(FIRST).model_copy(update={
        "required_source_timestamps": {"theme": SECOND},
    })
    report = TopDownReplayEngine().run(
        daily_signal=_signal(),
        frames=[_frame(FIRST, context=future, pool_members={"000001"})],
    )

    assert report.status == "invalid"
    assert "frame_1_theme_from_future" in report.hard_failures
    assert "frame_1_not_in_historical_pool" in report.hard_failures
    assert not report.scores


def test_missing_historical_theme_data_is_incomplete_not_zero_filled() -> None:
    report = TopDownReplayEngine().run(
        daily_signal=_signal(),
        frames=[_frame(FIRST, context=_context(FIRST, missing_theme=True))],
    )

    assert report.status == "data_incomplete"
    assert report.scores[0].total_score is None
    assert "missing_theme" in report.data_gaps
    assert report.eligible_count == 0

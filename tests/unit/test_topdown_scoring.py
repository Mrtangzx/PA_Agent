from __future__ import annotations

from pa_agent.trading.topdown import (
    HotspotItem,
    HotspotSnapshot,
    IndexScoreInput,
    SentimentScoreInput,
    StockScoreInput,
    ThemeScoreInput,
    TopDownScoreStatus,
    TopDownScoring,
    TopDownScoringContext,
)

NOW = "2026-08-12T10:00:00+08:00"
NEXT = "2026-08-12T10:15:00+08:00"


def _indexes(*, bearish: bool = False):
    result = []
    for code, name in (
        ("000300", "沪深300"),
        ("000001", "上证指数"),
        ("000852", "中证1000"),
        ("399006", "创业板指"),
    ):
        result.append(IndexScoreInput(
            code=code,
            name=name,
            close_above_ma60=not bearish,
            ma20_above_ma60=not bearish,
            ma20_slope_positive=not bearish,
            intraday_above_vwap_and_ma20_rising=True,
            captured_at=NOW,
        ))
    return result


def _sentiment(**updates):
    values = dict(
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
        captured_at=NOW,
    )
    values.update(updates)
    return SentimentScoreInput(**values)


def _theme(*, major_negative: bool = False):
    item = HotspotItem(
        item_id="news-1",
        title="白酒板块获得资金关注",
        source="东方财富",
        published_at=NOW,
        official=major_negative,
        verified=True,
        positive=not major_negative,
        major_negative=major_negative,
        risk_code="regulatory_investigation" if major_negative else "",
        related_themes=["白酒"],
    )
    hotspot = HotspotSnapshot(
        symbol="600519",
        captured_at=NOW,
        frozen_at=NOW,
        industries=["白酒"],
        concepts=["消费"],
        items=[item],
        positive_score=3 if not major_negative else 0,
    ).with_source_hash()
    return ThemeScoreInput(
        relative_strength_percentile=90,
        advancing_pct=80,
        main_net_inflow_pct=2.5,
        turnover_vs_recent=1.4,
        persistence_days=4,
        hotspot=hotspot,
        captured_at=NOW,
    )


def _stock():
    return StockScoreInput(
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
        captured_at=NOW,
    )


def _context(**updates):
    values = dict(
        symbol="600519",
        bar_closed_at=NOW,
        indexes=_indexes(),
        sentiment=_sentiment(),
        theme=_theme(),
        stock=_stock(),
        pool_version="hs300-2026-08",
        daily_signal_id="signal-1",
        required_source_timestamps={"index": NOW, "sentiment": NOW, "theme": NOW, "quote": NOW},
    )
    values.update(updates)
    return TopDownScoringContext(**values)


def test_same_input_produces_same_scores_and_hash() -> None:
    scoring = TopDownScoring()
    first = scoring.evaluate(_context())
    second = scoring.evaluate(_context())
    assert first.model_dump() == second.model_dump()
    assert first.input_hash == second.input_hash
    assert first.total_score is not None and 70 <= first.total_score <= 100
    assert first.index_score == 40
    assert first.stock_score == 10
    assert first.status is TopDownScoreStatus.WAIT_CONFIRMATION


def test_two_closed_bars_are_required_for_risk_gate() -> None:
    scoring = TopDownScoring()
    first = scoring.evaluate(_context())
    second = scoring.evaluate(_context(
        bar_closed_at=NEXT,
        previous_snapshot=first,
        required_source_timestamps={
            "index": NEXT, "sentiment": NEXT, "theme": NEXT, "quote": NEXT,
        },
    ))
    assert first.consecutive_pass_count == 1
    assert second.consecutive_pass_count == 2
    assert second.status is TopDownScoreStatus.ELIGIBLE_FOR_RISK


def test_same_bar_or_non_adjacent_score_does_not_increment_confirmation() -> None:
    scoring = TopDownScoring()
    first = scoring.evaluate(_context())
    same_bar = scoring.evaluate(_context(previous_snapshot=first))
    skipped = scoring.evaluate(_context(
        bar_closed_at="2026-08-12T10:30:00+08:00",
        previous_snapshot=first,
    ))

    assert same_bar.consecutive_pass_count == 1
    assert same_bar.status is TopDownScoreStatus.WAIT_CONFIRMATION
    assert skipped.consecutive_pass_count == 1
    assert skipped.status is TopDownScoreStatus.WAIT_CONFIRMATION


def test_lunch_break_adjacent_trading_bars_are_consecutive() -> None:
    scoring = TopDownScoring()
    first = scoring.evaluate(_context(bar_closed_at="2026-08-12T11:30:00+08:00"))
    second = scoring.evaluate(_context(
        bar_closed_at="2026-08-12T13:15:00+08:00",
        previous_snapshot=first,
    ))

    assert second.consecutive_pass_count == 2
    assert second.status is TopDownScoreStatus.ELIGIBLE_FOR_RISK


def test_missing_required_index_fails_closed_without_zero_score() -> None:
    result = TopDownScoring().evaluate(_context(indexes=_indexes()[:-1]))
    assert result.status is TopDownScoreStatus.DATA_INCOMPLETE
    assert result.total_score is None
    assert "missing_index_399006" in result.data_gaps


def test_index_hard_gate_cannot_be_offset_by_other_components() -> None:
    result = TopDownScoring().evaluate(_context(indexes=_indexes(bearish=True)))
    assert result.total_score is not None
    assert "index_score_below_24" in result.hard_blocks
    assert "three_indexes_bearish" in result.hard_blocks
    assert result.status is TopDownScoreStatus.BLOCKED


def test_verified_official_negative_event_is_a_hard_block() -> None:
    result = TopDownScoring().evaluate(_context(theme=_theme(major_negative=True)))
    assert any(value.startswith("major_negative_") for value in result.hard_blocks)
    assert result.status is TopDownScoreStatus.BLOCKED


def test_open_authorization_is_revoked_below_65() -> None:
    weak = _sentiment(
        advancing_pct=50,
        hs300_above_ma20_pct=45,
        limit_up_count=3,
        limit_down_count=2,
        seal_success_pct=50,
        blast_board_pct=40,
        new_high_count=5,
        new_low_count=5,
        turnover_vs_ma20=0.5,
    )
    stock = _stock().model_copy(update={
        "in_trigger_zone": False,
        "breakout_confirmed_on_closed_bar": False,
        "above_vwap": False,
        "volume_confirmed": False,
        "no_intraday_reversal": False,
    })
    theme = _theme().model_copy(update={
        "relative_strength_percentile": 5,
        "advancing_pct": 10,
        "main_net_inflow_pct": -2,
        "turnover_vs_recent": 0.1,
        "persistence_days": 0,
        "hotspot": _theme().hotspot.model_copy(update={"positive_score": 0}),
    })
    result = TopDownScoring().evaluate(
        _context(sentiment=weak, theme=theme, stock=stock, authorization_open=True)
    )
    assert result.total_score is not None and result.total_score < 65
    assert result.status is TopDownScoreStatus.AUTHORIZATION_REVOKED

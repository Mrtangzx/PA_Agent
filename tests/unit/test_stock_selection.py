from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pa_agent.trading.stock_selection import (
    SelectionStrategy,
    StockSelectionService,
    StockSelectionSettings,
)
from pa_agent.trading.topdown import HotspotSnapshot

NOW = datetime(2026, 8, 15, 10, 30, tzinfo=timezone(timedelta(hours=8)))


def _hotspot(*, negative: bool = False, complete: bool = True) -> HotspotSnapshot:
    return HotspotSnapshot(
        symbol="600519",
        captured_at=NOW.isoformat(),
        frozen_at=NOW.isoformat(),
        industries=["白酒"],
        concepts=["消费复苏"],
        items=[],
        board_strength={
            "market_verified": True,
            "flows": [{
                "board_name": "白酒",
                "relative_strength_percentile": 88,
                "persistence_days": 3,
                "main_net_pct": 1.2,
            }],
        },
        positive_score=1,
        negative_blocks=["major_negative_regulatory_investigation"] if negative else [],
        data_gaps=[] if complete else ["announcement_source_missing"],
        rule_version="hotspot_time_window_v2",
        source_hash="hotspot-hash",
    )


def _flat_bars() -> list[dict]:
    rows = []
    for index in range(65):
        close = 100 + index * 0.08
        rows.append({
            "time": f"2026-05-{(index % 28) + 1:02d}",
            "open": close - 0.1,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": 1000,
        })
    return rows


def test_hot_theme_and_main_force_theme_are_deterministic() -> None:
    service = StockSelectionService(clock=lambda: NOW)
    result = service.evaluate(
        symbol="600519", name="贵州茅台", daily_bars=_flat_bars(),
        hotspot=_hotspot(), evaluated_at=NOW,
    )

    assert result.status == "eligible"
    assert result.strategy_tags == [
        SelectionStrategy.HOT_THEME.value,
        SelectionStrategy.MAIN_FORCE_THEME.value,
    ]
    assert result.evidence["negative_news_check"] == "passed"


def test_major_negative_is_a_hard_exclusion() -> None:
    service = StockSelectionService(clock=lambda: NOW)
    result = service.evaluate(
        symbol="600519", name="贵州茅台", daily_bars=_flat_bars(),
        hotspot=_hotspot(negative=True), evaluated_at=NOW,
    )

    assert result.status == "blocked"
    assert result.strategy_tags == []
    assert "major_negative_regulatory_investigation" in result.hard_blocks


def test_missing_or_unverifiable_negative_news_data_fails_closed() -> None:
    service = StockSelectionService(clock=lambda: NOW)
    missing = service.evaluate(
        symbol="600519", name="贵州茅台", daily_bars=_flat_bars(),
        hotspot=None, evaluated_at=NOW,
    )
    incomplete = service.evaluate(
        symbol="600519", name="贵州茅台", daily_bars=_flat_bars(),
        hotspot=_hotspot(complete=False), evaluated_at=NOW,
    )

    assert missing.status == "data_incomplete"
    assert "major_negative_evidence_missing" in missing.data_gaps
    assert incomplete.status == "data_incomplete"
    assert "announcement_source_missing" in incomplete.data_gaps


def test_volume_suffocation_requires_volume_and_volatility_contraction() -> None:
    settings = StockSelectionSettings(
        hot_theme_min_positive_score=3,
        main_force_min_net_inflow_pct=10,
    )
    service = StockSelectionService(settings, clock=lambda: NOW)
    bars = _flat_bars()
    for index in range(-5, 0):
        close = bars[index]["close"]
        bars[index].update(high=close + 0.25, low=close - 0.25, volume=400)

    result = service.evaluate(
        symbol="600519", name="贵州茅台", daily_bars=bars,
        hotspot=_hotspot(), evaluated_at=NOW,
    )

    assert result.status == "eligible"
    assert result.strategy_tags == [SelectionStrategy.VOLUME_SUFFOCATION.value]
    assert result.evidence["volume_ratio_5_to_previous20"] <= 0.65
    assert result.evidence["atr_contraction_ratio"] <= 0.8


def test_trend_start_requires_breakout_ma_structure_and_volume() -> None:
    settings = StockSelectionSettings(
        hot_theme_min_positive_score=3,
        main_force_min_net_inflow_pct=10,
    )
    service = StockSelectionService(settings, clock=lambda: NOW)
    bars = _flat_bars()
    bars[-1].update(open=108, high=112.5, low=107.8, close=112, volume=2200)

    result = service.evaluate(
        symbol="600519", name="贵州茅台", daily_bars=bars,
        hotspot=_hotspot(), evaluated_at=NOW,
    )

    assert result.status == "eligible"
    assert SelectionStrategy.TREND_START.value in result.strategy_tags
    assert result.evidence["latest_volume_ratio_20"] >= 1.2


def test_non_a_share_never_enters_selection() -> None:
    service = StockSelectionService(clock=lambda: NOW)
    result = service.evaluate(
        symbol="800000", name="非A股", daily_bars=_flat_bars(),
        hotspot=_hotspot(), evaluated_at=NOW,
    )

    assert result.status == "blocked"
    assert result.strategy_tags == []
    assert "not_a_share" in result.hard_blocks

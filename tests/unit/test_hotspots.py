from datetime import datetime

from pa_agent.data.eastmoney_extended import _optional_float
from pa_agent.trading.hotspots import (
    HotspotService,
    classify_major_negative,
    publication_window_status,
)
from pa_agent.trading.topdown import HotspotSnapshot


def test_official_negative_titles_map_to_stable_codes() -> None:
    assert (
        classify_major_negative("关于收到中国证监会立案告知书的公告")
        == "regulatory_investigation"
    )
    assert classify_major_negative("股票可能被实施退市风险警示") == "delisting_or_st_risk"
    assert classify_major_negative("控股股东减持股份计划公告") == "major_shareholder_reduction"


def test_normal_business_title_is_not_a_hard_block() -> None:
    assert classify_major_negative("关于召开年度股东大会的通知") == ""


def test_director_or_manager_reduction_is_not_misclassified_as_major_shareholder() -> None:
    assert classify_major_negative("部分董事及高级管理人员减持股份进展公告") == ""
    assert classify_major_negative("董事、高级管理人员减持股份结果公告") == ""


def test_completed_or_cancelled_major_shareholder_reduction_is_not_an_open_block() -> None:
    assert classify_major_negative("控股股东减持计划实施完毕公告") == ""
    assert classify_major_negative("实际控制人终止减持计划的公告") == ""


def test_broad_words_without_a_confirmed_major_fact_do_not_hard_block() -> None:
    assert classify_major_negative("关于股票交易异常波动的公告") == ""
    assert classify_major_negative("关于收到监管工作函的公告") == ""
    assert classify_major_negative("年度审计意见为标准无保留意见") == ""
    assert classify_major_negative("关于不予立案的通知") == ""


def test_material_performance_deterioration_requires_a_performance_context() -> None:
    assert (
        classify_major_negative("2026年半年度业绩预告下修暨预计亏损公告")
        == "material_performance_deterioration"
    )
    assert classify_major_negative("原材料采购价格大幅下降的公告") == ""


def test_eastmoney_dash_numeric_value_stays_missing() -> None:
    assert _optional_float("-") is None
    assert _optional_float("--") is None
    assert _optional_float(None) is None
    assert _optional_float("1.25") == 1.25


def test_publication_window_rejects_missing_future_and_stale_information() -> None:
    frozen = datetime.fromisoformat("2026-08-14T10:00:00+08:00")

    assert publication_window_status("", frozen_at=frozen, valid_days=3) == (
        False,
        "published_at_missing",
    )
    assert publication_window_status(
        "2026-08-14T10:00:01+08:00", frozen_at=frozen, valid_days=3
    ) == (False, "published_at_in_future")
    assert publication_window_status(
        "2026-08-10T09:00:00+08:00", frozen_at=frozen, valid_days=3
    ) == (False, "published_at_outside_window")
    assert publication_window_status(
        "2026-08-13T09:00:00+08:00", frozen_at=frozen, valid_days=3
    ) == (True, "within_effective_window")


def test_major_negative_has_no_silent_expiry_without_resolution_evidence() -> None:
    frozen = datetime.fromisoformat("2026-08-14T10:00:00+08:00")

    assert publication_window_status(
        "2025-01-01T09:00:00+08:00", frozen_at=frozen, valid_days=None
    ) == (True, "within_effective_window")


def test_freeze_applies_time_rules_and_is_deterministic(monkeypatch) -> None:
    monkeypatch.setattr(
        "pa_agent.data.eastmoney_extended.fetch_stock_board_tags",
        lambda _symbol: {"concepts": ["算力"]},
    )
    monkeypatch.setattr(
        "pa_agent.data.eastmoney_extended.fetch_operations_required",
        lambda _symbol: {"ssbk": ["算力"]},
    )
    monkeypatch.setattr(
        "pa_agent.data.eastmoney_extended.fetch_stock_board_money_flows",
        lambda *_args, **_kwargs: [
            {"pct_chg": 2.0, "main_net_pct": 1.0}
        ],
    )
    monkeypatch.setattr(
        "pa_agent.data.eastmoney_extended.fetch_stock_announcements",
        lambda *_args, **_kwargs: [
            {"title": "控股股东减持股份计划公告", "notice_date": "2026-08-13 09:00:00"},
            {"title": "关于召开股东大会的公告", "notice_date": "2026-06-01 09:00:00"},
            {"title": "关于日常经营事项的公告"},
        ],
    )
    monkeypatch.setattr(
        "pa_agent.data.eastmoney_extended.fetch_stock_news",
        lambda *_args, **_kwargs: [
            {"title": "算力板块活跃", "show_time": "2026-08-13 09:30:00"},
            {"title": "算力板块旧闻", "show_time": "2026-07-01 09:30:00"},
            {"title": "算力板块未来稿", "show_time": "2026-08-15 09:30:00"},
        ],
    )
    service = HotspotService()

    first = service.freeze("688158", frozen_at="2026-08-14T10:00:00+08:00")
    second = service.freeze("688158", frozen_at="2026-08-14T10:00:00+08:00")

    assert first.source_hash == second.source_hash
    assert first.positive_score == 1
    assert first.negative_blocks == ["major_negative_major_shareholder_reduction"]
    assert first.data_gaps == []
    assert first.rule_version == "hotspot_time_window_v2"
    status = {item.title: item.time_validation_reason for item in first.items}
    assert status["算力板块活跃"] == "within_effective_window"
    assert status["算力板块旧闻"] == "published_at_outside_window"
    assert status["算力板块未来稿"] == "published_at_in_future"


def test_unverifiable_major_negative_time_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(
        "pa_agent.data.eastmoney_extended.fetch_stock_board_tags",
        lambda _symbol: {},
    )
    monkeypatch.setattr(
        "pa_agent.data.eastmoney_extended.fetch_operations_required",
        lambda _symbol: {},
    )
    monkeypatch.setattr(
        "pa_agent.data.eastmoney_extended.fetch_stock_board_money_flows",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "pa_agent.data.eastmoney_extended.fetch_stock_announcements",
        lambda *_args, **_kwargs: [{"title": "收到立案告知书的公告"}],
    )
    monkeypatch.setattr(
        "pa_agent.data.eastmoney_extended.fetch_stock_news",
        lambda *_args, **_kwargs: [],
    )

    snapshot = HotspotService().freeze(
        "688158", frozen_at="2026-08-14T10:00:00+08:00"
    )

    assert snapshot.data_gaps == [
        "major_negative_time_unverified:regulatory_investigation"
    ]
    assert snapshot.negative_blocks == [
        "major_negative_time_unverified:regulatory_investigation"
    ]
    assert HotspotService.theme_metrics(snapshot) is None


def test_empty_announcement_payload_cannot_prove_no_major_negative(monkeypatch) -> None:
    monkeypatch.setattr(
        "pa_agent.data.eastmoney_extended.fetch_stock_board_tags", lambda _symbol: {}
    )
    monkeypatch.setattr(
        "pa_agent.data.eastmoney_extended.fetch_operations_required", lambda _symbol: {}
    )
    monkeypatch.setattr(
        "pa_agent.data.eastmoney_extended.fetch_stock_board_money_flows",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "pa_agent.data.eastmoney_extended.fetch_stock_announcements",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "pa_agent.data.eastmoney_extended.fetch_stock_news",
        lambda *_args, **_kwargs: [],
    )

    snapshot = HotspotService().freeze(
        "600519", frozen_at="2026-08-14T10:00:00+08:00"
    )

    assert "announcement_snapshot_empty" in snapshot.data_gaps


def _snapshot_with_flows(flows: list[dict]) -> HotspotSnapshot:
    return HotspotSnapshot(
        symbol="300308",
        captured_at="2026-08-13T10:15:00+08:00",
        frozen_at="2026-08-13T10:15:00+08:00",
        industries=["通信设备"],
        concepts=["CPO"],
        board_strength={"flows": flows, "market_verified": True},
    )


def test_theme_metrics_fail_closed_when_board_dimensions_are_missing() -> None:
    snapshot = _snapshot_with_flows([
        {"board_code": "BK0736", "pct_chg": 2.1, "main_net_pct": 1.2},
    ])

    assert HotspotService.theme_metrics(snapshot) is None


def test_theme_metrics_are_deterministic_for_complete_frozen_board_data() -> None:
    snapshot = _snapshot_with_flows([
        {
            "board_code": "BK0736",
            "pct_chg": 2.1,
            "main_net_pct": 1.2,
            "advancing_pct": 72.5,
            "turnover_vs_recent": 1.35,
            "persistence_days": 3,
            "relative_strength_percentile": 88.0,
        },
        {
            "board_code": "BK0816",
            "pct_chg": -0.4,
            "main_net_pct": -0.2,
            "advancing_pct": 42.0,
            "turnover_vs_recent": 0.8,
            "persistence_days": 0,
            "relative_strength_percentile": 30.0,
        },
    ])

    expected = {
        "relative_strength_percentile": 88.0,
        "advancing_pct": 72.5,
        "main_net_inflow_pct": 1.2,
        "turnover_vs_recent": 1.35,
        "persistence_days": 3,
        "positive_board_share": 50.0,
    }
    assert HotspotService.theme_metrics(snapshot) == expected
    assert HotspotService.theme_metrics(snapshot) == expected

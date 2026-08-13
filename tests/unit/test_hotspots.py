from pa_agent.trading.hotspots import HotspotService, classify_major_negative
from pa_agent.trading.topdown import HotspotSnapshot


def test_official_negative_titles_map_to_stable_codes() -> None:
    assert classify_major_negative("关于收到中国证监会立案告知书的公告") == "regulatory_investigation"
    assert classify_major_negative("股票可能被实施退市风险警示") == "delisting_or_st_risk"
    assert classify_major_negative("控股股东减持股份计划公告") == "major_shareholder_reduction"


def test_normal_business_title_is_not_a_hard_block() -> None:
    assert classify_major_negative("关于召开年度股东大会的通知") == ""


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

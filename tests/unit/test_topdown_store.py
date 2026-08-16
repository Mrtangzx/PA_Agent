from __future__ import annotations

from datetime import date

from pa_agent.trading.store import TradeStore
from pa_agent.trading.topdown import (
    HotspotSnapshot,
    TopDownScoreSnapshot,
    TopDownScoreStatus,
)
from pa_agent.trading.universe import UniverseSnapshot

NOW = "2026-08-12T10:00:00+08:00"


def test_store_round_trips_universe_score_and_hotspot(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    universe = UniverseSnapshot(
        as_of=date(2026, 8, 1),
        version="hs300-2026-08",
        symbols=["600519", "000858"],
        rejected={"600000": ["data_incomplete"]},
    )
    store.upsert_universe_snapshot(universe, source_updated_at=NOW)
    assert store.list_universe_snapshots()[0]["snapshot"]["symbols"] == ["600519", "000858"]

    score = TopDownScoreSnapshot(
        strategy_version="hs300_topdown_4321_intraday_v1",
        scoring_version="1.0.0",
        symbol="600519",
        pool_version="hs300-2026-08",
        bar_closed_at=NOW,
        index_score=31,
        sentiment_score=22,
        theme_score=14,
        stock_score=8,
        total_score=75,
        consecutive_pass_count=2,
        input_hash="b" * 64,
        status=TopDownScoreStatus.ELIGIBLE_FOR_RISK,
    )
    first_id = store.add_topdown_score(score)
    second_id = store.add_topdown_score(score)
    assert first_id == second_id
    latest = store.latest_topdown_score("600519")
    assert latest is not None
    assert latest["snapshot"]["total_score"] == 75

    current = score.model_copy(update={
        "strategy_version": "cloud_ai_topdown_4321_intraday_v1",
        "scoring_version": "1.1.0",
        "bar_closed_at": "2026-08-12T10:15:00+08:00",
        "input_hash": "c" * 64,
    })
    store.add_topdown_score(current)
    filtered = store.latest_topdown_score(
        "600519",
        strategy_version="hs300_topdown_4321_intraday_v1",
        scoring_version="1.0.0",
    )
    assert filtered is not None
    assert filtered["snapshot"]["bar_closed_at"] == NOW
    current_rows = store.list_topdown_scores(
        strategy_version="cloud_ai_topdown_4321_intraday_v1",
        scoring_version="1.1.0",
    )
    assert [item["snapshot"]["input_hash"] for item in current_rows] == ["c" * 64]

    manual = current.model_copy(update={
        "pool_version": "manual-exception-hs300-2026-08-2026-08-12-600519",
        "bar_closed_at": "2026-08-12T10:30:00+08:00",
        "input_hash": "d" * 64,
    })
    store.add_topdown_score(manual)
    base_only = store.latest_topdown_score(
        "600519",
        strategy_version="cloud_ai_topdown_4321_intraday_v1",
        scoring_version="1.1.0",
        pool_version="hs300-2026-08",
    )
    assert base_only is not None
    assert base_only["snapshot"]["input_hash"] == "c" * 64
    manual_only = store.latest_topdown_score(
        "600519",
        strategy_version="cloud_ai_topdown_4321_intraday_v1",
        scoring_version="1.1.0",
        pool_version=manual.pool_version,
    )
    assert manual_only is not None
    assert manual_only["snapshot"]["input_hash"] == "d" * 64

    hotspot = HotspotSnapshot(
        symbol="600519",
        captured_at=NOW,
        frozen_at=NOW,
        industries=["白酒"],
    ).with_source_hash()
    hotspot_id = store.add_hotspot_snapshot(hotspot)
    assert store.add_hotspot_snapshot(hotspot) == hotspot_id
    latest_hotspot = store.latest_hotspot_snapshot("600519")
    assert latest_hotspot is not None
    assert latest_hotspot["snapshot"]["industries"] == ["白酒"]

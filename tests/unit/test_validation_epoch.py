from datetime import date, datetime, timedelta, timezone

import pytest

from pa_agent.trading.hotspots import (
    ANNOUNCEMENT_WINDOW_DAYS,
    HOTSPOT_RULE_VERSION,
    NEWS_WINDOW_DAYS,
)
from pa_agent.trading.oos_observations import OosObservationRecorder
from pa_agent.trading.promotion import StrategyPromotionService
from pa_agent.trading.store import TradeStore
from pa_agent.trading.topdown import TOPDOWN_STRATEGY_ID, HotspotSnapshot
from pa_agent.trading.universe import (
    CurrentUniverseMember,
    UniverseSnapshot,
)
from pa_agent.trading.validation_epoch import ValidationEpochRegistry

TZ8 = timezone(timedelta(hours=8))


def _snapshot(version: str, symbols: list[str], member_hash: str) -> UniverseSnapshot:
    return UniverseSnapshot(
        as_of=date(2026, 8, 14),
        version=version,
        symbols=symbols,
        members=[
            CurrentUniverseMember(
                rank=index,
                symbol=symbol,
                name=symbol,
                average_amount_20=1_000_000,
                authorization_eligible=True,
            )
            for index, symbol in enumerate(symbols, 1)
        ],
        source_kind="user_managed_a_share_universe",
        source_hash=member_hash,
        member_hash=member_hash,
        data_complete=True,
    )


def test_refresh_keeps_epoch_but_member_change_creates_isolated_epoch(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    now = datetime(2026, 8, 14, 16, 0, tzinfo=TZ8)
    registry = ValidationEpochRegistry(store, clock=lambda: now)
    first = registry.activate(_snapshot("pool-v1", ["600519"], "1" * 64))

    refreshed = registry.activate(_snapshot("pool-v2", ["600519"], "1" * 64))
    assert refreshed.epoch_id == first.epoch_id
    assert refreshed.pool_version == "pool-v2"
    assert refreshed.pool_versions == ["pool-v1", "pool-v2"]

    changed = registry.activate(
        _snapshot("pool-v3", ["600519", "300750"], "2" * 64),
        activated_at=now + timedelta(minutes=1),
    )
    assert changed.epoch_id != first.epoch_id
    assert changed.observation_strategy_version != first.observation_strategy_version
    epochs = store.list_validation_epochs()
    assert len(epochs) == 2
    assert epochs[0]["is_current"] is True
    assert epochs[1]["is_current"] is False


def test_observations_are_written_only_to_current_pool_epoch(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    registry = ValidationEpochRegistry(store)
    first = registry.activate(
        _snapshot("pool-v1", ["600519"], "3" * 64),
        activated_at=datetime(2026, 8, 14, 16, 0, tzinfo=TZ8),
    )
    recorder = OosObservationRecorder(store, validation_epochs=registry)
    recorder.record_strategy_definition()

    second = registry.activate(
        _snapshot("pool-v2", ["300750"], "4" * 64),
        activated_at=datetime(2026, 8, 14, 16, 5, tzinfo=TZ8),
    )
    recorder.record_strategy_definition()

    old_rows = store.list_oos_observations(
        strategy_version=first.observation_strategy_version,
        kind="historical_constituents",
    )
    new_rows = store.list_oos_observations(
        strategy_version=second.observation_strategy_version,
        kind="historical_constituents",
    )
    assert old_rows[0]["payload"]["symbols"] == ["600519"]
    assert new_rows[0]["payload"]["symbols"] == ["300750"]
    assert old_rows[0]["payload"]["validation_epoch_id"] == first.epoch_id
    assert new_rows[0]["payload"]["validation_epoch_id"] == second.epoch_id


def test_promotion_rejects_report_from_previous_private_pool_epoch(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    registry = ValidationEpochRegistry(store)
    current = registry.activate(
        _snapshot("pool-current", ["600519"], "5" * 64),
        activated_at=datetime(2026, 8, 14, 16, 0, tzinfo=TZ8),
    )
    service = StrategyPromotionService(store, validation_epochs=registry)
    report = {
        "strategy_version": TOPDOWN_STRATEGY_ID,
        "validation_epoch_id": "old-epoch",
        "pool_version": current.pool_version,
        "member_hash": current.member_hash,
    }
    with pytest.raises(ValueError, match="validation epoch"):
        service.record_out_of_sample_report(report)


def _hotspot(
    symbol: str,
    captured_at: datetime,
    *,
    frozen_at: datetime | None = None,
) -> HotspotSnapshot:
    return HotspotSnapshot(
        symbol=symbol,
        captured_at=captured_at.isoformat(),
        frozen_at=(frozen_at or captured_at).isoformat(),
        board_strength={"flows": [{"pct_chg": 1.0, "main_net_pct": 2.0}]},
        rule_version=HOTSPOT_RULE_VERSION,
        effective_windows_days={
            "announcement": ANNOUNCEMENT_WINDOW_DAYS,
            "news": NEWS_WINDOW_DAYS,
        },
    ).with_source_hash()


def test_hotspots_follow_current_epoch_and_allow_same_member_refresh_aliases(
    tmp_path,
) -> None:
    store = TradeStore(tmp_path / "trades.db")
    registry = ValidationEpochRegistry(store)
    activated = datetime(2026, 8, 14, 16, 0, tzinfo=TZ8)
    first = registry.activate(
        _snapshot("pool-v1", ["600519"], "6" * 64), activated_at=activated
    )
    recorder = OosObservationRecorder(store, validation_epochs=registry)

    pre_epoch = registry.bind_hotspot(_hotspot("600519", activated - timedelta(minutes=1)))
    assert pre_epoch.validation_epoch_id == ""
    assert recorder.record_hotspot(pre_epoch) is None

    late_response_from_pre_epoch_freeze = registry.bind_hotspot(
        _hotspot(
            "600519",
            activated + timedelta(minutes=1),
            frozen_at=activated - timedelta(minutes=1),
        )
    )
    assert late_response_from_pre_epoch_freeze.validation_epoch_id == ""
    assert "hotspot_validation_epoch_time_invalid" in (
        late_response_from_pre_epoch_freeze.data_gaps
    )

    first_hotspot = registry.bind_hotspot(
        _hotspot("600519", activated + timedelta(minutes=1))
    )
    assert first_hotspot.validation_epoch_id == first.epoch_id
    assert first_hotspot.pool_version == "pool-v1"
    assert recorder.record_hotspot(first_hotspot)

    stale_request = registry.bind_hotspot(
        _hotspot("600519", activated + timedelta(minutes=1, seconds=1)),
        expected_epoch_id="previous-epoch",
        expected_pool_version="pool-v0",
    )
    assert stale_request.validation_epoch_id == ""
    assert "hotspot_request_validation_epoch_mismatch" in stale_request.data_gaps

    refreshed = registry.activate(_snapshot("pool-v2", ["600519"], "6" * 64))
    second_hotspot = registry.bind_hotspot(
        _hotspot("600519", activated + timedelta(minutes=2))
    )
    assert refreshed.epoch_id == first.epoch_id
    assert second_hotspot.pool_version == "pool-v2"
    assert recorder.record_hotspot(second_hotspot)

    changed = registry.activate(
        _snapshot("pool-v3", ["300750"], "7" * 64),
        activated_at=activated + timedelta(minutes=3),
    )
    assert changed.epoch_id != first.epoch_id
    assert recorder.record_hotspot(second_hotspot) is None

    rows = store.list_oos_observations(
        strategy_version=first.observation_strategy_version,
        kind="hotspots",
        limit=10,
    )
    assert {row["payload"]["pool_version"] for row in rows} == {"pool-v1", "pool-v2"}
    assert {row["payload"]["validation_epoch_id"] for row in rows} == {first.epoch_id}

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from pa_agent.trading.broker_models import BrokerPosition
from pa_agent.trading.models import AssetClass, PlanStatus, TradePlan
from pa_agent.trading.store import TradeStore
from pa_agent.trading.topdown import TOPDOWN_STRATEGY_ID
from pa_agent.trading.universe import (
    CurrentUniverseMember,
    ManagedAshareUniverseService,
    UniverseMutationBlocked,
    UniverseSnapshot,
)

NOW = datetime.fromisoformat("2026-08-14T16:30:00+08:00")


def _bars(symbol: str, **_kwargs) -> list[dict]:
    start = datetime.fromisoformat("2026-07-01T15:00:00+08:00")
    return [
        {
            "time": start + timedelta(days=offset),
            "open": 10.0,
            "high": 10.2,
            "low": 9.8,
            "close": 10.0 + offset / 100,
            "amount": 1_000_000 + offset,
            "pct_chg": 0.1,
        }
        for offset in range(25)
    ]


def _profile(symbol: str) -> dict:
    names = {
        "600519": "贵州茅台",
        "600941": "中国移动",
        "300750": "宁德时代",
    }
    return {
        "symbol": symbol,
        "name": names.get(symbol, "测试股份"),
        "listing_date": "20010101",
        "industry": "测试行业",
    }


def _seed(store: TradeStore, symbols: list[str] | None = None) -> UniverseSnapshot:
    symbols = symbols or ["600519"]
    snapshot = UniverseSnapshot(
        as_of=date(2026, 8, 13),
        version="cloud_ai_11_v1-2026-08",
        symbols=symbols,
        members=[
            CurrentUniverseMember(
                rank=rank,
                symbol=symbol,
                name=_profile(symbol)["name"],
                average_amount_20=1_000_000,
                listing_date=date(2001, 1, 1),
            )
            for rank, symbol in enumerate(symbols, 1)
        ],
        source_kind="user_fixed_theme_universe",
        source_as_of=date(2026, 8, 13),
        input_member_count=len(symbols),
    )
    store.upsert_universe_snapshot(snapshot, source_updated_at="2026-08-13")
    return snapshot


def _service(store: TradeStore, *, daily_loader=_bars) -> ManagedAshareUniverseService:
    return ManagedAshareUniverseService(
        store,
        daily_loader=daily_loader,
        profile_loader=_profile,
        now=lambda: NOW,
        max_workers=1,
    )


def test_add_member_creates_new_version_and_preserves_old_snapshot(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    old = _seed(store)
    service = _service(store)

    result = service.add_member("600941")

    assert result.previous_version == old.version
    assert result.snapshot.symbols == ["600519", "600941"]
    assert result.snapshot.parent_version == old.version
    assert result.snapshot.change_kind == "add"
    assert result.snapshot.change_symbol == "600941"
    assert result.snapshot.member_hash
    assert result.snapshot.version.startswith("ashare_private_pool-")
    snapshots = store.list_universe_snapshots(limit=10)
    assert len(snapshots) == 2
    assert snapshots[0]["version"] == result.snapshot.version
    assert snapshots[1]["version"] == old.version
    assert store.current_strategy_state(TOPDOWN_STRATEGY_ID) == "candidate"
    transitions = store.list_strategy_transitions(strategy_id=TOPDOWN_STRATEGY_ID)
    assert transitions[0]["reasons"][0] == (
        "universe_revision_requires_new_oos_and_shadow_validation"
    )


def test_add_rejects_non_a_share_duplicate_and_incomplete_data(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    _seed(store)
    service = _service(store)

    with pytest.raises(ValueError, match="6位A股"):
        service.add_member("00700")
    with pytest.raises(ValueError, match="已在当前股票池"):
        service.add_member("600519")

    incomplete = _service(store, daily_loader=lambda *_args, **_kwargs: [])
    with pytest.raises(ValueError, match="20日行情"):
        incomplete.add_member("300750")
    assert store.list_universe_snapshots(limit=10)[0]["snapshot"]["symbols"] == [
        "600519"
    ]


def test_remove_creates_revision_but_open_position_or_plan_blocks(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    _seed(store, ["600519", "600941"])
    service = _service(store)
    broker = {
        "positions": [
            BrokerPosition(
                symbol="600941",
                name="中国移动",
                quantity=100,
                sellable_quantity=100,
                cost_price=100,
                last_price=101,
                market_value=10_100,
            ).model_dump(mode="json")
        ],
        "orders": [],
    }

    with pytest.raises(UniverseMutationBlocked, match="持仓与退出"):
        service.remove_member("600941", broker_snapshot=broker)

    decision = store.add_decision(
        symbol="600941",
        timeframe="15m",
        asset_class="a_share",
        original_decision={},
        final_decision={},
        meta={},
    )
    store.add_plan(
        TradePlan(
            id="open-plan",
            decision_event_id=decision,
            symbol="600941",
            timeframe="15m",
            asset_class=AssetClass.A_SHARE,
            direction="buy",
            order_type="limit",
            entry_price=100,
            stop_loss_price=95,
            take_profit_price=110,
            status=PlanStatus.PROPOSED,
            strategy_version=TOPDOWN_STRATEGY_ID,
        )
    )
    with pytest.raises(UniverseMutationBlocked, match="开放交易计划"):
        service.remove_member("600941", broker_snapshot={"positions": [], "orders": []})

    store.update_plan("open-plan", status="ignored", shadow_status="closed")
    result = service.remove_member(
        "600941", broker_snapshot={"positions": [], "orders": []}
    )
    assert result.snapshot.symbols == ["600519"]
    assert result.snapshot.change_kind == "remove"
    assert store.get_plan("open-plan") is not None


def test_managed_current_version_is_not_replaced_by_fixed_seed(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    _seed(store)
    service = _service(store)
    changed = service.add_member("600941").snapshot

    assert service.current_version(NOW) == changed.version
    refreshed = service.generate(as_of=NOW)
    assert refreshed.source_kind == "user_managed_a_share_universe"
    assert refreshed.parent_version == changed.version
    assert refreshed.symbols == changed.symbols
    assert refreshed.version != changed.version


def test_managed_snapshot_cannot_be_overwritten_in_place(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    _seed(store)
    snapshot = _service(store).add_member("600941").snapshot

    changed = snapshot.model_copy(update={"change_summary": "试图覆盖历史"})
    with pytest.raises(ValueError, match="不可原地覆盖"):
        store.upsert_universe_snapshot(changed)

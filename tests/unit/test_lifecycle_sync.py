from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from pa_agent.trading.lifecycle import TradeLifecycleTracker
from pa_agent.trading.lifecycle_sync import LifecycleMarketDataSync
from pa_agent.trading.models import AssetClass, PlanStatus, TradePlan
from pa_agent.trading.store import TradeStore


def _plan(store: TradeStore, created: datetime) -> TradePlan:
    decision_id = store.add_decision(
        symbol="600519",
        timeframe="15m",
        asset_class=AssetClass.A_SHARE.value,
        original_decision={},
        final_decision={},
        meta={},
    )
    plan = TradePlan(
        id=uuid.uuid4().hex,
        decision_event_id=decision_id,
        symbol="600519",
        timeframe="15m",
        asset_class=AssetClass.A_SHARE,
        direction="buy",
        order_type="limit",
        entry_price=100,
        stop_loss_price=95,
        take_profit_price=110,
        status=PlanStatus.PROPOSED,
        shadow_status="open",
        strategy_version="hs300_topdown_4321_intraday_v1",
        created_at=created.isoformat(),
        risk_snapshot={
            "management_timeframe": "1d",
            "daily_condition_snapshot": {"atr14": 2},
            "exit_rules": {
                "breakeven_after_r": 1,
                "trailing_atr": 2,
                "time_stop_bars": 10,
                "time_stop_min_r": 0.5,
            },
        },
    )
    store.add_plan(plan)
    store.update_plan(
        plan.id,
        shadow_entry_price=100,
        shadow_opened_at=created.isoformat(),
        shadow_active_stop=95,
        shadow_highest_close=100,
    )
    return plan


def test_daily_lifecycle_sync_excludes_forming_day_and_is_restart_idempotent(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    tz = timezone(timedelta(hours=8))
    created = datetime(2026, 8, 10, 10, tzinfo=tz)
    plan = _plan(store, created)
    rows = [
        {
            "time": datetime(2026, 8, 11),
            "open": 100,
            "high": 103,
            "low": 99,
            "close": 101,
            "volume": 1_000,
            "amount": 100_000,
        },
        {
            "time": datetime(2026, 8, 12),
            "open": 101,
            "high": 104,
            "low": 100,
            "close": 102,
            "volume": 1_000,
            "amount": 100_000,
        },
    ]
    tracker = TradeLifecycleTracker(store)
    first = LifecycleMarketDataSync(
        store, tracker, daily_loader=lambda *_args, **_kwargs: rows
    )

    result = first.sync_open_daily(
        now=datetime(2026, 8, 12, 10, tzinfo=tz)
    )
    assert result["symbols"] == ["600519"]
    assert store.get_plan(plan.id)["shadow_holding_bars"] == 1

    restarted = LifecycleMarketDataSync(
        store,
        TradeLifecycleTracker(TradeStore(store.db_path)),
        daily_loader=lambda *_args, **_kwargs: rows,
    )
    restarted.sync_open_daily(now=datetime(2026, 8, 12, 16, tzinfo=tz))
    assert store.get_plan(plan.id)["shadow_holding_bars"] == 2
    restarted.sync_open_daily(now=datetime(2026, 8, 12, 16, tzinfo=tz))
    assert store.get_plan(plan.id)["shadow_holding_bars"] == 2

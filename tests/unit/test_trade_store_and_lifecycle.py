from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest

from pa_agent.trading.lifecycle import TradeLifecycleTracker
from pa_agent.trading.models import AssetClass, Execution, PlanStatus, TradePlan
from pa_agent.trading.store import TradeStore


@dataclass
class Bar:
    ts_open: float
    low: float
    high: float
    open: float | None = None
    close_value: float | None = None

    @property
    def close(self) -> float:
        return self.close_value if self.close_value is not None else (self.low + self.high) / 2


def _store(tmp_path) -> TradeStore:
    return TradeStore(tmp_path / "trade_records" / "trades.db")


def _plan(
    store: TradeStore,
    *,
    asset=AssetClass.UNKNOWN,
    risk_snapshot: dict | None = None,
) -> TradePlan:
    decision_id = store.add_decision(
        symbol="600519" if asset is AssetClass.A_SHARE else "X", timeframe="1d",
        asset_class=asset.value, original_decision={}, final_decision={}, meta={},
    )
    created = (datetime.now().astimezone() - timedelta(days=2)).isoformat()
    plan = TradePlan(
        id=uuid.uuid4().hex, decision_event_id=decision_id,
        symbol="600519" if asset is AssetClass.A_SHARE else "X", timeframe="1d",
        asset_class=asset, direction="做多", order_type="限价单", entry_price=100,
        stop_loss_price=95, take_profit_price=110, status=PlanStatus.PROPOSED,
        created_at=created,
        risk_snapshot=risk_snapshot or {},
    )
    store.add_plan(plan)
    return plan


def _managed_plan(store: TradeStore, *, asset=AssetClass.A_SHARE) -> TradePlan:
    return _plan(
        store,
        asset=asset,
        risk_snapshot={
            "max_entry_price": 103.0,
            "daily_condition_snapshot": {"atr14": 2.0},
            "exit_rules": {
                "breakeven_after_r": 1.0,
                "trailing_atr": 2.0,
                "time_stop_bars": 10,
                "time_stop_min_r": 0.5,
                "t_plus_one": True,
            },
        },
    )


def test_shadow_gap_entry_uses_open_and_above_max_entry_invalidates(tmp_path) -> None:
    store = _store(tmp_path)
    filled_plan = _managed_plan(store)
    tracker = TradeLifecycleTracker(store)
    start = datetime.now().astimezone() + timedelta(days=1)

    tracker.process_closed_bar(
        symbol="600519",
        timeframe="1d",
        bar=Bar(start.timestamp(), low=100.5, high=104, open=102, close_value=103),
    )
    assert store.get_plan(filled_plan.id)["shadow_entry_price"] == 102

    store.update_plan(
        filled_plan.id,
        shadow_status="closed",
    )
    another = _managed_plan(store)
    tracker.process_closed_bar(
        symbol="600519",
        timeframe="1d",
        bar=Bar(
            (start + timedelta(days=1)).timestamp(),
            low=103.5,
            high=106,
            open=104,
            close_value=105,
        ),
    )
    assert store.get_plan(another.id)["shadow_status"] == "invalidated"
    assert any(
        event["event_type"] == "gap_above_max_entry"
        for event in store.list_events(another.id)
    )


def test_store_uses_wal_and_explicit_dataset_statistics(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.available
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert store.statistics(dataset="actual")["dataset"] == "actual"
    assert store.statistics(dataset="shadow")["dataset"] == "shadow"


def test_shadow_same_bar_stop_wins(tmp_path) -> None:
    store = _store(tmp_path); _plan(store)
    tracker = TradeLifecycleTracker(store)
    events = tracker.process_closed_bar(
        symbol="X", timeframe="1d",
        bar=Bar(datetime.now().timestamp(), low=94, high=111),
    )
    result = store.list_results(dataset="shadow")[0]
    assert result["outcome"] == "loss"
    assert result["ambiguous_same_bar"] == 1
    assert any(event["event_type"] == "stop_detected" for event in events)
    output = store.export_csv(tmp_path / "shadow.csv", dataset="shadow", symbol="X")
    assert output.read_bytes().startswith(b"\xef\xbb\xbf")
    assert store.statistics(dataset="shadow", symbol="X")["result_count"] == 1


def test_shadow_managed_exit_raises_stop_only_after_closed_bar(tmp_path) -> None:
    store = _store(tmp_path)
    plan = _managed_plan(store)
    tracker = TradeLifecycleTracker(store)
    start = datetime.now().astimezone() + timedelta(days=1)

    tracker.process_closed_bar(
        symbol="600519",
        timeframe="1d",
        bar=Bar(start.timestamp(), low=99, high=102, open=100, close_value=101),
    )
    tracker.process_closed_bar(
        symbol="600519",
        timeframe="1d",
        bar=Bar(
            (start + timedelta(days=1)).timestamp(),
            low=99.5,
            high=106,
            open=101,
            close_value=105,
        ),
    )

    updated = store.get_plan(plan.id)
    assert updated["shadow_status"] == "open"
    assert updated["shadow_active_stop"] == 101
    assert updated["shadow_highest_close"] == 105
    assert store.list_results(dataset="shadow") == []

    tracker.process_closed_bar(
        symbol="600519",
        timeframe="1d",
        bar=Bar(
            (start + timedelta(days=2)).timestamp(),
            low=100.5,
            high=103,
            open=102,
            close_value=101,
        ),
    )
    result = store.list_results(dataset="shadow")[0]
    assert result["exit_price"] == 101
    assert any(
        event["event_type"] == "trailing_stop_detected"
        for event in store.list_events(plan.id)
    )


def test_shadow_time_exit_is_scheduled_then_filled_at_next_open(tmp_path) -> None:
    store = _store(tmp_path)
    plan = _managed_plan(store)
    tracker = TradeLifecycleTracker(store)
    start = datetime.now().astimezone() + timedelta(days=1)

    for offset in range(10):
        tracker.process_closed_bar(
            symbol="600519",
            timeframe="1d",
            bar=Bar(
                (start + timedelta(days=offset)).timestamp(),
                low=99,
                high=102,
                open=100,
                close_value=101,
            ),
        )

    scheduled = store.get_plan(plan.id)
    assert scheduled["shadow_status"] == "open"
    assert scheduled["shadow_time_exit_pending"] == 1
    assert any(
        event["event_type"] == "time_exit_scheduled"
        for event in store.list_events(plan.id)
    )

    tracker.process_closed_bar(
        symbol="600519",
        timeframe="1d",
        bar=Bar(
            (start + timedelta(days=10)).timestamp(),
            low=98,
            high=101,
            open=99,
            close_value=100,
        ),
    )
    result = store.list_results(dataset="shadow")[0]
    assert result["exit_price"] == 99
    assert result["holding_bars"] == 10
    assert any(
        event["event_type"] == "time_exit_filled"
        for event in store.list_events(plan.id)
    )


def test_actual_managed_exit_updates_protection_then_waits_for_user_exit(tmp_path) -> None:
    store = _store(tmp_path)
    plan = _managed_plan(store)
    tracker = TradeLifecycleTracker(store)
    start = datetime.now().astimezone() + timedelta(days=1)
    store.confirm_execution(Execution(
        id=uuid.uuid4().hex,
        plan_id=plan.id,
        executed_at=start.isoformat(),
        price=100,
        quantity=100,
        fees=5,
    ))

    tracker.process_closed_bar(
        symbol="600519",
        timeframe="1d",
        bar=Bar(start.timestamp(), low=99, high=106, open=100, close_value=105),
    )
    protected = store.get_plan(plan.id)
    assert protected["status"] == "executed_open"
    assert protected["actual_active_stop"] == 101
    assert any(
        event["event_type"] == "actual_protective_stop_updated"
        for event in store.list_events(plan.id)
    )

    tracker.process_closed_bar(
        symbol="600519",
        timeframe="1d",
        bar=Bar(
            (start + timedelta(days=1)).timestamp(),
            low=100.5,
            high=103,
            open=102,
            close_value=101,
        ),
    )
    detected = store.get_plan(plan.id)
    assert detected["status"] == "exit_detected"
    assert any(
        event["event_type"] == "trailing_stop_detected"
        and event["dataset"] == "actual"
        and event["details"]["requires_user_exit_confirmation"]
        for event in store.list_events(plan.id)
    )
    assert store.list_results(dataset="actual") == []


def test_topdown_plan_uses_15m_for_entry_and_daily_bars_for_management(tmp_path) -> None:
    store = _store(tmp_path)
    decision_id = store.add_decision(
        symbol="600519",
        timeframe="15m",
        asset_class=AssetClass.A_SHARE.value,
        original_decision={},
        final_decision={},
        meta={"strategy_version": "hs300_topdown_4321_intraday_v1"},
    )
    created = datetime.now().astimezone() - timedelta(days=1)
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
        shadow_status="proposed",
        strategy_version="hs300_topdown_4321_intraday_v1",
        created_at=created.isoformat(),
        risk_snapshot={
            "max_entry_price": 103,
            "management_timeframe": "1d",
            "daily_condition_snapshot": {"atr14": 2},
            "exit_rules": {
                "breakeven_after_r": 1,
                "trailing_atr": 2,
                "time_stop_bars": 10,
                "time_stop_min_r": 0.5,
                "t_plus_one": True,
            },
        },
    )
    store.add_plan(plan)
    tracker = TradeLifecycleTracker(store)

    tracker.process_closed_bar(
        symbol="600519",
        timeframe="15m",
        bar=Bar(
            (created + timedelta(hours=1)).timestamp(),
            low=99,
            high=102,
            open=100,
            close_value=101,
        ),
    )
    assert store.get_plan(plan.id)["shadow_status"] == "open"
    assert store.get_plan(plan.id)["shadow_holding_bars"] == 0

    tracker.process_closed_bar(
        symbol="600519",
        timeframe="15m",
        bar=Bar(
            (created + timedelta(hours=1, minutes=15)).timestamp(),
            low=94,
            high=106,
            open=101,
            close_value=100,
        ),
    )
    assert store.get_plan(plan.id)["shadow_status"] == "open"
    assert store.get_plan(plan.id)["shadow_holding_bars"] == 0
    assert any(
        event["event_type"] == "t1_locked_breach"
        and event["dataset"] == "shadow"
        for event in store.list_events(plan.id)
    )

    tracker.process_closed_bar(
        symbol="600519",
        timeframe="1d",
        bar=Bar(
            (created + timedelta(days=1)).timestamp(),
            low=96,
            high=104,
            open=101,
            close_value=102,
        ),
    )
    assert store.get_plan(plan.id)["shadow_holding_bars"] == 1

    tracker.process_closed_bar(
        symbol="600519",
        timeframe="1d",
        bar=Bar(
            (created + timedelta(days=1)).timestamp(),
            low=96,
            high=104,
            open=101,
            close_value=102,
        ),
    )
    assert store.get_plan(plan.id)["shadow_holding_bars"] == 1


def test_a_share_t1_records_locked_breach(tmp_path) -> None:
    store = _store(tmp_path); plan = _plan(store, asset=AssetClass.A_SHARE)
    now = datetime.now().astimezone()
    store.confirm_execution(Execution(
        id=uuid.uuid4().hex, plan_id=plan.id, executed_at=now.isoformat(),
        price=100, quantity=100,
    ))
    tracker = TradeLifecycleTracker(store)
    tracker.process_closed_bar(
        symbol="600519", timeframe="1d",
        bar=Bar(now.timestamp(), low=94, high=101),
    )
    assert store.get_plan(plan.id)["status"] == "executed_open"
    assert any(event["event_type"] == "t1_locked_breach" for event in store.list_events(plan.id))
    next_day = now + timedelta(days=1)
    tracker.process_closed_bar(
        symbol="600519", timeframe="1d",
        bar=Bar(next_day.timestamp(), low=94, high=103),
    )
    assert store.get_plan(plan.id)["status"] == "exit_detected"
    store.confirm_exit(
        plan.id, exited_at=next_day.isoformat(), exit_price=94.5, exit_fees=5,
    )
    actual = store.list_results(dataset="actual")[0]
    assert actual["holding_bars"] == 2
    assert actual["mae_r"] is not None


def test_continuous_futures_cannot_confirm_execution(tmp_path) -> None:
    store = _store(tmp_path); plan = _plan(store, asset=AssetClass.CN_FUTURES)
    with pytest.raises(ValueError, match="真实合约"):
        store.confirm_execution(Execution(
            id=uuid.uuid4().hex, plan_id=plan.id,
            executed_at=datetime.now().astimezone().isoformat(), price=100,
            quantity=1, real_contract="AU0",
        ))


def test_legacy_csv_import_is_hash_idempotent_and_keeps_result_unknown(tmp_path) -> None:
    legacy = tmp_path / "trade_records"; legacy.mkdir()
    csv_path = legacy / "600519_1d.csv"
    csv_path.write_text(
        "record_time,symbol,timeframe,order_direction,order_type,entry_price,stop_loss_price,take_profit_price\n"
        "2026-01-01T10:00:00+08:00,600519,1d,做多,限价单,100,95,110\n",
        encoding="utf-8",
    )
    store = TradeStore(legacy / "trades.db", legacy_dir=legacy)
    assert len(store.list_plans()) == 1
    assert store.list_results() == []
    assert store.import_legacy_csvs() == 0
    assert len(store.list_plans()) == 1

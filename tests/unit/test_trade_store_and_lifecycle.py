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

    @property
    def close(self) -> float:
        return (self.low + self.high) / 2


def _store(tmp_path) -> TradeStore:
    return TradeStore(tmp_path / "trade_records" / "trades.db")


def _plan(store: TradeStore, *, asset=AssetClass.UNKNOWN) -> TradePlan:
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
    )
    store.add_plan(plan)
    return plan


def test_store_uses_wal_and_explicit_dataset_statistics(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.available
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert store.statistics(dataset="actual")["dataset"] == "actual"
    assert store.statistics(dataset="shadow")["dataset"] == "shadow"


def test_shadow_same_bar_stop_wins(tmp_path) -> None:
    store = _store(tmp_path); plan = _plan(store)
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

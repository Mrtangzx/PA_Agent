from __future__ import annotations

from types import SimpleNamespace

from pa_agent.gui.trade_ledger_window import TradeLedgerWindow
from pa_agent.trading.hotspots import classify_major_negative
from pa_agent.trading.models import AssetClass, PlanStatus, TradePlan
from pa_agent.trading.store import TradeStore
from pa_agent.trading.topdown import HotspotSnapshot

NOW = "2026-08-12T10:00:00+08:00"


def test_major_negative_snapshot_invalidates_unexecuted_plan(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    decision_id = store.add_decision(
        symbol="600519",
        timeframe="15m",
        asset_class="a_share",
        original_decision={},
        final_decision={},
        meta={},
    )
    plan = TradePlan(
        id="p1",
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
    )
    store.add_plan(plan)
    snapshot = HotspotSnapshot(
        symbol="600519",
        captured_at=NOW,
        frozen_at=NOW,
        negative_blocks=[
            "major_negative_" + classify_major_negative("收到证监会立案告知书")
        ],
    ).with_source_hash()
    fake = SimpleNamespace(store=store)
    TradeLedgerWindow._store_hotspot_snapshot(fake, snapshot)
    assert store.get_plan("p1")["status"] == "invalidated"
    events = store.list_events("p1")
    assert events[-1]["event_type"] == "major_negative_invalidated"

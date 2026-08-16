from __future__ import annotations

from types import SimpleNamespace

from pa_agent.gui.trade_ledger_window import TradeLedgerWindow
from pa_agent.trading.broker_models import AuthorizedOrder, PrefillClearReceipt
from pa_agent.trading.hotspot_risk import apply_major_hotspot_risk
from pa_agent.trading.hotspots import classify_major_negative
from pa_agent.trading.models import AssetClass, PlanStatus, TradePlan
from pa_agent.trading.store import TradeStore
from pa_agent.trading.topdown import HotspotSnapshot

NOW = "2026-08-12T10:00:00+08:00"


class _Broker:
    def __init__(self) -> None:
        self.calls = 0

    def clear_prefill_if_matches(self, order: AuthorizedOrder) -> PrefillClearReceipt:
        self.calls += 1
        return PrefillClearReceipt(
            status="cleared",
            message="matched and cleared",
            verified_fields=order.model_dump(mode="json"),
            created_at=NOW,
        )


def _plan(store: TradeStore, *, plan_id: str, status: PlanStatus) -> TradePlan:
    decision_id = store.add_decision(
        symbol="600519", timeframe="15m", asset_class="a_share",
        original_decision={}, final_decision={}, meta={},
    )
    plan = TradePlan(
        id=plan_id, decision_event_id=decision_id, symbol="600519",
        timeframe="15m", asset_class=AssetClass.A_SHARE, direction="buy",
        order_type="limit", entry_price=100, stop_loss_price=95,
        take_profit_price=110, status=status,
    )
    store.add_plan(plan)
    return plan


def _snapshot() -> HotspotSnapshot:
    return HotspotSnapshot(
        symbol="600519", captured_at=NOW, frozen_at=NOW,
        negative_blocks=[
            "major_negative_" + classify_major_negative("收到证监会立案告知书")
        ],
    ).with_source_hash()


def test_major_negative_snapshot_invalidates_unexecuted_plan(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    _plan(store, plan_id="p1", status=PlanStatus.PROPOSED)
    snapshot = _snapshot()
    fake = SimpleNamespace(store=store)
    TradeLedgerWindow._store_hotspot_snapshot(fake, snapshot)
    assert store.get_plan("p1")["status"] == "invalidated"
    events = store.list_events("p1")
    assert events[-1]["event_type"] == "major_negative_invalidated"


def test_major_negative_clears_exact_prefill_and_invalidates_plan(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    _plan(store, plan_id="prefilled", status=PlanStatus.AWAITING_USER_CONFIRMATION)
    order = AuthorizedOrder(
        plan_id="prefilled", account_fingerprint="account", symbol="600519",
        name="贵州茅台", direction="buy", price=100, quantity=100,
        stop_loss_price=95, strategy_id="s", authorized_at=NOW, expires_at=NOW,
    )
    store.append_event(
        "prefilled", "awaiting_user_confirmation",
        details={"authorized_order": order.model_dump(mode="json")},
    )
    broker = _Broker()

    actions = apply_major_hotspot_risk(
        store=store, snapshot=_snapshot(), broker_adapter=broker
    )

    assert broker.calls == 1
    assert store.get_plan("prefilled")["status"] == "invalidated"
    assert actions[0]["prefill_clear"]["status"] == "cleared"
    assert store.list_events("prefilled")[-1]["event_type"] == "major_negative_invalidated"


def test_major_negative_does_not_claim_submitted_order_was_cancelled(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    _plan(store, plan_id="submitted", status=PlanStatus.SUBMITTED)

    first = apply_major_hotspot_risk(store=store, snapshot=_snapshot())
    second = apply_major_hotspot_risk(store=store, snapshot=_snapshot())

    assert store.get_plan("submitted")["status"] == "submitted"
    assert first[0]["event_type"] == "major_negative_action_required"
    assert second == []
    event = store.list_events("submitted")[-1]
    assert event["event_type"] == "major_negative_action_required"
    assert "真实委托/成交" in event["details"]["required_action"]

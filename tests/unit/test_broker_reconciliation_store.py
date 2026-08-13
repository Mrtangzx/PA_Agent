from datetime import UTC, datetime

from pa_agent.trading.broker_models import BrokerFill, ReconciliationResult
from pa_agent.trading.models import AssetClass, TradePlan
from pa_agent.trading.store import TradeStore

NOW = datetime.now(UTC).astimezone().isoformat()


def _plan() -> TradePlan:
    return TradePlan(
        id="plan-1", decision_event_id="decision-1", symbol="600519", timeframe="15m",
        asset_class=AssetClass.A_SHARE, direction="buy", order_type="limit",
        entry_price=100, stop_loss_price=95, take_profit_price=110,
        strategy_version="hs300_topdown_4321_intraday_v1",
    )


def _store(tmp_path) -> TradeStore:
    store = TradeStore(tmp_path / "trades.db")
    store.add_decision(
        decision_id="decision-1", symbol="600519", timeframe="15m",
        asset_class="a_share", original_decision={}, final_decision={}, meta={},
    )
    store.add_plan(_plan())
    return store


def test_broker_links_decode_fill_ids_and_real_fill_opens_plan(tmp_path) -> None:
    store = _store(tmp_path)
    reconciliation = ReconciliationResult(
        status="matched", plan_id="plan-1", matched_order_ids=["order-1"],
        matched_fill_ids=["fill-1"],
    )
    store.link_broker_order(reconciliation)
    fill = BrokerFill(
        broker_fill_id="fill-1", broker_order_id="order-1", symbol="600519",
        direction="buy", price=101, quantity=100, fees=5, filled_at=NOW,
    )
    store.upsert_broker_execution(
        plan_id="plan-1", fills=[fill], plan_status="executed_open",
        event_type="broker_filled", broker_order_id="order-1",
    )

    assert store.linked_broker_fill_ids() == {"fill-1"}
    assert store.get_plan("plan-1")["status"] == "executed_open"
    assert store.get_execution("plan-1")["price"] == 101
    assert [event["event_type"] for event in store.list_events("plan-1")].count(
        "broker_filled"
    ) == 1


def test_unmatched_fill_is_stored_once_outside_strategy_performance(tmp_path) -> None:
    store = _store(tmp_path)
    fill = BrokerFill(
        broker_fill_id="manual-1", symbol="000858", direction="buy", price=120,
        quantity=100, fees=5, filled_at=NOW,
    )
    assert store.add_external_broker_trade(fill, account_fingerprint="account")
    assert not store.add_external_broker_trade(fill, account_fingerprint="account")
    assert store.list_external_broker_trades()[0]["symbol"] == "000858"
    assert store.list_results(dataset="actual") == []

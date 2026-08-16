from datetime import UTC, datetime

import pytest

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
    store.link_broker_order(reconciliation, account_fingerprint="account-a")
    fill = BrokerFill(
        broker_fill_id="fill-1", broker_order_id="order-1", symbol="600519",
        direction="buy", price=101, quantity=100, fees=5, filled_at=NOW,
    )
    store.upsert_broker_execution(
        plan_id="plan-1", fills=[fill], plan_status="executed_open",
        event_type="broker_filled", broker_order_id="order-1",
        account_fingerprint="account-a",
    )

    assert store.linked_broker_fill_ids() == {"fill-1"}
    assert store.get_plan("plan-1")["status"] == "executed_open"
    assert store.get_execution("plan-1")["price"] == 101
    assert store.get_execution("plan-1")["account_fingerprint"] == "account-a"
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


def test_identical_broker_fill_ids_are_isolated_by_account(tmp_path) -> None:
    store = _store(tmp_path)
    fill = BrokerFill(
        broker_fill_id="same-id", symbol="000858", direction="buy", price=120,
        quantity=100, fees=5, filled_at=NOW,
    )

    assert store.add_external_broker_trade(fill, account_fingerprint="account-a")
    assert store.add_external_broker_trade(fill, account_fingerprint="account-b")

    rows = store.list_external_broker_trades()
    assert len(rows) == 2
    assert {row["account_fingerprint"] for row in rows} == {
        "account-a", "account-b",
    }


def test_broker_order_links_and_executions_are_isolated_by_account(tmp_path) -> None:
    store = _store(tmp_path)
    reconciliation = ReconciliationResult(
        status="matched", plan_id="plan-1", matched_order_ids=["same-order"],
        matched_fill_ids=["fill-a"],
    )
    store.link_broker_order(reconciliation, account_fingerprint="account-a")
    store.link_broker_order(
        reconciliation.model_copy(update={"matched_fill_ids": ["fill-b"]}),
        account_fingerprint="account-b",
    )

    assert len(store.list_broker_order_links(account_fingerprint="account-a")) == 1
    assert store.linked_broker_fill_ids(account_fingerprint="account-a") == {"fill-a"}
    assert store.linked_broker_fill_ids(account_fingerprint="account-b") == {"fill-b"}
    fill = BrokerFill(
        broker_fill_id="fill-a", broker_order_id="same-order", symbol="600519",
        direction="buy", price=101, quantity=100, fees=5, filled_at=NOW,
    )
    with pytest.raises(ValueError, match="account-scoped order link"):
        store.upsert_broker_execution(
            plan_id="plan-1", fills=[fill], plan_status="executed_open",
            event_type="broker_filled", broker_order_id="missing-order",
            account_fingerprint="account-a",
        )

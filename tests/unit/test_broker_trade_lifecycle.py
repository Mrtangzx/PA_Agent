from pa_agent.trading.trade_lifecycle import TradeLifecycle


def test_broker_order_status_covers_partial_filled_cancelled_and_rejected() -> None:
    assert TradeLifecycle.broker_order_status("已报", 0, 100) == (
        "submitted", "broker_submitted",
    )
    assert TradeLifecycle.broker_order_status("部成", 50, 100) == (
        "partially_filled", "broker_partial_fill",
    )
    assert TradeLifecycle.broker_order_status("已成", 100, 100) == (
        "filled", "broker_filled",
    )
    assert TradeLifecycle.broker_order_status("已撤", 0, 100) == (
        "cancelled", "broker_cancelled",
    )
    assert TradeLifecycle.broker_order_status("废单", 0, 100) == (
        "rejected", "broker_rejected",
    )

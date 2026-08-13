from datetime import UTC, datetime

from pa_agent.data.base import KlineBar
from pa_agent.trading.broker_models import AuthorizedOrder
from pa_agent.trading.execution_simulator import AShareCostModel, AShareExecutionSimulator


def _bar(open_: float, high: float, low: float) -> KlineBar:
    return KlineBar(
        seq=1, ts_open=datetime.now(UTC).timestamp() * 1000,
        open=open_, high=high, low=low, close=(high + low) / 2, volume=100,
    )


def test_gap_through_stop_uses_open_price_and_records_slippage() -> None:
    result = AShareExecutionSimulator().process_exit(
        entry_price=100, stop_price=95, target_price=110, quantity=100,
        bar=_bar(90, 94, 88),
    )
    assert result.price == 90
    assert result.slippage == 5


def test_same_bar_stop_wins_and_t1_blocks_exit() -> None:
    module = AShareExecutionSimulator()
    ambiguous = module.process_exit(
        entry_price=100, stop_price=95, target_price=110, quantity=100,
        bar=_bar(100, 111, 94),
    )
    assert ambiguous.price == 95
    assert ambiguous.ambiguous_same_bar
    locked = module.process_exit(
        entry_price=100, stop_price=95, target_price=110, quantity=100,
        bar=_bar(100, 111, 94), bought_same_day=True,
    )
    assert locked.t1_locked
    assert locked.status == "blocked"


def test_entry_requires_board_lot_and_limit_touch() -> None:
    order = AuthorizedOrder(
        plan_id="p", account_fingerprint="x", symbol="600519", direction="buy",
        price=100, quantity=101, stop_loss_price=95, strategy_id="s",
        authorized_at="2026-01-01T00:00:00+08:00", expires_at="2026-01-02T00:00:00+08:00",
    )
    result = AShareExecutionSimulator().process_entry(order, _bar(99, 101, 98))
    assert result.status == "blocked"
    assert result.reason == "board_lot_violation"


def test_gap_above_trigger_fills_at_open_when_below_maximum_price() -> None:
    order = AuthorizedOrder(
        plan_id="p", account_fingerprint="x", symbol="600519", direction="buy",
        price=100, quantity=100, stop_loss_price=95, strategy_id="s",
        authorized_at="2026-01-01T00:00:00+08:00",
        expires_at="2026-01-02T00:00:00+08:00",
    )

    filled = AShareExecutionSimulator().process_entry(
        order, _bar(102, 104, 101), max_price=103,
    )
    cancelled = AShareExecutionSimulator().process_entry(
        order, _bar(104, 105, 103), max_price=103,
    )

    assert filled.status == "filled"
    assert filled.price == 102
    assert filled.reason == "gap_open_fill"
    assert filled.slippage == 2
    assert cancelled.status == "blocked"
    assert cancelled.reason == "gap_above_max_entry"


def test_suspension_and_limit_lock_fail_closed() -> None:
    order = AuthorizedOrder(
        plan_id="p", account_fingerprint="x", symbol="600519", direction="buy",
        price=100, quantity=100, stop_loss_price=95, strategy_id="s",
        authorized_at="2026-01-01T00:00:00+08:00",
        expires_at="2026-01-02T00:00:00+08:00",
    )

    suspended = AShareExecutionSimulator().process_entry(
        order, _bar(99, 101, 98), suspended=True,
    )
    limit_locked = AShareExecutionSimulator().process_entry(
        order, _bar(99, 101, 98), limit_locked=True,
    )

    assert suspended.reason == "suspended"
    assert limit_locked.reason == "price_limit_locked"


def test_a_share_cost_model_applies_minimum_commission_and_sell_tax() -> None:
    costs = AShareCostModel(
        commission_rate=0.00025,
        minimum_commission=5,
        sell_tax_rate=0.0005,
    ).calculate(entry_price=10, exit_price=11, quantity=100)

    assert costs.buy_commission == 5
    assert costs.sell_commission == 5
    assert costs.sell_tax == 0.55
    assert costs.total == 10.55

from pa_agent.trading.broker_models import (
    BrokerCashFlow,
    BrokerConnectionStatus,
    BrokerFill,
    BrokerPosition,
    BrokerSnapshot,
    ConnectionState,
)
from pa_agent.trading.equity import (
    monthly_equity_peak_drawdown_pct,
    monthly_return_pct,
    portfolio_snapshot_from_store,
)
from pa_agent.trading.store import TradeStore


def test_monthly_return_removes_external_cash_flow() -> None:
    snapshots = [
        {"captured_at": "2026-08-01T15:00:00+08:00", "total_equity": 100_000, "external_cash_flow": 0},
        {"captured_at": "2026-08-10T15:00:00+08:00", "total_equity": 111_000, "external_cash_flow": 10_000},
        {"captured_at": "2026-08-31T15:00:00+08:00", "total_equity": 112_110, "external_cash_flow": 0},
    ]
    assert round(monthly_return_pct(snapshots), 4) == 2.01


def test_monthly_peak_drawdown_uses_worst_intramonth_trough_not_only_latest() -> None:
    snapshots = [
        {"captured_at": "2026-08-01T15:00:00+08:00", "total_equity": 100_000},
        {"captured_at": "2026-08-10T15:00:00+08:00", "total_equity": 110_000},
        {"captured_at": "2026-08-15T15:00:00+08:00", "total_equity": 99_000},
        {"captured_at": "2026-08-20T15:00:00+08:00", "total_equity": 108_000},
    ]
    assert monthly_equity_peak_drawdown_pct(snapshots) == 10.0


def test_portfolio_snapshot_fails_closed_without_monthly_equity_baseline(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    broker = BrokerSnapshot(
        connection=ConnectionState(
            status=BrokerConnectionStatus.CONNECTED,
            checked_at="2026-08-13T10:00:00+08:00",
        ),
        account_fingerprint="abc",
        total_equity=100_000,
        available_cash=100_000,
        captured_at="2026-08-13T10:00:00+08:00",
        complete=True,
    )

    result = portfolio_snapshot_from_store(store, broker)

    assert not result.data_complete
    assert "monthly_equity_baseline_incomplete" in result.data_gaps


def test_portfolio_snapshot_counts_new_positions_and_unexplained_holdings(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    base = {
        "account_fingerprint": "abc",
        "total_equity": 100_000,
        "available_cash": 80_000,
        "position_value": 20_000,
        "complete": True,
    }
    store.add_equity_snapshot({
        **base, "captured_at": "2026-08-01T15:00:00+08:00",
    })
    store.upsert_broker_cash_flows(
        "abc", [],
        captured_at="2026-08-13T10:00:00+08:00",
        range_start="2026-08-01T00:00:00+08:00",
        range_end="2026-08-13T10:00:00+08:00",
        complete=True,
    )
    store.add_equity_snapshot({
        **base, "total_equity": 101_000,
        "captured_at": "2026-08-13T10:00:00+08:00",
    })
    broker = BrokerSnapshot(
        connection=ConnectionState(
            status=BrokerConnectionStatus.CONNECTED,
            checked_at="2026-08-13T10:00:00+08:00",
        ),
        account_fingerprint="abc",
        total_equity=101_000,
        available_cash=80_000,
        position_value=21_000,
        positions=[BrokerPosition(
            symbol="600519", quantity=100, sellable_quantity=0, cost_price=100,
            last_price=101, market_value=10_100,
        )],
        fills=[BrokerFill(
            broker_fill_id="f1", symbol="600519", direction="buy", price=100,
            quantity=100, filled_at="2026-08-13T09:45:00+08:00",
        )],
        captured_at="2026-08-13T10:00:00+08:00",
        complete=True,
    )

    result = portfolio_snapshot_from_store(store, broker)

    assert result.data_complete
    assert round(result.monthly_return_pct, 6) == 1.0
    assert result.new_positions_today == 1
    assert result.unexplained_position_difference


def _financial_snapshot(
    *,
    account: str,
    captured_at: str,
    equity: float,
    flows: list[BrokerCashFlow],
    complete: bool = True,
) -> BrokerSnapshot:
    return BrokerSnapshot(
        connection=ConnectionState(
            status=BrokerConnectionStatus.CONNECTED,
            checked_at=captured_at,
        ),
        account_fingerprint=account,
        total_equity=equity,
        available_cash=equity,
        position_value=0,
        cash_flows=flows,
        cash_flow_complete=complete,
        cash_flow_range_start=captured_at[:8] + "01T00:00:00+08:00",
        cash_flow_range_end=captured_at,
        captured_at=captured_at,
        complete=True,
    )


def test_financial_snapshots_remove_deposit_and_withdrawal_once(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    first = _financial_snapshot(
        account="abc", captured_at="2026-08-01T09:00:00+08:00",
        equity=100_000, flows=[],
    )
    deposit = BrokerCashFlow(
        direction="deposit", amount=10_000,
        occurred_at="2026-08-05T10:00:00+08:00", description="银转证",
    )
    second = _financial_snapshot(
        account="abc", captured_at="2026-08-10T15:00:00+08:00",
        equity=111_000, flows=[deposit],
    )
    withdrawal = BrokerCashFlow(
        direction="withdrawal", amount=5_000,
        occurred_at="2026-08-15T10:00:00+08:00", description="证转银",
    )
    third = _financial_snapshot(
        account="abc", captured_at="2026-08-20T15:00:00+08:00",
        equity=107_110, flows=[deposit, withdrawal],
    )

    store.record_broker_financial_snapshot(first)
    store.record_broker_financial_snapshot(second)
    store.record_broker_financial_snapshot(third)
    equities = store.list_equity_snapshots(account_fingerprint="abc")

    assert [item["external_cash_flow"] for item in equities] == [0, 10_000, -5_000]
    assert round(monthly_return_pct(equities), 6) == 2.01


def test_cash_flow_sync_is_idempotent_without_broker_flow_id_and_account_isolated(
    tmp_path,
) -> None:
    store = TradeStore(tmp_path / "trades.db")
    flow = BrokerCashFlow(
        direction="deposit", amount=10_000,
        occurred_at="2026-08-05T10:00:00+08:00", description="银转证",
    )
    kwargs = {
        "captured_at": "2026-08-10T15:00:00+08:00",
        "range_start": "2026-08-01T00:00:00+08:00",
        "range_end": "2026-08-10T15:00:00+08:00",
        "complete": True,
    }
    store.upsert_broker_cash_flows("abc", [flow], **kwargs)
    store.upsert_broker_cash_flows("abc", [flow], **kwargs)
    store.upsert_broker_cash_flows("other", [flow], **kwargs)

    assert len(store.list_broker_cash_flows(account_fingerprint="abc")) == 1
    assert len(store.list_broker_cash_flows(account_fingerprint="other")) == 1
    assert store.cash_flow_between(
        "abc", after="2026-08-01T00:00:00+08:00",
        through="2026-08-10T15:00:00+08:00",
    ) == 10_000


def test_unverified_cash_flow_history_blocks_monthly_portfolio(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    for captured_at, equity in (
        ("2026-08-01T15:00:00+08:00", 100_000),
        ("2026-08-13T10:00:00+08:00", 101_000),
    ):
        store.add_equity_snapshot({
            "account_fingerprint": "abc", "captured_at": captured_at,
            "total_equity": equity, "available_cash": equity,
            "position_value": 0, "complete": True,
        })
    broker = _financial_snapshot(
        account="abc", captured_at="2026-08-13T10:00:00+08:00",
        equity=101_000, flows=[], complete=False,
    )

    result = portfolio_snapshot_from_store(store, broker)

    assert not result.data_complete
    assert "monthly_cash_flow_history_incomplete" in result.data_gaps


def test_cash_flow_validation_rejects_bad_direction_time_and_cross_month(tmp_path) -> None:
    store = TradeStore(tmp_path / "trades.db")
    base = {
        "captured_at": "2026-08-10T15:00:00+08:00",
        "range_start": "2026-08-01T00:00:00+08:00",
        "range_end": "2026-08-10T15:00:00+08:00",
        "complete": True,
    }
    for payload in (
        {
            "direction": "dividend", "amount": 1,
            "occurred_at": "2026-08-05T10:00:00+08:00",
        },
        {
            "direction": "deposit", "amount": 1,
            "occurred_at": "2026-07-31T10:00:00+08:00",
        },
    ):
        try:
            store.upsert_broker_cash_flows("abc", [payload], **base)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid cash flow must fail closed")
    assert store.list_broker_cash_flows(account_fingerprint="abc") == []

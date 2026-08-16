from __future__ import annotations

import inspect
from datetime import UTC, datetime

from pa_agent.brokers.ths_adapter import (
    ThsAdapterIncompatibleError,
    ThsBrokerAdapter,
    Win32ThsBackend,
    _account_identity_from_controls,
    _cash_flow_query_range,
    _funds_text_from_controls,
    _is_cash_flow_grid,
    _is_fill_grid,
    _is_order_grid,
    _parse_cash_flows,
    _parse_fills,
    _parse_funds,
    _parse_orders,
    _parse_quote,
    _trading_window_score,
    account_fingerprint,
)
from pa_agent.trading.broker_models import (
    AuthorizedOrder,
    BrokerConnectionStatus,
    BrokerFill,
    BrokerOrder,
    BrokerSnapshot,
    ConnectionState,
    ThsBinding,
)

NOW = datetime.now(UTC).astimezone().isoformat()


def test_real_trading_shell_outranks_transient_order_editor_window() -> None:
    assert _trading_window_score(
        "网上股票交易系统5.0", visible=False
    ) > _trading_window_score("表格委托价格编辑悬浮框", visible=True)


def test_native_prefill_contains_no_final_order_action_primitive() -> None:
    source = inspect.getsource(Win32ThsBackend.prefill_fields)

    assert "BM_CLICK" not in source
    assert "WM_COMMAND" not in source
    assert "mouse_event" not in source
    assert ".click(" not in source
    assert "WM_SETTEXT" in source


class FakeBackend:
    def __init__(self) -> None:
        self.prefill_calls = 0
        self.clear_calls = 0

    def discover(self) -> dict:
        return {
            "market_pid": 1, "trading_pid": 2, "market_window": 3, "trading_window": 4,
            "install_path": r"D:\ths", "client_version": "1.2.3.4",
            "detected_broker_name": "测试券商",
            "detected_masked_account": "****1234",
        }

    def visible_texts(self) -> list[str]:
        return []

    def read_snapshot_tables(self) -> dict[str, str]:
        return {
            "funds": "总资产\t可用资金\t股票市值\t当日盈亏\n100000\t80000\t20000\t100",
            "positions": "证券代码\t证券名称\t持仓数量\t可卖数量\t成本价\t最新价\t市值\n600519\t贵州茅台\t100\t100\t100\t101\t10100",
            "orders": "合同编号\t证券代码\t买卖标志\t委托价格\t委托数量\t成交数量\t委托状态\t委托时间\nO1\t600519\t买入\t100\t100\t100\t已成\t" + NOW,
            "fills": "成交编号\t合同编号\t证券代码\t买卖标志\t成交价格\t成交数量\t成交时间\nF1\tO1\t600519\t买入\t100\t100\t" + NOW,
            "quote": "600519 贵州茅台 最新价: 101",
        }

    def prefill_fields(self, order: AuthorizedOrder) -> dict:
        self.prefill_calls += 1
        return {"verified_fields": {
            "symbol": order.symbol, "direction": order.direction, "price": order.price,
            "quantity": order.quantity, "name": order.name,
        }}

    def clear_prefill_if_matches(self, order: AuthorizedOrder) -> dict:
        self.clear_calls += 1
        return {
            "status": "cleared",
            "message": "cleared after exact readback",
            "verified_fields": {
                "symbol": order.symbol,
                "direction": order.direction,
                "price": order.price,
                "quantity": order.quantity,
                "name": order.name,
            },
        }

    def clear_prefill_fields(self) -> dict:
        self.clear_calls += 1
        return {
            "status": "cleared",
            "message": "failed prefill fields cleared and read back empty",
            "verified_fields": {"symbol": "", "price": "", "quantity": ""},
        }


def _binding(*, prefill: bool = False) -> ThsBinding:
    fingerprint = account_fingerprint(
        install_path=r"D:\ths", client_version="1.2.3.4",
        broker_name="测试券商", masked_account="****1234",
    )
    return ThsBinding(
        enabled=True, read_only=not prefill, install_path=r"D:\ths",
        client_version="1.2.3.4", broker_name="测试券商",
        masked_account="****1234", account_fingerprint=fingerprint,
        confirmed=True, allow_prefill=prefill,
    )


def test_snapshot_parses_funds_positions_orders_and_fills() -> None:
    adapter = ThsBrokerAdapter(_binding(), backend=FakeBackend())
    snapshot = adapter.snapshot()
    assert snapshot.complete
    assert snapshot.positions_complete
    assert snapshot.orders_complete
    assert snapshot.fills_complete
    assert snapshot.total_equity == 100000
    assert snapshot.positions[0].sellable_quantity == 100
    assert snapshot.orders[0].broker_order_id == "O1"
    assert snapshot.fills[0].broker_fill_id == "F1"
    assert snapshot.quote is not None
    assert snapshot.quote.name == "贵州茅台"


def test_snapshot_contract_change_latches_adapter_incompatible_state() -> None:
    backend = FakeBackend()

    def incompatible_tables() -> dict:
        raise ThsAdapterIncompatibleError("持仓表列名已变化")

    backend.read_snapshot_tables = incompatible_tables
    adapter = ThsBrokerAdapter(_binding(), backend=backend)

    snapshot = adapter.snapshot()
    next_connection = adapter.connect()

    assert not snapshot.complete
    assert snapshot.connection.status is BrokerConnectionStatus.ADAPTER_INCOMPATIBLE
    assert next_connection.status is BrokerConnectionStatus.ADAPTER_INCOMPATIBLE
    assert "重新校准" in next_connection.message


def test_snapshot_never_treats_missing_fact_tables_as_complete() -> None:
    backend = FakeBackend()
    backend.read_snapshot_tables = lambda: {
        "funds": "总资产: 100000\n可用资金: 80000\n股票市值: 20000",
        "quote": "600519 贵州茅台 最新价: 101",
    }

    snapshot = ThsBrokerAdapter(_binding(), backend=backend).snapshot()

    assert not snapshot.complete
    assert not snapshot.positions_complete
    assert not snapshot.orders_complete
    assert not snapshot.fills_complete
    assert "positions_table_not_verified" in snapshot.warnings
    assert "orders_table_not_verified" in snapshot.warnings
    assert "fills_table_not_verified" in snapshot.warnings


def test_explicit_empty_fact_tables_are_complete_when_headers_are_verified() -> None:
    backend = FakeBackend()
    backend.read_snapshot_tables = lambda: {
        "funds": "总资产: 100000\n可用资金: 100000\n股票市值: 0",
        "positions": "证券代码\t证券名称\t持仓数量\t可卖数量\t成本价\t最新价\t市值\n暂无数据",
        "orders": "证券代码\t买卖标志\t委托价格\t委托数量\t委托状态\t委托时间\n暂无数据",
        "fills": "证券代码\t买卖标志\t成交价格\t成交数量\t成交时间\n暂无数据",
        "quote": "600519 贵州茅台 最新价: 101",
    }

    snapshot = ThsBrokerAdapter(_binding(), backend=backend).snapshot()

    assert snapshot.complete
    assert snapshot.positions == []
    assert snapshot.orders == []
    assert snapshot.fills == []
    assert snapshot.positions_complete
    assert snapshot.orders_complete
    assert snapshot.fills_complete


def test_malformed_fact_table_row_fails_closed() -> None:
    backend = FakeBackend()
    backend.read_snapshot_tables = lambda: {
        "funds": "总资产: 100000\n可用资金: 100000\n股票市值: 0",
        "positions": "证券代码\t证券名称\t持仓数量\t可卖数量\t成本价\t最新价\t市值\n600519\t贵州茅台\t100",
        "orders": "证券代码\t买卖标志\t委托价格\t委托数量\t委托状态\t委托时间\n暂无数据",
        "fills": "证券代码\t买卖标志\t成交价格\t成交数量\t成交时间\n暂无数据",
        "quote": "600519 贵州茅台 最新价: 101",
    }

    snapshot = ThsBrokerAdapter(_binding(), backend=backend).snapshot()

    assert not snapshot.complete
    assert not snapshot.positions_complete
    assert "positions_row_incomplete" in snapshot.warnings


def test_quote_requires_independent_security_name_for_authorization() -> None:
    warnings: list[str] = []
    quote = _parse_quote("600519 贵州茅台 最新价: 101", warnings)
    assert quote is not None
    assert quote.name == "贵州茅台"
    assert warnings == ["quote_execution_state_unverified"]

    missing_name_warnings: list[str] = []
    no_name = _parse_quote("证券代码 600519 最新价: 101", missing_name_warnings)
    assert no_name is not None
    assert no_name.name == ""
    assert "quote_security_name_unavailable" in missing_name_warnings


def test_quote_execution_state_requires_limits_and_explicit_trading_status() -> None:
    warnings: list[str] = []
    quote = _parse_quote(
        "600519 贵州茅台 最新价: 101 涨停价: 110 跌停价: 90 交易状态: 正常",
        warnings,
    )

    assert quote is not None
    assert quote.execution_state_verified
    assert quote.upper_limit == 110
    assert quote.lower_limit == 90
    assert not quote.suspended
    assert not quote.limit_locked
    assert "quote_execution_state_unverified" not in warnings

    locked = _parse_quote(
        "600519 贵州茅台 最新价: 110 涨停价: 110 跌停价: 90 交易状态: 正常",
        [],
    )
    assert locked is not None and locked.limit_locked

    incomplete_warnings: list[str] = []
    incomplete = _parse_quote(
        "600519 贵州茅台 最新价: 101",
        incomplete_warnings,
    )
    assert incomplete is not None
    assert not incomplete.execution_state_verified
    assert "quote_execution_state_unverified" in incomplete_warnings


def test_order_and_fill_grid_identification_is_strict() -> None:
    assert _is_order_grid(
        "证券代码\t买卖标志\t委托价格\t委托数量\t委托状态\t委托时间\n暂无数据"
    )
    assert _is_fill_grid(
        "证券代码\t买卖标志\t成交价格\t成交数量\t成交时间\n暂无数据"
    )
    assert not _is_order_grid(
        "证券代码\t证券名称\t持仓数量\t可卖数量\t成本价\t最新价\t市值"
    )


def test_read_only_binding_never_calls_prefill_backend() -> None:
    backend = FakeBackend()
    adapter = ThsBrokerAdapter(_binding(prefill=False), backend=backend)
    order = AuthorizedOrder(
        plan_id="p", account_fingerprint=_binding().account_fingerprint,
        symbol="600519", name="贵州茅台", direction="buy", price=100, quantity=100,
        stop_loss_price=95, strategy_id="s", authorized_at=NOW, expires_at=NOW,
    )
    receipt = adapter.prefill(order)
    assert receipt.status == "blocked"
    assert backend.prefill_calls == 0
    assert not receipt.final_confirmation_clicked


def test_prefill_rejects_non_a_share_before_touching_broker_ui() -> None:
    backend = FakeBackend()
    binding = _binding(prefill=True)
    adapter = ThsBrokerAdapter(binding, backend=backend)
    order = AuthorizedOrder(
        plan_id="non-a-share",
        account_fingerprint=binding.account_fingerprint,
        symbol="XAUUSD",
        name="Gold",
        direction="buy",
        price=100,
        quantity=100,
        stop_loss_price=95,
        strategy_id="legacy",
        authorized_at=NOW,
        expires_at=NOW,
    )

    receipt = adapter.prefill(order)

    assert receipt.status == "blocked"
    assert "仅限A股" in receipt.message
    assert backend.prefill_calls == 0
    assert not receipt.final_confirmation_clicked


def test_prefill_never_claims_final_confirmation() -> None:
    backend = FakeBackend()
    binding = _binding(prefill=True)
    adapter = ThsBrokerAdapter(binding, backend=backend)
    order = AuthorizedOrder(
        plan_id="p", account_fingerprint=binding.account_fingerprint,
        symbol="600519", name="贵州茅台", direction="buy", price=100, quantity=100,
        stop_loss_price=95, strategy_id="s", authorized_at=NOW, expires_at=NOW,
    )
    receipt = adapter.prefill(order)
    assert receipt.status == "awaiting_user_confirmation"
    assert not receipt.final_confirmation_clicked


def test_prefill_contract_change_blocks_and_latches_adapter_incompatible() -> None:
    backend = FakeBackend()
    binding = _binding(prefill=True)

    def incompatible_prefill(_order: AuthorizedOrder) -> dict:
        raise ThsAdapterIncompatibleError("委托输入框标签已变化")

    backend.prefill_fields = incompatible_prefill
    adapter = ThsBrokerAdapter(binding, backend=backend)
    order = AuthorizedOrder(
        plan_id="p",
        account_fingerprint=binding.account_fingerprint,
        symbol="600519",
        name="贵州茅台",
        direction="buy",
        price=100,
        quantity=100,
        stop_loss_price=95,
        strategy_id="s",
        authorized_at=NOW,
        expires_at=NOW,
    )

    receipt = adapter.prefill(order)

    assert receipt.status == "blocked"
    assert not receipt.final_confirmation_clicked
    assert adapter.connection.status is BrokerConnectionStatus.ADAPTER_INCOMPATIBLE
    assert adapter.connect().status is BrokerConnectionStatus.ADAPTER_INCOMPATIBLE


def test_prefill_accepts_equivalent_numeric_readback_formats() -> None:
    backend = FakeBackend()
    binding = _binding(prefill=True)
    backend.prefill_fields = lambda order: {"verified_fields": {
        "symbol": order.symbol,
        "direction": order.direction,
        "price": "100.00",
        "quantity": "100.0",
        "name": order.name,
    }}
    adapter = ThsBrokerAdapter(binding, backend=backend)
    order = AuthorizedOrder(
        plan_id="p", account_fingerprint=binding.account_fingerprint,
        symbol="600519", name="璐靛窞鑼呭彴", direction="buy",
        price=100, quantity=100, stop_loss_price=95, strategy_id="s",
        authorized_at=NOW, expires_at=NOW,
    )

    receipt = adapter.prefill(order)

    assert receipt.status == "awaiting_user_confirmation"
    assert backend.clear_calls == 0


def test_prefill_readback_mismatch_must_clear_and_verify_editable_fields() -> None:
    backend = FakeBackend()
    binding = _binding(prefill=True)
    backend.prefill_fields = lambda order: {"verified_fields": {
        "symbol": order.symbol,
        "direction": order.direction,
        "price": 101,
        "quantity": order.quantity,
        "name": order.name,
    }}
    adapter = ThsBrokerAdapter(binding, backend=backend)
    order = AuthorizedOrder(
        plan_id="p", account_fingerprint=binding.account_fingerprint,
        symbol="600519", name="璐靛窞鑼呭彴", direction="buy",
        price=100, quantity=100, stop_loss_price=95, strategy_id="s",
        authorized_at=NOW, expires_at=NOW,
    )

    receipt = adapter.prefill(order)

    assert receipt.status == "failed"
    assert receipt.verified_fields["cleanup_status"] == "cleared"
    assert backend.clear_calls == 1
    assert not receipt.final_confirmation_clicked


def test_prefill_clear_requires_connected_matching_account() -> None:
    backend = FakeBackend()
    binding = _binding(prefill=True)
    adapter = ThsBrokerAdapter(binding, backend=backend)
    order = AuthorizedOrder(
        plan_id="p", account_fingerprint="wrong-account",
        symbol="600519", name="贵州茅台", direction="buy", price=100, quantity=100,
        stop_loss_price=95, strategy_id="s", authorized_at=NOW, expires_at=NOW,
    )

    receipt = adapter.clear_prefill_if_matches(order)

    assert receipt.status == "not_cleared"
    assert backend.clear_calls == 0
    assert not receipt.final_confirmation_clicked


def test_prefill_clear_delegates_only_to_exact_readback_backend() -> None:
    backend = FakeBackend()
    binding = _binding(prefill=True)
    adapter = ThsBrokerAdapter(binding, backend=backend)
    order = AuthorizedOrder(
        plan_id="p", account_fingerprint=binding.account_fingerprint,
        symbol="600519", name="贵州茅台", direction="buy", price=100, quantity=100,
        stop_loss_price=95, strategy_id="s", authorized_at=NOW, expires_at=NOW,
    )

    receipt = adapter.clear_prefill_if_matches(order)

    assert receipt.status == "cleared"
    assert backend.clear_calls == 1
    assert receipt.verified_fields["name"] == "贵州茅台"
    assert not receipt.final_confirmation_clicked


def test_running_trade_process_without_detected_account_is_not_connected() -> None:
    backend = FakeBackend()
    backend.discover = lambda: {
        "market_pid": 1,
        "trading_pid": 2,
        "install_path": r"D:\ths",
        "client_version": "1.2.3.4",
    }
    state = ThsBrokerAdapter(_binding(), backend=backend).connect()
    assert state.status.value == "login_required"
    assert not state.usable
    assert state.account_fingerprint == ""


def test_binding_requires_client_readback_to_match_user_confirmation() -> None:
    backend = FakeBackend()
    adapter = ThsBrokerAdapter(_binding(), backend=backend)
    try:
        adapter.confirmed_binding(broker_name="另一券商", masked_account="****1234")
    except RuntimeError as exc:
        assert "券商名称" in str(exc)
    else:
        raise AssertionError("mismatched broker must fail closed")


def test_hidden_logged_in_window_reads_only_labeled_account_combo() -> None:
    controls = [
        {
            "hwnd": 1, "parent": 10, "class_name": "Button",
            "text": "退出", "visible": False, "selected_text": "",
        },
        {
            "hwnd": 2, "parent": 20, "class_name": "Static",
            "text": "总资产", "visible": False, "selected_text": "",
        },
        {
            "hwnd": 3, "parent": 20, "class_name": "CVirtualGridCtrl",
            "text": "Custom2", "visible": False, "selected_text": "",
        },
        {
            "hwnd": 4, "parent": 10, "class_name": "Static",
            "text": "资金帐户", "visible": False, "selected_text": "",
        },
        {
            "hwnd": 5, "parent": 10, "class_name": "ComboBox",
            "text": "", "visible": False, "selected_text": "12341252",
        },
        {
            "hwnd": 6, "parent": 10, "class_name": "ComboBox",
            "text": "", "visible": False, "selected_text": "默认显示",
        },
        {
            "hwnd": 7, "parent": 10, "class_name": "Button",
            "text": "登录", "visible": False, "selected_text": "",
        },
    ]

    identity = _account_identity_from_controls(
        "网上股票交易系统5.0", controls, modal_texts=[]
    )

    assert not identity["login_required"]
    assert identity["detected_masked_account"] == "****1252"
    assert identity["detected_broker_name"] == ""


def test_account_identity_uses_geometry_when_parent_has_multiple_numeric_combos() -> None:
    controls = [
        {
            "hwnd": 1, "parent": 10, "class_name": "Button",
            "text": "退出", "visible": False, "selected_text": "", "rect": (0, 0, 1, 1),
        },
        {
            "hwnd": 2, "parent": 10, "class_name": "Static",
            "text": "总资产", "visible": False, "selected_text": "", "rect": (0, 0, 1, 1),
        },
        {
            "hwnd": 3, "parent": 10, "class_name": "CVirtualGridCtrl",
            "text": "Custom2", "visible": False, "selected_text": "", "rect": (0, 0, 1, 1),
        },
        {
            "hwnd": 4, "parent": 20, "class_name": "Static",
            "text": "资金帐户", "visible": False, "selected_text": "",
            "rect": (100, 100, 160, 120),
        },
        {
            "hwnd": 5, "parent": 20, "class_name": "ComboBox",
            "text": "", "visible": False, "selected_text": "12341252",
            "rect": (170, 100, 270, 120),
        },
        {
            "hwnd": 6, "parent": 20, "class_name": "ComboBox",
            "text": "", "visible": False, "selected_text": "2026087076",
            "rect": (10, 140, 110, 160),
        },
    ]
    identity = _account_identity_from_controls(
        "网上股票交易系统5.0 国金证券", controls, modal_texts=[]
    )
    assert identity["detected_masked_account"] == "****1252"
    assert identity["detected_broker_name"] == "国金证券"


def test_missing_broker_name_remains_fail_closed_after_account_readback() -> None:
    backend = FakeBackend()
    backend.discover = lambda: {
        "market_pid": 1,
        "trading_pid": 2,
        "install_path": r"D:\ths",
        "client_version": "1.2.3.4",
        "login_required": False,
        "detected_broker_name": "",
        "detected_masked_account": "****1252",
    }

    state = ThsBrokerAdapter(ThsBinding(), backend=backend).connect()

    assert state.status.value == "login_required"
    assert state.detected_masked_account == "****1252"
    assert not state.usable


def test_visible_verification_or_error_modal_blocks_account() -> None:
    controls = [
        {
            "hwnd": 1, "parent": 10, "class_name": "Button",
            "text": "退出", "visible": False, "selected_text": "",
        },
        {
            "hwnd": 2, "parent": 10, "class_name": "Static",
            "text": "总资产", "visible": False, "selected_text": "",
        },
        {
            "hwnd": 3, "parent": 10, "class_name": "CVirtualGridCtrl",
            "text": "Custom2", "visible": False, "selected_text": "",
        },
    ]

    for message in ("请输入验证码", "错误提示", "账户锁定"):
        identity = _account_identity_from_controls(
            "网上股票交易系统5.0", controls, modal_texts=[message]
        )
        assert identity["blocked_by_modal"]


def test_adapter_reports_blocked_modal_before_login_state() -> None:
    backend = FakeBackend()
    backend.discover = lambda: {
        "market_pid": 1,
        "trading_pid": 2,
        "install_path": r"D:\ths",
        "client_version": "1.2.3.4",
        "login_required": False,
        "blocked_by_modal": True,
        "detected_broker_name": "测试券商",
        "detected_masked_account": "****1252",
    }

    state = ThsBrokerAdapter(ThsBinding(), backend=backend).connect()

    assert state.status.value == "blocked_by_modal"
    assert not state.usable


def test_adapter_launches_clients_without_login_interaction() -> None:
    backend = FakeBackend()
    launched: list[str] = []
    backend.launch_clients = lambda install_path="": launched.append(install_path) or []
    adapter = ThsBrokerAdapter(ThsBinding(install_path=r"D:\ths"), backend=backend)

    state = adapter.launch_clients()

    assert launched == [r"D:\ths"]
    assert state.status.value == "connected_read_only"


def test_hidden_funds_controls_are_paired_by_parent_and_geometry() -> None:
    controls = [
        {"parent": 20, "class_name": "Static", "text": "可用金额", "rect": (56, 123, 121, 139)},
        {"parent": 20, "class_name": "Static", "text": "573.55", "rect": (138, 123, 231, 139)},
        {"parent": 20, "class_name": "Static", "text": "股票市值", "rect": (56, 163, 121, 179)},
        {"parent": 20, "class_name": "Static", "text": "289233.00", "rect": (138, 163, 231, 179)},
        {"parent": 20, "class_name": "Static", "text": "总 资 产", "rect": (56, 183, 121, 199)},
        {"parent": 20, "class_name": "Static", "text": "289806.55", "rect": (138, 183, 231, 199)},
        {"parent": 20, "class_name": "Static", "text": "当日盈亏", "rect": (56, 223, 121, 239)},
        {"parent": 20, "class_name": "Static", "text": "0.00", "rect": (138, 223, 231, 239)},
        {"parent": 99, "class_name": "Static", "text": "123456.78", "rect": (138, 123, 231, 139)},
    ]

    text = _funds_text_from_controls(controls)

    assert "可用资金: 573.55" in text
    assert "股票市值: 289233.00" in text
    assert "总资产: 289806.55" in text
    assert "当日盈亏: 0.00" in text
    assert "123456.78" not in text
    warnings: list[str] = []
    funds = _parse_funds(text, warnings)
    assert funds == {
        "total_equity": 289806.55,
        "available_cash": 573.55,
        "position_value": 289233.00,
        "daily_pnl": 0.00,
    }
    assert not warnings


def test_snapshot_uses_current_grid_only_when_header_is_position_table() -> None:
    backend = FakeBackend()
    backend.read_snapshot_tables = lambda: {
        "funds": "总资产: 100000\n可用资金: 80000\n股票市值: 20000",
        "current_grid": (
            "操作\t序号\t证券代码\t证券名称\t股票余额\t可用余额\t冻结数量\t"
            "成本价\t市价\t盈亏\t盈亏比例(%)\t当日盈亏\t当日盈亏比(%)\t市值\n"
            "卖出\t1\t600519\t贵州茅台\t100\t100\t0\t100\t101\t100\t1\t0\t0\t10100"
        ),
    }

    snapshot = ThsBrokerAdapter(_binding(), backend=backend).snapshot()

    assert snapshot.positions[0].symbol == "600519"
    assert snapshot.positions[0].quantity == 100


def test_non_position_current_grid_is_not_parsed_as_position() -> None:
    backend = FakeBackend()
    backend.read_snapshot_tables = lambda: {
        "funds": "总资产: 100000\n可用资金: 80000\n股票市值: 20000",
        "current_grid": "委托编号\t证券代码\t委托价格\t委托数量\n1\t600519\t100\t100",
    }

    snapshot = ThsBrokerAdapter(_binding(), backend=backend).snapshot()

    assert snapshot.positions == []


def test_cash_flow_rows_and_explicit_empty_history_are_parsed() -> None:
    warnings: list[str] = []
    flows = _parse_cash_flows(
        "业务流水号\t业务名称\t发生金额\t发生时间\t状态\n"
        "A1\t银转证\t10000\t2026-08-05T10:00:00+08:00\t成功\n"
        "A2\t证转银\t5000\t2026-08-10T10:00:00+08:00\t成功",
        warnings,
    )
    assert [item.direction for item in flows] == ["deposit", "withdrawal"]
    assert [item.amount for item in flows] == [10_000, 5_000]
    assert not warnings

    empty_warnings: list[str] = []
    assert _parse_cash_flows("资金流水\n暂无数据", empty_warnings) == []
    assert not empty_warnings


def test_snapshot_cash_flow_history_is_only_complete_with_explicit_bounded_table() -> None:
    backend = FakeBackend()
    backend.read_snapshot_tables = lambda: {
        "funds": "总资产: 100000\n可用资金: 80000\n股票市值: 20000",
        "cash_flows": "资金流水\n暂无数据",
        "cash_flows_complete": True,
        "cash_flow_range_start": "2026-08-01T00:00:00+08:00",
        "cash_flow_range_end": NOW,
    }
    snapshot = ThsBrokerAdapter(_binding(), backend=backend).snapshot()
    assert snapshot.cash_flow_complete
    assert snapshot.cash_flows == []
    assert snapshot.cash_flow_range_start == "2026-08-01T00:00:00+08:00"

    backend.read_snapshot_tables = lambda: {
        "funds": "总资产: 100000\n可用资金: 80000\n股票市值: 20000",
    }
    incomplete = ThsBrokerAdapter(_binding(), backend=backend).snapshot()
    assert not incomplete.cash_flow_complete
    assert "cash_flow_history_not_verified" in incomplete.warnings


def test_cash_flow_grid_and_current_month_range_must_be_explicit() -> None:
    assert _is_cash_flow_grid(
        "业务名称\t发生金额\t发生时间\t状态\n银转证\t100\t2026-08-01\t成功"
    )
    assert not _is_cash_flow_grid(
        "证券代码\t证券名称\t持仓数量\n600519\t贵州茅台\t100"
    )
    controls = [
        {"visible": True, "text": "开始日期 2026-08-01"},
        {"visible": True, "text": "结束日期 2026-08-13"},
    ]
    start, end = _cash_flow_query_range(
        controls, captured_at="2026-08-13T15:00:00+08:00"
    )
    assert start == "2026-08-01T00:00:00+08:00"
    assert end == "2026-08-13T15:00:00+08:00"
    assert _cash_flow_query_range(
        [{"visible": True, "text": "2026-08-05 至 2026-08-13"}],
        captured_at="2026-08-13T15:00:00+08:00",
    ) == ("", "")


def test_snapshot_only_requests_cash_flow_page_on_explicit_call() -> None:
    backend = FakeBackend()
    backend.cash_flow_reads = 0

    def read_current_cash_flow_page(*, captured_at: str) -> dict:
        backend.cash_flow_reads += 1
        return {
            "cash_flows": (
                "业务名称\t发生金额\t发生时间\t状态\n"
                "银转证\t10000\t2026-08-05T10:00:00+08:00\t成功"
            ),
            "cash_flows_complete": True,
            "cash_flow_range_start": "2026-08-01T00:00:00+08:00",
            "cash_flow_range_end": captured_at,
        }

    backend.read_current_cash_flow_page = read_current_cash_flow_page
    adapter = ThsBrokerAdapter(_binding(), backend=backend)
    adapter.snapshot()
    assert backend.cash_flow_reads == 0
    snapshot = adapter.snapshot(read_current_cash_flow_page=True)
    assert backend.cash_flow_reads == 1
    assert snapshot.cash_flow_complete
    assert snapshot.cash_flows[0].amount == 10_000


def test_broker_table_clock_times_are_bound_to_snapshot_trading_date() -> None:
    captured = "2026-08-14T10:01:02+08:00"
    warnings: list[str] = []
    orders = _parse_orders(
        "委托编号\t证券代码\t买卖标志\t委托价格\t委托数量\t成交数量\t"
        "委托状态\t委托时间\n"
        "O1\t600519\t买入\t100\t100\t0\t已报\t10:00:30",
        warnings,
        captured_at=captured,
    )
    fills = _parse_fills(
        "成交编号\t委托编号\t证券代码\t买卖标志\t成交价格\t成交数量\t"
        "成交时间\n"
        "F1\tO1\t600519\t买入\t100\t100\t10:00:45",
        warnings,
        captured_at=captured,
    )

    assert orders[0].submitted_at == "2026-08-14T10:00:30+08:00"
    assert fills[0].filled_at == "2026-08-14T10:00:45+08:00"
    assert not warnings


def test_broker_rows_with_unverifiable_times_remain_incomplete() -> None:
    warnings: list[str] = []
    orders = _parse_orders(
        "委托编号\t证券代码\t买卖标志\t委托价格\t委托数量\t委托状态\t委托时间\n"
        "O1\t600519\t买入\t100\t100\t已报\t未知",
        warnings,
        captured_at="2026-08-14T10:01:02+08:00",
    )

    assert orders == []
    assert "orders_row_incomplete" in warnings


def _order_for_reconciliation(binding: ThsBinding) -> AuthorizedOrder:
    return AuthorizedOrder(
        plan_id="p", account_fingerprint=binding.account_fingerprint,
        symbol="600519", name="璐靛窞鑼呭彴", direction="buy",
        price=100, quantity=100, stop_loss_price=95, strategy_id="s",
        authorized_at=NOW, expires_at=NOW,
    )


def _reconciliation_snapshot(
    binding: ThsBinding, *, captured_at: str | None = None,
    orders_complete: bool = True, fills_complete: bool = True,
) -> BrokerSnapshot:
    connection = ConnectionState(
        status=BrokerConnectionStatus.CONNECTED,
        account_fingerprint=binding.account_fingerprint,
        checked_at=NOW,
    )
    return BrokerSnapshot(
        connection=connection,
        account_fingerprint=binding.account_fingerprint,
        orders=[BrokerOrder(
            broker_order_id="O1", symbol="600519", direction="buy",
            price=100, quantity=100, filled_quantity=100,
            status="filled", submitted_at=NOW,
        )],
        fills=[BrokerFill(
            broker_fill_id="F1", broker_order_id="O1", symbol="600519",
            direction="buy", price=100, quantity=100, filled_at=NOW,
        )],
        orders_complete=orders_complete,
        fills_complete=fills_complete,
        captured_at=captured_at or datetime.now(UTC).astimezone().isoformat(),
    )


def test_reconciliation_rejects_wrong_account_incomplete_or_stale_snapshot() -> None:
    binding = _binding(prefill=True)
    adapter = ThsBrokerAdapter(binding, backend=FakeBackend())
    order = _order_for_reconciliation(binding)

    wrong = _reconciliation_snapshot(binding)
    wrong.account_fingerprint = "wrong"
    result = adapter.reconcile(order, wrong)
    assert result.status == "reconciliation_required"
    assert "account_fingerprint_mismatch" in result.message

    incomplete = _reconciliation_snapshot(binding, fills_complete=False)
    result = adapter.reconcile(order, incomplete)
    assert "fills_table_incomplete" in result.message

    stale = _reconciliation_snapshot(
        binding, captured_at="2026-08-13T10:00:00+08:00"
    )
    result = adapter.reconcile(order, stale)
    assert "snapshot_stale" in result.message


def test_reconciliation_never_matches_unparseable_order_time_or_missing_ids() -> None:
    binding = _binding(prefill=True)
    adapter = ThsBrokerAdapter(binding, backend=FakeBackend())
    order = _order_for_reconciliation(binding)
    snapshot = _reconciliation_snapshot(binding)
    snapshot.orders[0].submitted_at = "not-a-time"

    result = adapter.reconcile(order, snapshot)

    assert result.status == "reconciliation_required"
    snapshot.orders[0].submitted_at = NOW
    snapshot.orders[0].broker_order_id = ""
    result = adapter.reconcile(order, snapshot)
    assert result.status == "reconciliation_required"


def test_snapshot_discards_values_if_account_changes_during_page_navigation() -> None:
    backend = FakeBackend()
    binding = _binding()
    calls = 0

    def discover():
        nonlocal calls
        calls += 1
        account = "****1234" if calls == 1 else "****5678"
        return {
            "market_pid": 1, "trading_pid": 2,
            "market_window": 3, "trading_window": 4,
            "install_path": r"D:\ths", "client_version": "1.2.3.4",
            "detected_broker_name": binding.broker_name,
            "detected_masked_account": account,
        }

    backend.discover = discover
    snapshot = ThsBrokerAdapter(binding, backend=backend).snapshot()

    assert not snapshot.complete
    assert snapshot.total_equity is None
    assert snapshot.positions == []
    assert "account_or_client_state_changed_during_snapshot" in snapshot.warnings

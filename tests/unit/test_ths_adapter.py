from __future__ import annotations

from datetime import UTC, datetime

from pa_agent.brokers.ths_adapter import (
    ThsBrokerAdapter,
    _account_identity_from_controls,
    _cash_flow_query_range,
    _funds_text_from_controls,
    _is_cash_flow_grid,
    _parse_cash_flows,
    _parse_funds,
    _trading_window_score,
    account_fingerprint,
)
from pa_agent.trading.broker_models import AuthorizedOrder, ThsBinding

NOW = datetime.now(UTC).astimezone().isoformat()


def test_real_trading_shell_outranks_transient_order_editor_window() -> None:
    assert _trading_window_score(
        "网上股票交易系统5.0", visible=False
    ) > _trading_window_score("表格委托价格编辑悬浮框", visible=True)


class FakeBackend:
    def __init__(self) -> None:
        self.prefill_calls = 0

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
    assert snapshot.total_equity == 100000
    assert snapshot.positions[0].sellable_quantity == 100
    assert snapshot.orders[0].broker_order_id == "O1"
    assert snapshot.fills[0].broker_fill_id == "F1"


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

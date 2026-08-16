from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import (
    QInputDialog,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QStatusBar,
    QWidget,
)

from pa_agent.config.settings import Settings
from pa_agent.gui.trade_ledger_window import (
    TradeLedgerWindow,
    _friendly_runtime_detail,
    _monthly_risk_mode,
    _prefill_strategy_is_supported,
    _strategy_state_owner,
)
from pa_agent.trading.broker_models import (
    AuthorizedOrder,
    BrokerCashFlow,
    BrokerConnectionStatus,
    BrokerFill,
    BrokerOrder,
    BrokerSnapshot,
    ConnectionState,
)
from pa_agent.trading.models import AssetClass, PlanStatus, TradePlan
from pa_agent.trading.portfolio import PortfolioRisk
from pa_agent.trading.quant import SignalDecision
from pa_agent.trading.store import TradeStore
from pa_agent.trading.topdown import MANUAL_EXCEPTION_STRATEGY_ID, TOPDOWN_STRATEGY_ID
from pa_agent.trading.universe import (
    CurrentUniverseMember,
    ManagedAshareUniverseService,
    UniverseSnapshot,
)

NOW = "2026-08-12T10:00:00+08:00"


def test_prefill_only_accepts_topdown_and_explicit_manual_exception() -> None:
    assert _prefill_strategy_is_supported(TOPDOWN_STRATEGY_ID)
    assert _prefill_strategy_is_supported("manual_exception_4321_v1")
    assert not _prefill_strategy_is_supported("hs300_daily_pullback_v1")
    assert not _prefill_strategy_is_supported("legacy_import")


def test_manual_exception_uses_validated_topdown_strategy_state() -> None:
    assert _strategy_state_owner(MANUAL_EXCEPTION_STRATEGY_ID) == TOPDOWN_STRATEGY_ID
    assert _strategy_state_owner(TOPDOWN_STRATEGY_ID) == TOPDOWN_STRATEGY_ID


def test_runtime_network_errors_are_actionable_without_losing_diagnostics() -> None:
    raw = (
        "失败关闭：Failed to perform, curl: (6) Could not resolve host: "
        "emweb.securities.eastmoney.com"
    )

    friendly = _friendly_runtime_detail("hotspots", raw)

    assert friendly == (
        "热点数据源暂时无法连接；已保留上次可信数据，本次新增交易保持阻断"
    )
    assert "curl" not in friendly


def test_runtime_status_keeps_raw_network_diagnostic_in_tooltip(qtbot, tmp_path) -> None:
    settings = Settings()
    store = TradeStore(tmp_path / "trades.db")
    context = SimpleNamespace(
        settings=settings,
        trade_store=store,
        broker_adapter=_Broker(),
        portfolio_risk=PortfolioRisk(settings.risk, settings.portfolio_risk),
        trading_service=SimpleNamespace(risk_settings=settings.risk),
        hotspot_service=None,
        universe_service=None,
        logger=SimpleNamespace(warning=lambda *args: None),
    )
    window = TradeLedgerWindow(context)
    qtbot.addWidget(window)
    raw = "失败关闭：curl: (6) Could not resolve host: example.test"

    window._runtime_status_changed("hotspots", raw)

    assert "热点数据源暂时无法连接" in window.activity_status_label.text()
    assert raw in window.activity_status_label.toolTip()


def test_today_actions_exclude_legacy_ai_research_plans(qtbot, tmp_path) -> None:
    settings = Settings()
    store = TradeStore(tmp_path / "trades.db")
    broker = _Broker()
    context = SimpleNamespace(
        settings=settings,
        trade_store=store,
        broker_adapter=broker,
        portfolio_risk=PortfolioRisk(settings.risk, settings.portfolio_risk),
        trading_service=SimpleNamespace(risk_settings=settings.risk),
        hotspot_service=None,
        universe_service=None,
        logger=SimpleNamespace(warning=lambda *args: None),
    )
    window = TradeLedgerWindow(context)
    qtbot.addWidget(window)
    legacy = {
        "id": "legacy-plan",
        "symbol": "600519",
        "status": "proposed",
        "strategy_version": "legacy_import",
    }

    window._refresh_actions([legacy], {})

    assert window.action_table.rowCount() == 0
    assert window.today_empty.isVisible() or not window.isVisible()


def test_action_queue_prioritizes_broker_truth_risk_with_clear_next_step(
    qtbot, tmp_path
) -> None:
    settings = Settings()
    store = TradeStore(tmp_path / "trades.db")
    decision = store.add_decision(
        symbol="600519", timeframe="15m", asset_class="a_share",
        original_decision={}, final_decision={}, meta={},
    )
    plan = TradePlan(
        id="risk-plan", decision_event_id=decision, symbol="600519",
        timeframe="15m", asset_class=AssetClass.A_SHARE, direction="buy",
        order_type="limit", entry_price=100, stop_loss_price=95,
        take_profit_price=110, status=PlanStatus.SUBMITTED,
        strategy_version=TOPDOWN_STRATEGY_ID,
    )
    store.add_plan(plan)
    store.append_event(
        plan.id, "major_negative_action_required",
        details={
            "negative_blocks": ["major_negative_regulatory_investigation"],
            "required_action": "核查同花顺真实委托/成交；如已有持仓，按T+1约束管理",
        },
    )
    context = SimpleNamespace(
        settings=settings, trade_store=store, broker_adapter=_Broker(),
        portfolio_risk=PortfolioRisk(settings.risk, settings.portfolio_risk),
        trading_service=SimpleNamespace(risk_settings=settings.risk),
        hotspot_service=None, universe_service=None,
        logger=SimpleNamespace(warning=lambda *args: None),
    )
    window = TradeLedgerWindow(context)
    qtbot.addWidget(window)

    window._refresh_actions([store.get_plan(plan.id)], {})

    assert window.action_table.item(0, 0).text() == "1"
    assert "重大负面事件" in window.action_table.item(0, 2).text()
    assert "核查同花顺真实委托/成交" in window.action_table.item(0, 5).text()
    assert "regulatory_investigation" in window.action_table.item(0, 6).text()


def test_invalidated_plan_stops_active_reconciliation_with_friendly_feedback(
    qtbot, tmp_path
) -> None:
    settings = Settings()
    store = TradeStore(tmp_path / "trades.db")
    decision = store.add_decision(
        symbol="600519", timeframe="15m", asset_class="a_share",
        original_decision={}, final_decision={}, meta={},
    )
    plan = TradePlan(
        id="invalidated-plan", decision_event_id=decision, symbol="600519",
        timeframe="15m", asset_class=AssetClass.A_SHARE, direction="buy",
        order_type="limit", entry_price=100, stop_loss_price=95,
        take_profit_price=110, status=PlanStatus.INVALIDATED,
        strategy_version=TOPDOWN_STRATEGY_ID,
    )
    store.add_plan(plan)
    runtime = SimpleNamespace(
        broker_snapshot=None,
        begin_reconciliation=lambda _plan_id: None,
        end_reconciliation_calls=[],
    )
    runtime.end_reconciliation = runtime.end_reconciliation_calls.append
    runtime.updated = SimpleNamespace(connect=lambda _slot: None)
    runtime.broker_snapshot_changed = SimpleNamespace(connect=lambda _slot: None)
    runtime.status_changed = SimpleNamespace(connect=lambda _slot: None)
    context = SimpleNamespace(
        settings=settings, trade_store=store, broker_adapter=_Broker(),
        portfolio_risk=PortfolioRisk(settings.risk, settings.portfolio_risk),
        trading_service=SimpleNamespace(risk_settings=settings.risk),
        hotspot_service=None, universe_service=None, quant_runtime=runtime,
        logger=SimpleNamespace(warning=lambda *args: None),
    )
    window = TradeLedgerWindow(context)
    qtbot.addWidget(window)
    window._reconciliation_order = AuthorizedOrder(
        plan_id=plan.id, account_fingerprint="account", symbol="600519",
        name="贵州茅台", direction="buy", price=100, quantity=100,
        stop_loss_price=95, strategy_id="s", authorized_at=NOW, expires_at=NOW,
    )
    window._reconciliation_timer.start()

    window._poll_reconciliation()

    assert not window._reconciliation_timer.isActive()
    assert window._reconciliation_order is None
    assert runtime.end_reconciliation_calls == [plan.id]
    assert "计划已失效" in window.activity_status_label.text()


def test_operational_plan_table_excludes_legacy_ai_research(qtbot, tmp_path) -> None:
    settings = Settings()
    store = TradeStore(tmp_path / "trades.db")
    broker = _Broker()
    context = SimpleNamespace(
        settings=settings,
        trade_store=store,
        broker_adapter=broker,
        portfolio_risk=PortfolioRisk(settings.risk, settings.portfolio_risk),
        trading_service=SimpleNamespace(risk_settings=settings.risk),
        hotspot_service=None,
        universe_service=None,
        logger=SimpleNamespace(warning=lambda *args: None),
    )
    window = TradeLedgerWindow(context)
    qtbot.addWidget(window)
    legacy = {
        "id": "legacy-plan",
        "symbol": "600519",
        "status": "proposed",
        "strategy_version": "legacy_import",
        "risk_snapshot": {},
        "direction": "buy",
        "entry_price": 100,
        "stop_loss_price": 95,
        "valid_until": NOW,
    }

    supported = {
        **legacy,
        "id": "topdown-plan",
        "strategy_version": TOPDOWN_STRATEGY_ID,
    }
    # Exercise the same operational filter used by refresh without writing
    # synthetic plans into SQLite.
    filtered = [
        plan
        for plan in [legacy, supported]
        if _prefill_strategy_is_supported(plan["strategy_version"])
    ]
    window._fill(window.pending[1], [[plan["id"]] for plan in filtered])

    assert window.pending[1].rowCount() == 1
    assert window.pending[1].item(0, 0).text() == "topdown-plan"


class _Broker:
    def __init__(self) -> None:
        self.connection = ConnectionState(
            status=BrokerConnectionStatus.DISCONNECTED,
            checked_at=NOW,
            message="同花顺未连接",
        )

    def snapshot(self) -> BrokerSnapshot:
        return BrokerSnapshot(
            connection=self.connection,
            captured_at=NOW,
            complete=False,
            warnings=["同花顺未连接"],
        )


def _manual_reconciliation_window(
    qtbot, tmp_path, *, order_updates=None, authorized=True, trusted=True
):
    now = datetime.now().astimezone()
    submitted_at = now.isoformat()
    account = "account-a"
    settings = Settings()
    settings.ths.confirmed = trusted
    settings.ths.account_fingerprint = account if trusted else ""
    settings.ths.max_quote_age_seconds = 60
    store = TradeStore(tmp_path / "trades.db")
    decision = store.add_decision(
        symbol="600519", timeframe="15m", asset_class="a_share",
        original_decision={}, final_decision={}, meta={},
    )
    plan = TradePlan(
        id="reconcile-plan", decision_event_id=decision, symbol="600519",
        timeframe="15m", asset_class=AssetClass.A_SHARE, direction="buy",
        order_type="limit", entry_price=100, stop_loss_price=95,
        take_profit_price=110, status=PlanStatus.RECONCILIATION_REQUIRED,
        strategy_version=TOPDOWN_STRATEGY_ID,
    )
    store.add_plan(plan)
    authorized_order = AuthorizedOrder(
        plan_id=plan.id, account_fingerprint=account, symbol="600519",
        name="贵州茅台", direction="buy", price=100, quantity=100,
        stop_loss_price=95, strategy_id=TOPDOWN_STRATEGY_ID,
        authorized_at=submitted_at,
        expires_at=(now + timedelta(minutes=1)).isoformat(),
    )
    if authorized:
        store.append_event(
            plan.id, "risk_authorized",
            details={"authorized_order": authorized_order.model_dump(mode="json")},
        )
    order_payload = {
        "broker_order_id": "order-1", "symbol": "600519", "direction": "buy",
        "price": 100, "quantity": 100, "filled_quantity": 100,
        "status": "已成", "submitted_at": submitted_at,
    }
    order_payload.update(order_updates or {})
    order = BrokerOrder(**order_payload)
    fill = BrokerFill(
        broker_fill_id="fill-1", broker_order_id="order-1", symbol="600519",
        direction="buy", price=100, quantity=100, fees=5,
        filled_at=submitted_at,
    )
    connection = ConnectionState(
        status=BrokerConnectionStatus.CONNECTED_READ_ONLY,
        account_fingerprint=account,
        checked_at=submitted_at,
    )
    snapshot = BrokerSnapshot(
        connection=connection, account_fingerprint=account,
        orders=[order], fills=[fill], orders_complete=trusted,
        fills_complete=trusted, captured_at=submitted_at, complete=trusted,
    )
    broker = _Broker()
    context = SimpleNamespace(
        settings=settings, trade_store=store, broker_adapter=broker,
        broker_trade_lifecycle=SimpleNamespace(
            broker_order_status=lambda *_args: ("filled", "broker_filled")
        ),
        portfolio_risk=PortfolioRisk(settings.risk, settings.portfolio_risk),
        trading_service=SimpleNamespace(risk_settings=settings.risk),
        hotspot_service=None, universe_service=None,
        logger=SimpleNamespace(warning=lambda *args: None, info=lambda *args: None),
    )
    window = TradeLedgerWindow(context)
    qtbot.addWidget(window)
    window._apply_broker_snapshot(snapshot)
    window.broker_orders.selectRow(0)
    return window, store, plan, authorized_order


def test_manual_reconciliation_blocks_untrusted_fact_tables(
    qtbot, tmp_path, monkeypatch
) -> None:
    window, store, plan, _ = _manual_reconciliation_window(
        qtbot, tmp_path, trusted=False
    )
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[2]))

    window._manually_link_broker_order()

    assert "账户事实尚未核验" not in warnings[0]
    assert "下一步" in warnings[0]
    assert store.list_broker_order_links() == []
    assert store.get_plan(plan.id)["status"] == "reconciliation_required"


@pytest.mark.parametrize(
    ("order_updates", "authorized", "expected"),
    [
        ({}, False, "缺少原始风控授权订单"),
        ({"price": 100.01}, True, "委托价格不一致"),
        ({"quantity": 200}, True, "委托数量不一致"),
        ({"submitted_at_offset_seconds": 120}, True, "委托时间超出授权窗口"),
    ],
)
def test_manual_reconciliation_explains_authorization_mismatch(
    qtbot, tmp_path, monkeypatch, order_updates, authorized, expected
) -> None:
    order_updates = dict(order_updates)
    offset = order_updates.pop("submitted_at_offset_seconds", None)
    if offset is not None:
        order_updates["submitted_at"] = (
            datetime.now().astimezone() + timedelta(seconds=offset)
        ).isoformat()
    window, store, _plan, _ = _manual_reconciliation_window(
        qtbot, tmp_path, order_updates=order_updates, authorized=authorized
    )
    warnings = []
    monkeypatch.setattr(QInputDialog, "getItem", lambda *args: (args[3][0], True))
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[2]))

    window._manually_link_broker_order()

    assert expected in warnings[0]
    assert store.list_broker_order_links() == []


def test_manual_reconciliation_accepts_only_exact_account_scoped_match(
    qtbot, tmp_path, monkeypatch
) -> None:
    window, store, plan, authorized = _manual_reconciliation_window(qtbot, tmp_path)
    monkeypatch.setattr(QInputDialog, "getItem", lambda *args: (args[3][0], True))

    window._manually_link_broker_order()

    links = store.list_broker_order_links(account_fingerprint="account-a")
    assert len(links) == 1
    assert links[0]["account_fingerprint"] == "account-a"
    assert links[0]["details"]["authorized_order"] == authorized.model_dump(mode="json")
    assert store.get_execution(plan.id)["account_fingerprint"] == "account-a"

def test_quant_status_is_fixed_above_nine_workbench_pages(qtbot, tmp_path) -> None:
    settings = Settings()
    store = TradeStore(tmp_path / "trades.db")
    store.upsert_universe_snapshot(
        UniverseSnapshot(
            as_of=date(2026, 8, 13),
            version="hs300-2026-08",
            symbols=["600519"],
            members=[CurrentUniverseMember(
                rank=1,
                symbol="600519",
                name="贵州茅台",
                industry="白酒",
                average_amount_20=4_717_613_108,
                latest_price=1343.0,
                latest_pct_chg=-0.26,
            )],
            source_kind="official_current_constituents",
            source_as_of=date(2026, 8, 12),
            input_member_count=300,
        ),
        source_updated_at="2026-08-12",
    )
    store.add_quant_signal({
        "status": "reject",
        "strategy_id": settings.strategy.strategy_id,
        "parameter_version": "1.0.0",
        "pool_version": "hs300-2026-08",
        "symbol": "600519",
        "signal_time": "2026-08-12T15:00:00+08:00",
        "reasons": ["daily_recovery_confirmed"],
        "condition_snapshot": {
            "checks": {"daily_recovery_confirmed": False},
        },
    })
    broker = _Broker()
    context = SimpleNamespace(
        settings=settings,
        trade_store=store,
        broker_adapter=broker,
        portfolio_risk=PortfolioRisk(settings.risk, settings.portfolio_risk),
        trading_service=SimpleNamespace(risk_settings=settings.risk),
        hotspot_service=None,
        universe_service=None,
        logger=SimpleNamespace(warning=lambda *args: None),
    )
    window = TradeLedgerWindow(context)
    qtbot.addWidget(window)
    window.show()
    qtbot.wait(20)

    assert window.layout().indexOf(window.tabs) == 1
    assert window.tabs.count() == 9
    assert [window.tabs.tabText(index) for index in range(9)] == [
        "今日工作台", "交易股票池", "股票详情", "交易计划", "持仓与退出",
        "月度表现", "同花顺与账户", "策略验证", "审计与设置",
    ]
    assert "数据不完整，禁止授权" in window.total_score_label.text()
    assert window.today_empty.isVisible()
    assert "系统保持空仓" in window.today_empty.text()
    assert window.broker_positions.columnCount() == 8
    assert window.broker_orders.columnCount() == 8
    assert window.broker_fills.columnCount() == 8
    assert window.broker_cash_flows.columnCount() == 7
    assert "总资产" in window.broker_funds.text()
    assert "事实表核验" in window.broker_funds.text()
    assert "持仓 未完成" in window.broker_funds.text()
    assert not window.small_live_button.isEnabled()
    assert window.import_oos_button.isEnabled()
    assert not window.run_oos_button.isEnabled()
    assert window.audit_production_oos_button.isEnabled()
    assert not window.export_production_oos_button.isEnabled()
    assert "生产 OOS 完整性审核：尚不可导出" in window.validation_summary.toPlainText()
    assert "缺少日线原始观察" in window.validation_summary.toPlainText()
    assert "缺少闭合15分钟K线" in window.validation_summary.toPlainText()
    assert "暂不可导出" in window.export_production_oos_button.toolTip()
    assert not window.prefill_enabled.isEnabled()
    assert not window.live_enabled.isEnabled()
    broker_buttons = [button.text() for button in window.broker_page.findChildren(QPushButton)]
    assert "启动同花顺（登录由用户完成）" in broker_buttons
    assert "交易笔数：0/80" in window.validation_summary.toPlainText()
    assert "小资金实盘批准：未开放" in window.validation_summary.toPlainText()
    assert "当前策略样本外数据包 v2：尚未导入" in window.validation_summary.toPlainText()
    assert "尚未导入 | 缺口 0项" not in window.validation_summary.toPlainText()
    assert "等待选择并校验数据包" in window.validation_summary.toPlainText()
    assert "交易 —/200笔" in window.validation_summary.toPlainText()
    assert "晋级结果：尚未评估" in window.validation_summary.toPlainText()
    assert "数据缺口：尚未评估（等待有效数据包）" in window.validation_summary.toPlainText()
    universe_tabs = window.universe[0].findChild(type(window.tabs))
    assert universe_tabs is not None
    assert [universe_tabs.tabText(index) for index in range(universe_tabs.count())] == [
        "当前基础池（1）", "今日候选（0）", "排除记录", "手工查询与专业评估", "历史股票池",
    ]
    assert "基础池 1只" in window.universe_status.text()
    assert "今日候选 0只" in window.universe_status.text()
    assert "热点监控 0/1只" in window.universe_status.text()
    assert "不是股票池无数据" in window.candidate_empty_label.text()
    assert not window.candidate_empty_label.isHidden()
    window.tabs.setCurrentIndex(1)
    universe_tabs.setCurrentIndex(1)
    assert window.candidate_empty_label.isVisible()
    window.score_labels["index"].clicked.emit("index")
    assert window.score_detail_panel.isVisible()
    assert "得分依据" in window.score_detail_panel.toPlainText()
    assert window.universe[1].columnCount() == 16
    assert "刷新当前股票池行情" in window.universe_generate_button.text()
    assert window.universe[1].rowCount() == 1
    assert window.universe[1].item(0, 2).text() == "贵州茅台"
    assert window.universe[1].item(0, 4).text().endswith("亿")
    assert window.universe[1].item(0, 8).text() == "未通过"
    assert "daily_recovery_confirmed" in window.universe[1].item(0, 14).text()


def test_private_pool_add_and_remove_are_in_place_and_versioned(
    qtbot, tmp_path, monkeypatch
) -> None:
    settings = Settings()
    store = TradeStore(tmp_path / "trades.db")
    store.upsert_universe_snapshot(
        UniverseSnapshot(
            as_of=date(2026, 8, 13),
            version="cloud_ai_11_v1-2026-08",
            symbols=["600519"],
            members=[CurrentUniverseMember(
                rank=1,
                symbol="600519",
                name="贵州茅台",
                average_amount_20=1_000_000,
                listing_date=date(2001, 1, 1),
            )],
            source_kind="user_fixed_theme_universe",
            source_as_of=date(2026, 8, 13),
            input_member_count=1,
        ),
        source_updated_at="2026-08-13",
    )
    start = datetime.fromisoformat("2026-07-01T15:00:00+08:00")

    def daily(_symbol: str, **_kwargs):
        return [
            {
                "time": start + timedelta(days=offset),
                "open": 10,
                "high": 10.2,
                "low": 9.8,
                "close": 10,
                "amount": 1_000_000,
            }
            for offset in range(25)
        ]

    service = ManagedAshareUniverseService(
        store,
        daily_loader=daily,
        profile_loader=lambda symbol: {
            "symbol": symbol,
            "name": "中国移动" if symbol == "600941" else "贵州茅台",
            "listing_date": "20010101",
            "industry": "通信服务" if symbol == "600941" else "白酒",
        },
        max_workers=1,
    )
    context = SimpleNamespace(
        settings=settings,
        trade_store=store,
        broker_adapter=_Broker(),
        portfolio_risk=PortfolioRisk(settings.risk, settings.portfolio_risk),
        trading_service=SimpleNamespace(risk_settings=settings.risk),
        hotspot_service=None,
        universe_service=service,
        daily_candidate_scanner=None,
        logger=SimpleNamespace(warning=lambda *args: None),
    )
    messages = []
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda *args: messages.append(args[2]),
    )
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args: messages.append(args[2]),
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args: QMessageBox.StandardButton.Yes,
    )
    window = TradeLedgerWindow(context)
    qtbot.addWidget(window)
    window.show()

    assert window.universe_member_input.placeholderText() == (
        "输入6位A股代码或完整股票名称"
    )
    assert window.universe_add_button.isEnabled()
    window.universe_member_input.setText("600941")
    window.universe_add_button.click()
    qtbot.waitUntil(
        lambda: store.list_universe_snapshots(limit=1)[0]["snapshot"]["symbols"]
        == ["600519", "600941"],
        timeout=3000,
    )
    qtbot.waitUntil(
        lambda: window._universe_mutation_thread is None,
        timeout=3000,
    )

    assert window.universe[1].rowCount() == 2
    assert window.universe_history_table.rowCount() == 2
    added_row = next(
        row
        for row in range(window.universe[1].rowCount())
        if window.universe[1].item(row, 1).text() == "600941"
    )
    window.universe[1].selectRow(added_row)
    assert window.universe_remove_button.isEnabled()
    window.universe_remove_button.click()
    qtbot.waitUntil(
        lambda: store.list_universe_snapshots(limit=1)[0]["snapshot"]["symbols"]
        == ["600519"],
        timeout=3000,
    )
    qtbot.waitUntil(
        lambda: window._universe_mutation_thread is None,
        timeout=3000,
    )

    assert window.universe[1].rowCount() == 1
    assert window.universe_history_table.rowCount() == 3
    assert any("历史版本、旧信号和旧交易仍可审计" in item for item in messages)


def test_trade_management_is_embedded_in_current_main_window(qtbot, tmp_path) -> None:
    from pa_agent.gui.main_window import MainWindow

    settings = Settings()
    store = TradeStore(tmp_path / "trades.db")
    broker = _Broker()
    context = SimpleNamespace(
        settings=settings,
        trade_store=store,
        broker_adapter=broker,
        portfolio_risk=PortfolioRisk(settings.risk, settings.portfolio_risk),
        trading_service=SimpleNamespace(risk_settings=settings.risk),
        hotspot_service=None,
        universe_service=None,
        logger=SimpleNamespace(warning=lambda *args: None),
    )

    class Host:
        def __init__(self) -> None:
            self._ctx = context
            self._page_stack = QStackedWidget()
            self._analysis_page = QWidget()
            self._page_stack.addWidget(self._analysis_page)
            self._trade_ledger_window = None
            self._status_bar = QStatusBar()

        def _show_analysis_page(self) -> None:
            self._page_stack.setCurrentWidget(self._analysis_page)

    host = Host()
    qtbot.addWidget(host._page_stack)
    qtbot.addWidget(host._status_bar)

    MainWindow._open_trade_ledger(host)
    trading_page = host._trade_ledger_window

    assert trading_page is host._page_stack.currentWidget()
    assert not trading_page.isWindow()
    assert trading_page.window() is host._page_stack
    assert host._page_stack.count() == 2
    assert trading_page.findChild(QPushButton, "returnToAnalysisButton") is not None
    assert trading_page.findChild(QPushButton, "quantRefreshButton") is not None
    assert trading_page.tabs.accessibleName() == "量化交易管理导航"

    MainWindow._open_trade_ledger(host)
    assert host._trade_ledger_window is trading_page
    assert host._page_stack.count() == 2

    trading_page.return_to_analysis_requested.emit()
    assert host._page_stack.currentWidget() is host._analysis_page


def test_workbench_shortcuts_and_risk_stage_are_visible(qtbot, tmp_path) -> None:
    settings = Settings()
    settings.risk.max_open_risk_pct = 1.5
    settings.portfolio_risk.initial_max_open_risk_pct = 1.0
    store = TradeStore(tmp_path / "trades.db")
    context = SimpleNamespace(
        settings=settings, trade_store=store, broker_adapter=_Broker(),
        portfolio_risk=PortfolioRisk(settings.risk, settings.portfolio_risk),
        trading_service=SimpleNamespace(risk_settings=settings.risk),
        hotspot_service=None, universe_service=None,
        logger=SimpleNamespace(warning=lambda *args: None),
    )
    window = TradeLedgerWindow(context)
    qtbot.addWidget(window)
    window.show()
    qtbot.wait(20)

    assert len(window._page_shortcuts) == 9
    assert "开放风险 —/1.00%" in window.risk_status_line.text()
    assert "首批阶段 0/30笔" in window.risk_status_line.text()
    assert "月度风控 数据待核验" in window.risk_status_line.text()
    assert window.monthly_warning.value() == 1.0
    assert window.highest_grade.value() == 80.0
    assert window.monthly_stop.value() == 1.5
    window.tabs.setCurrentIndex(1)
    assert "当前位置：交易股票池" in window.tabs.toolTip()
    assert "当前位置：交易股票池" in window.activity_status_label.text()


@pytest.mark.parametrize(
    ("monthly_return", "peak_drawdown", "expected"),
    [
        (None, None, "月度风控 数据待核验"),
        (-1.0, 0.0, "月度风控 仅≥80分"),
        (-1.5, 0.0, "月度风控 停止新增"),
        (2.0, 0.0, "月度风控 利润保护（风险减半）"),
        (0.5, 0.8, "月度风控 回撤保护（风险减半）"),
        (0.5, 0.2, "月度风控 正常"),
    ],
)
def test_monthly_risk_mode_is_visible_and_deterministic(
    monthly_return: float | None,
    peak_drawdown: float | None,
    expected: str,
) -> None:
    assert _monthly_risk_mode(
        monthly_return,
        peak_drawdown,
        Settings().portfolio_risk,
    ) == expected


def test_monthly_risk_settings_are_saved_into_live_portfolio_risk(
    qtbot, tmp_path, monkeypatch
) -> None:
    settings = Settings()
    store = TradeStore(tmp_path / "trades.db")
    context = SimpleNamespace(
        settings=settings,
        trade_store=store,
        broker_adapter=_Broker(),
        portfolio_risk=PortfolioRisk(settings.risk, settings.portfolio_risk),
        trading_service=SimpleNamespace(risk_settings=settings.risk),
        hotspot_service=None,
        universe_service=None,
        logger=SimpleNamespace(warning=lambda *args: None),
    )
    saved = []
    monkeypatch.setattr(
        "pa_agent.gui.trade_ledger_window.save_settings",
        lambda value, _path: saved.append(value),
    )
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
    window = TradeLedgerWindow(context)
    qtbot.addWidget(window)
    window.monthly_warning.setValue(1.2)
    window.highest_grade.setValue(85.0)
    window.monthly_stop.setValue(1.8)

    window._save_risk()

    assert saved == [settings]
    assert settings.portfolio_risk.monthly_warning_loss_pct == 1.2
    assert settings.portfolio_risk.highest_grade_score == 85.0
    assert settings.portfolio_risk.monthly_stop_loss_pct == 1.8
    assert context.portfolio_risk.settings is settings.portfolio_risk


def test_validation_page_exposes_resumable_real_market_history_backfill(
    qtbot, tmp_path
) -> None:
    settings = Settings()
    store = TradeStore(tmp_path / "trades.db")
    store.add_validation_run({
        "strategy_version": "market_sentiment_history_v1",
        "dataset": "market_history_backfill",
        "status": "data_incomplete",
        "input_hash": "d" * 64,
        "finished_at": NOW,
        "session_dates": ["2026-08-11", "2026-08-12"],
        "coverage_by_date": {"2026-08-11": 3100, "2026-08-12": 2999},
        "completed_symbols": 2999,
        "processed_symbols": 400,
        "newly_completed_symbols": 316,
        "remaining_symbols": 2301,
        "universe_count": 5300,
    }, dataset="market_history_backfill")
    runtime = SimpleNamespace(
        broker_snapshot=None,
        updated=SimpleNamespace(connect=lambda _slot: None),
        broker_snapshot_changed=SimpleNamespace(connect=lambda _slot: None),
        status_changed=SimpleNamespace(connect=lambda _slot: None),
        ensure_market_history_calls=[],
    )
    runtime.ensure_market_history = lambda **kwargs: runtime.ensure_market_history_calls.append(kwargs)
    context = SimpleNamespace(
        settings=settings, trade_store=store, broker_adapter=_Broker(),
        portfolio_risk=PortfolioRisk(settings.risk, settings.portfolio_risk),
        trading_service=SimpleNamespace(risk_settings=settings.risk),
        hotspot_service=None, universe_service=None, quant_runtime=runtime,
        strategy_stability=None,
        logger=SimpleNamespace(warning=lambda *args: None),
    )
    window = TradeLedgerWindow(context)
    qtbot.addWidget(window)

    assert "补齐真实全A 21日情绪基线" in window.market_history_button.text()
    assert "最低单日覆盖 2999/3000" in window.validation_summary.toPlainText()
    assert "本批处理 400" in window.validation_summary.toPlainText()
    assert "本批新增完整 316" in window.validation_summary.toPlainText()
    assert "待补齐 2301" in window.validation_summary.toPlainText()
    assert "为什么仍阻断" in window.validation_summary.toPlainText()
    assert "生产OOS原始观察账本" in window.validation_summary.toPlainText()
    assert "盘前不会生成15分钟记录" in window.validation_summary.toPlainText()
    window._backfill_market_history()
    assert runtime.ensure_market_history_calls == [{"force": True}]
    assert not window.market_history_button.isEnabled()


def test_workbench_friendly_search_empty_states_and_refresh_feedback(
    qtbot, tmp_path
) -> None:
    settings = Settings()
    store = TradeStore(tmp_path / "trades.db")
    store.upsert_universe_snapshot(
        UniverseSnapshot(
            as_of=date(2026, 8, 13),
            version="cloud_ai_11_v1-2026-08",
            symbols=["688158", "600941"],
            members=[
                CurrentUniverseMember(
                    rank=1,
                    symbol="688158",
                    name="优刻得-W",
                    industry="云计算",
                    average_amount_20=900_000_000,
                ),
                CurrentUniverseMember(
                    rank=2,
                    symbol="600941",
                    name="中国移动",
                    industry="通信服务",
                    average_amount_20=1_500_000_000,
                ),
            ],
            source_kind="fixed_cloud_ai_universe",
            source_as_of=date(2026, 8, 13),
            input_member_count=2,
        ),
        source_updated_at="2026-08-13",
    )
    context = SimpleNamespace(
        settings=settings,
        trade_store=store,
        broker_adapter=_Broker(),
        portfolio_risk=PortfolioRisk(settings.risk, settings.portfolio_risk),
        trading_service=SimpleNamespace(risk_settings=settings.risk),
        hotspot_service=None,
        universe_service=None,
        logger=SimpleNamespace(warning=lambda *args: None),
    )
    window = TradeLedgerWindow(context)
    qtbot.addWidget(window)
    window.resize(960, 720)
    window.show()
    qtbot.wait(20)

    assert window.header_refresh_button.text() == "立即同步"
    assert all(label.isVisible() for label in window.score_labels.values())
    assert window.guidance_line.text().startswith("下一步：")
    assert window.guidance_context_line.text().startswith("发生了什么：")
    assert "为什么：" in window.guidance_context_line.text()
    assert window.guidance_action_button.isVisible()
    assert (
        window.header_refresh_button.geometry().right()
        <= window.dashboard_header.contentsRect().right()
    )
    assert (
        window.guidance_action_button.geometry().right()
        <= window.dashboard_header.contentsRect().right()
    )
    assert not window.pending[1].isVisible()
    window.tabs.setCurrentIndex(3)
    assert window._table_empty_labels[window.pending[1]].isVisible()
    assert "当前没有待执行" in window._table_empty_labels[window.pending[1]].text()

    window.tabs.setCurrentIndex(1)
    window.universe_search.setText("中国移动")
    assert window.universe[1].isRowHidden(0)
    assert not window.universe[1].isRowHidden(1)
    assert "1/2" in window.universe_search.toolTip()
    window.universe_search.clear()
    assert not any(
        window.universe[1].isRowHidden(row)
        for row in range(window.universe[1].rowCount())
    )
    window.universe_search.setText("不存在的股票")
    assert window.universe_filter_feedback.isVisible()
    assert "没有找到" in window.universe_filter_feedback.text()
    window.universe_search.clear()
    assert not window.universe_filter_feedback.isVisible()

    window._refresh_all_now()
    assert window.header_refresh_button.text() == "同步中…"
    qtbot.wait(400)
    assert window.header_refresh_button.text() == "立即同步"
    assert "同步完成" in window.activity_status_label.text()


def test_manual_refresh_waits_for_real_runtime_tasks(qtbot, tmp_path) -> None:
    settings = Settings()
    store = TradeStore(tmp_path / "trades.db")

    class Runtime:
        broker_snapshot = None
        active_tasks = ("hotspots", "topdown")
        updated = SimpleNamespace(connect=lambda _slot: None)
        broker_snapshot_changed = SimpleNamespace(connect=lambda _slot: None)
        status_changed = SimpleNamespace(connect=lambda _slot: None)

        def __getattr__(self, _name):
            return lambda *args, **kwargs: None

    runtime = Runtime()
    context = SimpleNamespace(
        settings=settings, trade_store=store, broker_adapter=_Broker(),
        portfolio_risk=PortfolioRisk(settings.risk, settings.portfolio_risk),
        trading_service=SimpleNamespace(risk_settings=settings.risk),
        hotspot_service=None, universe_service=None, quant_runtime=runtime,
        logger=SimpleNamespace(warning=lambda *args: None),
    )
    window = TradeLedgerWindow(context)
    qtbot.addWidget(window)

    window._refresh_all_now()
    qtbot.wait(400)
    assert not window.header_refresh_button.isEnabled()
    assert "热点、四层评分" in window.activity_status_label.text()

    runtime.active_tasks = ()
    qtbot.wait(350)
    assert window.header_refresh_button.isEnabled()
    assert window.header_refresh_button.text() == "立即同步"
    assert "同步完成" in window.activity_status_label.text()


def test_stock_lookup_supports_enter_and_recovers_from_source_error(
    qtbot, tmp_path, monkeypatch
) -> None:
    settings = Settings()
    store = TradeStore(tmp_path / "trades.db")
    loaded = []
    monkeypatch.setattr(
        TradeLedgerWindow, "_load_stock_detail", lambda _self: loaded.append(True)
    )
    context = SimpleNamespace(
        settings=settings, trade_store=store, broker_adapter=_Broker(),
        portfolio_risk=PortfolioRisk(settings.risk, settings.portfolio_risk),
        trading_service=SimpleNamespace(risk_settings=settings.risk),
        hotspot_service=None, universe_service=None,
        logger=SimpleNamespace(warning=lambda *args: None),
    )
    window = TradeLedgerWindow(context)
    qtbot.addWidget(window)
    window.stock_symbol.returnPressed.emit()
    assert loaded == [True]

    window.stock_load_button.setEnabled(False)
    window.stock_load_button.setText("加载中…")
    window._show_stock_profile_error("600519", "curl: (6) Could not resolve host")
    assert window.stock_load_button.isEnabled()
    assert window.stock_load_button.text() == "重新查询并评估"
    assert "curl" not in window.stock_detail_texts["company"].toPlainText()

    window.stock_symbol.setText("中国移动")
    window._show_stock_query_error(
        "中国移动",
        "A股名称「中国移动」存在多个候选；请输入6位代码",
    )
    assert window.stock_load_button.isEnabled()
    assert window.stock_symbol.hasSelectedText()
    assert "未找到唯一的A股股票" in window.stock_detail_texts["quant"].toPlainText()
    assert "未读取行情、未评分、未生成交易计划" in (
        window.stock_detail_texts["quant"].toPlainText()
    )
    assert "尚未确认唯一的A股证券身份" in (
        window.stock_detail_texts["hotspot"].toPlainText()
    )
    assert "未生成交易计划" in window.stock_detail_texts["plan"].toPlainText()


def test_manual_stock_assessment_persists_review_without_authorizing(
    qtbot, tmp_path
) -> None:
    settings = Settings()
    store = TradeStore(tmp_path / "trades.db")
    context = SimpleNamespace(
        settings=settings,
        trade_store=store,
        broker_adapter=_Broker(),
        portfolio_risk=PortfolioRisk(settings.risk, settings.portfolio_risk),
        trading_service=SimpleNamespace(risk_settings=settings.risk),
        hotspot_service=None,
        universe_service=None,
        logger=SimpleNamespace(warning=lambda *args: None),
    )
    window = TradeLedgerWindow(context)
    qtbot.addWidget(window)
    decision = SignalDecision(
        status="allow",
        strategy_id=MANUAL_EXCEPTION_STRATEGY_ID,
        parameter_version="1.0.0+manual-exception-1.0.0",
        pool_version=(
            "manual-exception-cloud_ai_11_v1-2026-08-2026-08-12-600519"
        ),
        symbol="600519",
        signal_time="2026-08-12T15:00:00+08:00",
        condition_snapshot={
            "base_pool_version": "cloud_ai_11_v1-2026-08",
            "expected_security_name": "贵州茅台",
            "industry": "白酒",
        },
        trigger_price=100,
        max_entry_price=101,
        initial_stop=95,
        valid_until="2026-08-13T15:00:00+08:00",
    )
    evaluation = SimpleNamespace(
        base_pool_version="cloud_ai_11_v1-2026-08",
        base_scan=SimpleNamespace(decisions=[], market_breadth_pct=60.0),
        decision=decision,
    )

    window._apply_manual_stock_assessment("600519", {}, evaluation, "")

    signals = store.list_quant_signals(strategy_id=MANUAL_EXCEPTION_STRATEGY_ID)
    assert len(signals) == 1
    assert signals[0]["status"] == "allow"
    assert store.list_plans(symbol="600519") == []
    detail = window.stock_detail_texts["quant"].toPlainText()
    assert "基础股票池未修改" in detail
    assert "等待连续两根" in detail
    assert "查询不会直接授权" in detail


def test_header_guidance_opens_the_relevant_page_in_same_window(
    qtbot, tmp_path
) -> None:
    settings = Settings()
    store = TradeStore(tmp_path / "trades.db")
    context = SimpleNamespace(
        settings=settings,
        trade_store=store,
        broker_adapter=_Broker(),
        portfolio_risk=PortfolioRisk(settings.risk, settings.portfolio_risk),
        trading_service=SimpleNamespace(risk_settings=settings.risk),
        hotspot_service=None,
        universe_service=None,
        logger=SimpleNamespace(warning=lambda *args: None),
    )
    window = TradeLedgerWindow(context)
    qtbot.addWidget(window)
    window.show()
    qtbot.wait(20)

    assert window._guidance_action == "broker"
    window.guidance_action_button.click()
    assert window.tabs.currentIndex() == 6
    assert window.tabs.tabText(window.tabs.currentIndex()) == "同花顺与账户"
    assert "当前位置：同花顺与账户" in window.activity_status_label.text()


def test_header_distinguishes_client_detection_from_account_readiness(
    qtbot, tmp_path
) -> None:
    settings = Settings()
    settings.ths.confirmed = False
    store = TradeStore(tmp_path / "trades.db")

    class DetectedBroker(_Broker):
        def __init__(self) -> None:
            self.connection = ConnectionState(
                status=BrokerConnectionStatus.CONNECTED_READ_ONLY,
                checked_at=NOW,
            )

        def snapshot(self) -> BrokerSnapshot:
            return BrokerSnapshot(
                connection=self.connection,
                captured_at=NOW,
                complete=False,
            )

    context = SimpleNamespace(
        settings=settings,
        trade_store=store,
        broker_adapter=DetectedBroker(),
        portfolio_risk=PortfolioRisk(settings.risk, settings.portfolio_risk),
        trading_service=SimpleNamespace(risk_settings=settings.risk),
        hotspot_service=None,
        universe_service=None,
        logger=SimpleNamespace(warning=lambda *args: None),
    )
    window = TradeLedgerWindow(context)
    qtbot.addWidget(window)
    window.show()
    qtbot.wait(20)

    assert "客户端已连接 · 账户待确认" in window.system_status_line.text()
    assert "核对券商和脱敏资金账号" in window.guidance_line.text()


def test_monthly_page_shows_cash_flow_completeness_and_broker_rows(qtbot, tmp_path) -> None:
    settings = Settings()
    store = TradeStore(tmp_path / "trades.db")
    store.add_equity_snapshot({
        "account_fingerprint": "abc",
        "captured_at": "2026-08-01T15:00:00+08:00",
        "total_equity": 100_000,
        "available_cash": 100_000,
        "position_value": 0,
        "complete": True,
    })
    store.add_equity_snapshot({
        "account_fingerprint": "abc",
        "captured_at": NOW,
        "total_equity": 111_000,
        "available_cash": 111_000,
        "position_value": 0,
        "complete": True,
    }, external_cash_flow=10_000)
    flow = BrokerCashFlow(
        broker_flow_id="F1", direction="deposit", amount=10_000,
        occurred_at="2026-08-05T10:00:00+08:00", status="成功",
        description="银转证",
    )
    store.upsert_broker_cash_flows(
        "abc", [flow], captured_at=NOW,
        range_start="2026-08-01T00:00:00+08:00", range_end=NOW,
        complete=True,
    )

    class CompleteBroker(_Broker):
        def snapshot(self) -> BrokerSnapshot:
            return BrokerSnapshot(
                connection=ConnectionState(
                    status=BrokerConnectionStatus.CONNECTED_READ_ONLY,
                    checked_at=NOW,
                ),
                account_fingerprint="abc", total_equity=111_000,
                available_cash=111_000, position_value=0,
                cash_flows=[flow], cash_flow_complete=True,
                cash_flow_range_start="2026-08-01T00:00:00+08:00",
                cash_flow_range_end=NOW, captured_at=NOW, complete=True,
            )

    broker = CompleteBroker()
    broker.connection = broker.snapshot().connection
    context = SimpleNamespace(
        settings=settings, trade_store=store, broker_adapter=broker,
        portfolio_risk=PortfolioRisk(settings.risk, settings.portfolio_risk),
        trading_service=SimpleNamespace(risk_settings=settings.risk),
        hotspot_service=None, universe_service=None,
        logger=SimpleNamespace(warning=lambda *args: None),
    )
    window = TradeLedgerWindow(context)
    qtbot.addWidget(window)
    window.show()
    qtbot.wait(20)

    assert window.broker_cash_flows.rowCount() == 1
    assert window.broker_cash_flows.item(0, 1).text() == "入金"
    assert "本月入金：10000.00" in window.monthly_summary.toPlainText()
    assert "扣除出入金后的月度收益：+1.00%" in window.monthly_summary.toPlainText()
    assert "资金流水完整性：已核验" in window.monthly_summary.toPlainText()
    assert "数据缺口：无" in window.monthly_summary.toPlainText()

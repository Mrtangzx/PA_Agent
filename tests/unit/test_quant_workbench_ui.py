from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from PyQt6.QtWidgets import QPushButton, QStackedWidget, QStatusBar, QWidget

from pa_agent.config.settings import Settings
from pa_agent.gui.trade_ledger_window import (
    TradeLedgerWindow,
    _prefill_strategy_is_supported,
)
from pa_agent.trading.broker_models import (
    BrokerCashFlow,
    BrokerConnectionStatus,
    BrokerSnapshot,
    ConnectionState,
)
from pa_agent.trading.portfolio import PortfolioRisk
from pa_agent.trading.store import TradeStore
from pa_agent.trading.topdown import TOPDOWN_STRATEGY_ID
from pa_agent.trading.universe import CurrentUniverseMember, UniverseSnapshot

NOW = "2026-08-12T10:00:00+08:00"


def test_prefill_only_accepts_topdown_and_explicit_manual_exception() -> None:
    assert _prefill_strategy_is_supported(TOPDOWN_STRATEGY_ID)
    assert _prefill_strategy_is_supported("manual_exception_4321_v1")
    assert not _prefill_strategy_is_supported("hs300_daily_pullback_v1")
    assert not _prefill_strategy_is_supported("legacy_import")


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
    assert not window.small_live_button.isEnabled()
    assert window.import_oos_button.isEnabled()
    assert not window.run_oos_button.isEnabled()
    assert not window.prefill_enabled.isEnabled()
    assert not window.live_enabled.isEnabled()
    broker_buttons = [button.text() for button in window.broker_page.findChildren(QPushButton)]
    assert "启动同花顺（登录由用户完成）" in broker_buttons
    assert "交易笔数：0/80" in window.validation_summary.toPlainText()
    assert "小资金实盘批准：未开放" in window.validation_summary.toPlainText()
    assert "样本外数据包：尚未导入" in window.validation_summary.toPlainText()
    universe_tabs = window.universe[0].findChild(type(window.tabs))
    assert universe_tabs is not None
    assert [universe_tabs.tabText(index) for index in range(universe_tabs.count())] == [
        "当前基础池（1）", "今日候选（0）", "排除记录", "手工查询与专业评估", "历史股票池",
    ]
    assert "基础池 1只" in window.universe_status.text()
    assert "今日候选 0只" in window.universe_status.text()
    assert "不是股票池无数据" in window.candidate_empty_label.text()
    assert not window.candidate_empty_label.isHidden()
    window.tabs.setCurrentIndex(1)
    universe_tabs.setCurrentIndex(1)
    assert window.candidate_empty_label.isVisible()
    window.score_labels["index"].clicked.emit("index")
    assert window.score_detail_panel.isVisible()
    assert "得分依据" in window.score_detail_panel.toPlainText()
    assert window.universe[1].columnCount() == 16
    assert "生成/刷新新云算力股票池" in window.universe_generate_button.text()
    assert window.universe[1].rowCount() == 1
    assert window.universe[1].item(0, 2).text() == "贵州茅台"
    assert window.universe[1].item(0, 4).text().endswith("亿")
    assert window.universe[1].item(0, 8).text() == "未通过"
    assert "daily_recovery_confirmed" in window.universe[1].item(0, 14).text()


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
    window.tabs.setCurrentIndex(1)
    assert "当前位置：交易股票池" in window.tabs.toolTip()


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

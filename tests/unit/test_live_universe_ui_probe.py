from __future__ import annotations

import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from pa_agent.config.settings import Settings
from pa_agent.gui.trade_ledger_window import TradeLedgerWindow
from pa_agent.trading.broker_models import (
    BrokerConnectionStatus,
    BrokerSnapshot,
    ConnectionState,
)
from pa_agent.trading.portfolio import PortfolioRisk
from pa_agent.trading.store import TradeStore


class _ReadOnlyBroker:
    def __init__(self) -> None:
        self.connection = ConnectionState(
            status=BrokerConnectionStatus.DISCONNECTED,
            checked_at="2026-08-13T11:00:00+08:00",
            message="probe",
        )

    def snapshot(self) -> BrokerSnapshot:
        return BrokerSnapshot(
            connection=self.connection,
            captured_at=self.connection.checked_at,
            complete=False,
        )


@pytest.mark.skipif(
    not os.environ.get("PA_AGENT_LIVE_UI_PROBE"),
    reason="requires the workspace's live trade database",
)
def test_live_universe_ui_probe(qtbot) -> None:
    root = Path(__file__).resolve().parents[2]
    store = TradeStore(root / "trade_records" / "trades.db")
    settings = Settings()
    broker = _ReadOnlyBroker()
    context = SimpleNamespace(
        settings=settings,
        trade_store=store,
        broker_adapter=broker,
        portfolio_risk=PortfolioRisk(settings.risk, settings.portfolio_risk),
        trading_service=SimpleNamespace(risk_settings=settings.risk),
        hotspot_service=None,
        universe_service=None,
        logger=logging.getLogger("pa_agent.live_ui_probe"),
    )
    window = TradeLedgerWindow(context)
    qtbot.addWidget(window)
    window.show()
    window.tabs.setCurrentIndex(1)
    window.universe_tabs.setCurrentIndex(1)
    qtbot.wait(50)

    assert window.universe[1].rowCount() == 30
    assert window.candidate_table.rowCount() == 0
    assert window.universe_tabs.tabText(0) == "当前基础池（30）"
    assert window.universe_tabs.tabText(1) == "今日候选（0）"
    assert "基础池 30只" in window.universe_status.text()
    assert "日线扫描 30只" in window.universe_status.text()
    assert "今日候选 0只" in window.universe_status.text()
    assert "不是股票池无数据" in window.candidate_empty_label.text()
    assert window.candidate_empty_label.isVisible()
    assert "当前状态：CANDIDATE" in window.validation_summary.toPlainText()
    assert "通过 10/10 项" in window.validation_summary.toPlainText()
    assert "日线+15分钟组合样本外回测：尚未运行" in window.validation_summary.toPlainText()
    assert "交易笔数：0/80" in window.validation_summary.toPlainText()
    assert "小资金实盘批准：未开放" in window.validation_summary.toPlainText()
    assert not window.small_live_button.isEnabled()
    assert not window.run_oos_button.isEnabled()
    assert not window.live_enabled.isEnabled()
    assert not window.prefill_enabled.isEnabled()

    output = Path(os.environ["PA_AGENT_LIVE_UI_PROBE"])
    assert window.grab().save(str(output))

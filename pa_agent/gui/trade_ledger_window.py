"""Independent wide local trade ledger window."""
from __future__ import annotations

import json
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from pa_agent.config.paths import SETTINGS_JSON_PATH
from pa_agent.config.settings import save_settings
from pa_agent.gui.trade_dialogs import ExitDialog, InstrumentProfileDialog
from pa_agent.trading.profiles import default_profile


class TradeLedgerWindow(QMainWindow):
    def __init__(self, ctx, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.store = ctx.trade_store
        self.setWindowTitle("PA Agent 交易台账（本地）")
        self.resize(1380, 820)
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self.pending = self._table_tab(["计划ID", "品种", "周期", "方向", "订单", "入场", "止损", "TP1", "状态", "影子状态"])
        self.open_positions = self._table_tab(["计划ID", "品种/合约", "成交价", "数量", "当前风险", "浮动R", "退出检测"])
        self.closed = self._table_tab(["计划ID", "数据集", "品种", "结果", "毛收益", "净收益", "R", "MFE(R)", "MAE(R)", "持有K线"])
        self.tabs.addTab(self.pending[0], "待处理")
        self.tabs.addTab(self.open_positions[0], "实际持仓")
        self.tabs.addTab(self.closed[0], "已结束")
        self.tabs.addTab(self._build_statistics(), "策略统计")
        self.tabs.addTab(self._build_audit(), "审计详情")
        self.tabs.addTab(self._build_config(), "配置")
        self._add_actions()
        for _, table in (self.pending, self.open_positions, self.closed):
            table.doubleClicked.connect(self._show_selected_audit)
        self.refresh()

    def _table_tab(self, headers: list[str]) -> tuple[QWidget, QTableWidget]:
        page = QWidget(); layout = QVBoxLayout(page)
        table = QTableWidget(0, len(headers)); table.setHorizontalHeaderLabels(headers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(table)
        return page, table

    def _add_actions(self) -> None:
        pending_layout = self.pending[0].layout()
        row = QHBoxLayout()
        refresh = QPushButton("刷新"); refresh.clicked.connect(self.refresh)
        audit = QPushButton("查看所选审计"); audit.clicked.connect(self._show_selected_audit)
        row.addWidget(refresh); row.addWidget(audit); row.addStretch(1)
        pending_layout.insertLayout(0, row)

        open_layout = self.open_positions[0].layout()
        exit_button = QPushButton("确认所选持仓已退出")
        exit_button.clicked.connect(self._confirm_selected_exit)
        open_layout.insertWidget(0, exit_button)

        closed_layout = self.closed[0].layout()
        exports = QHBoxLayout()
        for dataset, label in (("actual", "导出实际交易 CSV"), ("shadow", "导出影子交易 CSV")):
            button = QPushButton(label); button.clicked.connect(lambda _=False, d=dataset: self._export(d)); exports.addWidget(button)
        exports.addStretch(1); closed_layout.insertLayout(0, exports)

    def _build_statistics(self) -> QWidget:
        page = QWidget(); layout = QVBoxLayout(page)
        note = QLabel("统计严格分为 actual 与 shadow 两个数据集，不允许混合。")
        filters = QHBoxLayout()
        self.stats_filters: dict[str, QLineEdit] = {}
        for key, hint in (
            ("asset_class", "资产 a_share/cn_futures"), ("symbol", "品种"),
            ("timeframe", "周期"), ("market_state", "市场状态"),
            ("order_type", "订单类型"), ("strategy_version", "策略版本"),
        ):
            edit = QLineEdit(); edit.setPlaceholderText(hint); self.stats_filters[key] = edit; filters.addWidget(edit)
        apply_button = QPushButton("应用筛选"); apply_button.clicked.connect(self.refresh); filters.addWidget(apply_button)
        self.stats_text = QTextEdit(); self.stats_text.setReadOnly(True)
        layout.addWidget(note); layout.addLayout(filters); layout.addWidget(self.stats_text)
        return page

    def _build_audit(self) -> QWidget:
        page = QWidget(); layout = QVBoxLayout(page)
        self.audit_text = QTextEdit(); self.audit_text.setReadOnly(True)
        from pa_agent.gui.chart_widget import ChartWidget

        self.audit_chart = ChartWidget()
        self.audit_chart.setMinimumHeight(320)
        layout.addWidget(self.audit_text, 1)
        layout.addWidget(self.audit_chart, 2)
        return page

    def _build_config(self) -> QWidget:
        page = QWidget(); layout = QVBoxLayout(page)
        form = QFormLayout(); risk = self.ctx.settings.risk
        self.equity = _pct_spin(risk.account_equity or 0, maximum=1_000_000_000)
        self.cash = _pct_spin(risk.available_cash or 0, maximum=1_000_000_000)
        self.per_trade = _pct_spin(risk.per_trade_risk_pct)
        self.max_open = _pct_spin(risk.max_open_risk_pct)
        self.daily = _pct_spin(risk.daily_loss_warning_pct)
        self.weekly = _pct_spin(risk.weekly_loss_warning_pct)
        form.addRow("账户权益（必填后才计算数量）", self.equity)
        form.addRow("可用资金", self.cash)
        form.addRow("默认单笔风险 %", self.per_trade)
        form.addRow("最大未平仓总风险 %", self.max_open)
        form.addRow("单日亏损警戒 %", self.daily)
        form.addRow("单周亏损警戒 %", self.weekly)
        layout.addLayout(form)
        save = QPushButton("保存账户风险配置"); save.clicked.connect(self._save_risk); layout.addWidget(save)
        profile_row = QHBoxLayout()
        self.profile_symbol = QTextEdit(); self.profile_symbol.setMaximumHeight(36); self.profile_symbol.setPlaceholderText("输入品种代码，如 600519 或 AU0")
        edit_profile = QPushButton("编辑品种制度与成本"); edit_profile.clicked.connect(self._edit_profile)
        profile_row.addWidget(self.profile_symbol); profile_row.addWidget(edit_profile); layout.addLayout(profile_row)
        self.health_label = QLabel(); self.health_label.setWordWrap(True); layout.addWidget(self.health_label)
        layout.addStretch(1)
        return page

    def refresh(self) -> None:
        health = self.store.health()
        self.health_label.setText(
            f"SQLite: {'可用' if health['available'] else '故障'} · {health['path']}"
            + (f" · {health['error']}" if health["error"] else "")
        )
        if not health["available"]:
            return
        plans = self.store.list_plans()
        pending_rows = [p for p in plans if p["status"] in {"proposed", "ignored", "expired", "invalidated"}]
        self._fill(self.pending[1], [[
            p["id"], p["symbol"], p["timeframe"], p["direction"], p["order_type"], p["entry_price"],
            p["stop_loss_price"], p["take_profit_price"], p["status"], p["shadow_status"],
        ] for p in pending_rows])
        actual = [p for p in plans if p["status"] in {"executed_open", "exit_detected"}]
        actual_rows = []
        for p in actual:
            execution = self.store.get_execution(p["id"]) or {}
            risk = p.get("risk_snapshot") or {}
            quantity = float(execution.get("quantity") or 0)
            entry = float(execution.get("price") or 0)
            stop_distance = abs(entry - float(p["stop_loss_price"])) if entry else 0
            last_price = p.get("last_price")
            direction_sign = 1 if "多" in p["direction"] or str(p["direction"]).lower() in {"long", "buy"} else -1
            profile = self.store.get_profile(p["symbol"])
            multiplier = float(profile.contract_multiplier) if profile and profile.contract_multiplier else 1.0
            floating_r = None
            if last_price is not None and stop_distance > 0:
                floating_r = (float(last_price) - entry) * direction_sign / stop_distance
            current_risk = stop_distance * quantity * multiplier if quantity else risk.get("planned_risk", "无法计算")
            actual_rows.append([
                p["id"], execution.get("real_contract") or p["symbol"], execution.get("price", ""),
                execution.get("quantity", ""), current_risk,
                "—" if floating_r is None else f"{floating_r:.2f}R",
                "待人工确认" if p["status"] == "exit_detected" else "未触发",
            ])
        self._fill(self.open_positions[1], actual_rows)
        results = self.store.list_results()
        self._fill(self.closed[1], [[
            r["plan_id"], r["dataset"], r["symbol"], r["outcome"], r["gross_pnl"], r["net_pnl"],
            r["r_multiple"], r["mfe_r"], r["mae_r"], r["holding_bars"],
        ] for r in results])
        filters = {key: edit.text().strip() for key, edit in self.stats_filters.items()}
        self.stats_text.setPlainText(json.dumps({
            "实际交易 actual": self.store.statistics(dataset="actual", **filters),
            "影子交易 shadow": self.store.statistics(dataset="shadow", **filters),
        }, ensure_ascii=False, indent=2))

    def _show_selected_audit(self, _index=None) -> None:
        current_index = self.tabs.currentIndex()
        table = self.pending[1] if current_index == 0 else self.open_positions[1] if current_index == 1 else self.closed[1] if current_index == 2 else None
        if table is None:
            return
        row = table.currentRow()
        if row < 0:
            return
        plan_id = table.item(row, 0).text()
        plan = self.store.get_plan(plan_id)
        if not plan:
            return
        decision = self.store.get_decision(plan["decision_event_id"])
        events = self.store.list_events(plan_id)
        self.audit_text.setPlainText(json.dumps({"plan": plan, "decision": decision, "events": events}, ensure_ascii=False, indent=2, default=str))
        record_path = Path(plan.get("analysis_record_ref") or "")
        if record_path.is_file():
            try:
                from pa_agent.demo.record_loader import frame_from_record_klines, load_analysis_record

                record = load_analysis_record(record_path)
                frame = frame_from_record_klines(
                    record.kline_data, symbol=record.meta.symbol, timeframe=record.meta.timeframe,
                    snapshot_ts_local_ms=record.meta.timestamp_local_ms,
                )
                self.audit_chart.set_frame(frame)
                self.audit_chart.set_decision((decision or {}).get("final_decision") or {})
                self.audit_chart.fit_view()
            except Exception:  # noqa: BLE001
                self.audit_chart.clear_decision_overlay()
        self.tabs.setCurrentIndex(4)

    def _confirm_selected_exit(self) -> None:
        row = self.open_positions[1].currentRow()
        if row < 0:
            return
        plan = self.store.get_plan(self.open_positions[1].item(row, 0).text())
        if not plan:
            return
        dialog = ExitDialog(plan, self)
        if dialog.exec():
            try:
                self.store.confirm_exit(plan["id"], **dialog.values())
                self.refresh()
            except Exception as exc:  # noqa: BLE001
                QMessageBox.warning(self, "关闭失败", str(exc))

    def _export(self, dataset: str) -> None:
        filename, _ = QFileDialog.getSaveFileName(self, "导出 CSV", f"trade_{dataset}.csv", "CSV (*.csv)")
        if filename:
            filters = {key: edit.text().strip() for key, edit in self.stats_filters.items()}
            self.store.export_csv(Path(filename), dataset=dataset, **filters)

    def _save_risk(self) -> None:
        risk = self.ctx.settings.risk
        risk.account_equity = self.equity.value() or None
        risk.available_cash = self.cash.value() or None
        risk.per_trade_risk_pct = self.per_trade.value(); risk.max_open_risk_pct = self.max_open.value()
        risk.daily_loss_warning_pct = self.daily.value(); risk.weekly_loss_warning_pct = self.weekly.value()
        save_settings(self.ctx.settings, SETTINGS_JSON_PATH)
        self.ctx.trading_service.risk_settings = risk
        QMessageBox.information(self, "已保存", "风险配置已保存在本地 settings.json。")

    def _edit_profile(self) -> None:
        symbol = self.profile_symbol.toPlainText().strip().upper()
        if not symbol:
            return
        profile = self.store.get_profile(symbol)
        if profile is None:
            source = getattr(self.ctx.settings.general, "last_data_source", "")
            profile = default_profile(symbol, source, getattr(self.ctx.settings.general, "kline_adjust", ""))
        dialog = InstrumentProfileDialog(profile, self)
        if dialog.exec():
            self.store.upsert_profile(dialog.value()); self.refresh()

    @staticmethod
    def _fill(table: QTableWidget, rows: list[list]) -> None:
        table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for col_index, value in enumerate(row):
                item = QTableWidgetItem("" if value is None else str(value))
                item.setData(Qt.ItemDataRole.UserRole, value)
                table.setItem(row_index, col_index, item)
        table.resizeColumnsToContents()


def _pct_spin(value: float, maximum: float = 100.0) -> QDoubleSpinBox:
    spin = QDoubleSpinBox(); spin.setRange(0, maximum); spin.setDecimals(4); spin.setValue(float(value)); return spin

"""Compact home-page monitor for the full deterministic A-share stock pool."""
from __future__ import annotations

from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


class StockPoolMonitor(QWidget):
    """A live read-only view over per-stock sandbox snapshots."""

    symbol_selected = pyqtSignal(str)
    open_trading_requested = pyqtSignal()
    refresh_requested = pyqtSignal()

    _COLUMNS = (
        "交易状态",
        "股票",
        "最新15m收盘",
        "日线形态",
        "4:3:2:1",
        "确认进度",
        "热点 / 风险",
        "交易计划",
        "下一步",
    )

    def __init__(self, ctx: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.store = getattr(ctx, "trade_store", None)
        self.setObjectName("stockPoolMonitor")
        self.setMinimumHeight(270)
        self.setMaximumHeight(360)
        self._expanded = True
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(7)

        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        title = QLabel("股票池实时监控")
        title.setObjectName("stockPoolMonitorTitle")
        title.setStyleSheet("font-size:15px; font-weight:700; color:#f0f6fc;")
        title_row.addWidget(title)
        self.summary_label = QLabel("正在载入每股独立沙箱…")
        self.summary_label.setObjectName("stockPoolMonitorSummary")
        self.summary_label.setStyleSheet("color:#8b949e; font-weight:500;")
        title_row.addWidget(self.summary_label, stretch=1)

        self.feishu_label = QLabel("飞书 · 待检查")
        self.feishu_label.setObjectName("stockPoolFeishuStatus")
        title_row.addWidget(self.feishu_label)

        refresh_button = QPushButton("立即刷新")
        refresh_button.setObjectName("stockPoolRefreshButton")
        refresh_button.setToolTip("刷新股票池、热点和最近闭合15分钟评分")
        refresh_button.clicked.connect(self.refresh_requested)
        title_row.addWidget(refresh_button)

        trading_button = QPushButton("交易计划与账户")
        trading_button.setObjectName("stockPoolOpenTradingButton")
        trading_button.setToolTip("在当前窗口进入详细交易管理，不会打开第二个窗口")
        trading_button.clicked.connect(self.open_trading_requested)
        title_row.addWidget(trading_button)

        self.toggle_button = QPushButton("收起")
        self.toggle_button.setObjectName("stockPoolToggleButton")
        self.toggle_button.setMaximumWidth(58)
        self.toggle_button.clicked.connect(self._toggle_table)
        title_row.addWidget(self.toggle_button)
        root.addLayout(title_row)

        self.runtime_label = QLabel("全池调度 · 等待后台状态")
        self.runtime_label.setObjectName("stockPoolRuntimeStatus")
        self.runtime_label.setStyleSheet("color:#7d8590; font-size:11px;")
        root.addWidget(self.runtime_label)

        self.table = QTableWidget(0, len(self._COLUMNS))
        self.table.setObjectName("stockPoolSandboxTable")
        self.table.setHorizontalHeaderLabels(self._COLUMNS)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(29)
        header = self.table.horizontalHeader()
        for column in range(len(self._COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(8, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._emit_selected_symbol)
        self.table.itemDoubleClicked.connect(lambda _item: self._emit_selected_symbol())
        root.addWidget(self.table, stretch=1)

        self.empty_label = QLabel(
            "股票池正在初始化。后台会持续扫描全部A股池成员；没有候选不等于没有股票池数据。"
        )
        self.empty_label.setObjectName("stockPoolMonitorEmpty")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_label.setStyleSheet(
            "padding:14px; color:#8b949e; background:#0f141b; border-radius:5px;"
        )
        root.addWidget(self.empty_label)

        self.setStyleSheet(
            "QWidget#stockPoolMonitor {"
            "background:#161b22; border:1px solid #30363d; border-radius:7px;"
            "}"
            "QTableWidget#stockPoolSandboxTable {"
            "background:#0d1117; alternate-background-color:#111821;"
            "border:0; border-top:1px solid #21262d; color:#c9d1d9;"
            "selection-background-color:#1f3b55; selection-color:#f0f6fc;"
            "}"
            "QTableWidget#stockPoolSandboxTable::item { padding:4px 7px; }"
            "QPushButton#stockPoolOpenTradingButton {"
            "background:#1f6f56; color:#f0fff8; border:1px solid #2b8a6e;"
            "font-weight:600; padding:5px 10px; border-radius:4px;"
            "}"
            "QPushButton#stockPoolOpenTradingButton:hover { background:#258064; }"
            "QPushButton#stockPoolOpenTradingButton:pressed { padding-top:6px; }"
        )

    def refresh(self) -> None:
        if self.store is None or not getattr(self.store, "available", False):
            self._show_empty("量化数据库不可用，无法读取股票池实时状态。")
            return
        universes = self.store.list_universe_snapshots(limit=1)
        if not universes:
            self._show_empty("股票池尚未生成，后台正在初始化A股交易范围。")
            return
        pool = universes[0].get("snapshot") or {}
        pool_version = str(pool.get("version") or "")
        records = self.store.list_stock_sandboxes(pool_version=pool_version, limit=500)
        snapshots = [item.get("snapshot") or {} for item in records]
        snapshots.sort(key=lambda item: (
            int(item.get("action_priority") or 999),
            str(item.get("symbol") or ""),
        ))
        if not snapshots:
            self._show_empty(
                f"{pool_version} 已载入 {len(pool.get('symbols') or [])} 只股票，"
                "正在生成逐股交易状态。"
            )
            self._update_feishu_status()
            return
        self.empty_label.hide()
        self.table.show()
        self.table.setRowCount(len(snapshots))
        for row, snapshot in enumerate(snapshots):
            self._fill_row(row, snapshot)
        counts: dict[str, int] = {}
        for item in snapshots:
            state = str(item.get("state") or "unknown")
            counts[state] = counts.get(state, 0) + 1
        tradeable = counts.get("quant_tradeable", 0)
        confirming = counts.get("wait_confirmation", 0)
        candidates = sum(
            counts.get(key, 0)
            for key in ("intraday_observing", "wait_confirmation", "quant_tradeable")
        )
        risks = (
            counts.get("major_risk_blocked", 0)
            + counts.get("invalidated", 0)
            + counts.get("exit_required", 0)
        )
        self.summary_label.setText(
            f"全池 {len(snapshots)}只 · 日线候选 {candidates} · "
            f"待确认 {confirming} · 可交易 {tradeable} · 风险事项 {risks}"
        )
        self.summary_label.setStyleSheet(
            "color:#3fb950; font-weight:600;" if tradeable
            else "color:#8b949e; font-weight:500;"
        )
        self._update_feishu_status()

    def set_runtime_status(self, task: str, detail: str) -> None:
        labels = {
            "universe": "股票池",
            "daily_candidates": "日线扫描",
            "hotspots": "热点监控",
            "topdown": "15分钟评分",
            "feishu": "飞书提醒",
            "broker": "交易账户",
            "lifecycle": "持仓退出",
        }
        self.runtime_label.setText(f"{labels.get(task, '全池调度')} · {detail}")

    def _fill_row(self, row: int, snapshot: dict[str, Any]) -> None:
        score = snapshot.get("total_score")
        score_text = "—" if score is None else f"{float(score):.1f}"
        confirm_text = (
            f"{int(snapshot.get('consecutive_pass_count') or 0)}/2"
            if score is not None else "—"
        )
        trigger = snapshot.get("trigger_price")
        maximum = snapshot.get("max_entry_price")
        stop = snapshot.get("initial_stop")
        plan_text = "无计划"
        if trigger is not None:
            plan_text = f"入 {trigger:g} / 上限 {maximum:g}" if maximum is not None else f"入 {trigger:g}"
            if stop is not None:
                plan_text += f" / 止损 {stop:g}"
        hotspot_text = snapshot.get("hotspot_title") or snapshot.get("hotspot_status") or "—"
        values = (
            snapshot.get("state_label") or "—",
            f"{snapshot.get('name') or ''} {snapshot.get('symbol') or ''}".strip(),
            _number(snapshot.get("latest_price")),
            snapshot.get("daily_status") or "—",
            score_text,
            confirm_text,
            hotspot_text,
            plan_text,
            snapshot.get("action") or "—",
        )
        tooltip = "\n".join(filter(None, (
            f"状态：{snapshot.get('state_label') or '—'}",
            f"评分状态：{snapshot.get('score_status') or '—'}",
            f"硬阻断：{', '.join(snapshot.get('hard_blocks') or [])}"
            if snapshot.get("hard_blocks") else "",
            f"数据缺口：{', '.join(snapshot.get('data_gaps') or [])}"
            if snapshot.get("data_gaps") else "",
            f"有效期：{snapshot.get('valid_until') or '—'}",
        )))
        state_color = _state_color(str(snapshot.get("state") or ""))
        for column, value in enumerate(values):
            item = QTableWidgetItem(str(value))
            item.setData(Qt.ItemDataRole.UserRole, snapshot.get("symbol"))
            item.setToolTip(tooltip)
            if column == 0:
                item.setForeground(QColor(state_color))
            if column in {2, 4, 5}:
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
            self.table.setItem(row, column, item)

    def _show_empty(self, text: str) -> None:
        self.table.hide()
        self.empty_label.setText(text)
        self.empty_label.show()
        self.summary_label.setText("全池状态尚未就绪")
        self._update_feishu_status()

    def _update_feishu_status(self) -> None:
        feishu = getattr(getattr(self.ctx, "settings", None), "feishu", None)
        configured = bool(
            feishu
            and getattr(feishu, "enabled", True)
            and str(getattr(feishu, "webhook_url", "") or "").strip()
        )
        if not configured:
            self.feishu_label.setText("飞书 · 未配置")
            self.feishu_label.setStyleSheet("color:#d29922;")
            return
        notifications = self.store.list_quant_notifications(limit=1)
        if notifications:
            status = notifications[0].get("status")
            label = {"delivered": "已发送", "failed": "最近失败", "pending": "发送中"}.get(
                status, "已启用"
            )
            self.feishu_label.setText(f"飞书 · {label}")
            self.feishu_label.setStyleSheet(
                "color:#3fb950;" if status == "delivered" else "color:#d29922;"
            )
        else:
            self.feishu_label.setText("飞书 · 已启用")
            self.feishu_label.setStyleSheet("color:#58a6ff;")

    def _emit_selected_symbol(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        item = self.table.item(row, 0)
        symbol = str(item.data(Qt.ItemDataRole.UserRole) or "") if item else ""
        if symbol:
            self.symbol_selected.emit(symbol)

    def _toggle_table(self) -> None:
        self._expanded = not self._expanded
        self.table.setVisible(self._expanded and self.table.rowCount() > 0)
        self.empty_label.setVisible(self._expanded and self.table.rowCount() == 0)
        self.runtime_label.setVisible(self._expanded)
        self.toggle_button.setText("收起" if self._expanded else "展开")
        self.setMaximumHeight(360 if self._expanded else 52)
        self.setMinimumHeight(270 if self._expanded else 52)


def _number(value: Any) -> str:
    if value in (None, ""):
        return "—"
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value)


def _state_color(state: str) -> str:
    if state in {"quant_tradeable", "authorized"}:
        return "#3fb950"
    if state in {
        "major_risk_blocked", "invalidated", "exit_required", "account_risk_blocked"
    }:
        return "#f85149"
    if state in {"wait_confirmation", "data_incomplete"}:
        return "#d29922"
    if state in {"submitted", "partially_filled", "filled", "waiting_user_confirmation"}:
        return "#58a6ff"
    return "#8b949e"

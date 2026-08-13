"""Private quant trading workbench with TongHuaShun binding and audit views."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QObject, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from pa_agent.brokers.ths_adapter import ThsBrokerAdapter
from pa_agent.config.paths import SETTINGS_JSON_PATH
from pa_agent.config.settings import save_settings
from pa_agent.gui.trade_dialogs import ExitDialog, InstrumentProfileDialog
from pa_agent.trading.profiles import default_profile
from pa_agent.trading.promotion import (
    StrategyPromotionService,
    build_shadow_performance_evidence,
)
from pa_agent.trading.quant import SignalDecision, StrategyState
from pa_agent.trading.topdown import (
    TOPDOWN_STRATEGY_ID,
    TopDownScoreSnapshot,
)


def _prefill_strategy_is_supported(strategy_version: str) -> bool:
    """Only the active 4:3:2:1 route and its explicit exception may reach prefill."""
    return strategy_version in {TOPDOWN_STRATEGY_ID, "manual_exception_4321_v1"}


class _StockProfileWorker(QObject):
    finished = pyqtSignal(str, object)
    failed = pyqtSignal(str, str)

    def __init__(self, symbol: str) -> None:
        super().__init__()
        self.symbol = symbol

    def run(self) -> None:
        try:
            from pa_agent.data.eastmoney_extended import fetch_stock_extended_profile

            self.finished.emit(self.symbol, fetch_stock_extended_profile(self.symbol))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(self.symbol, str(exc))


class _HotspotBatchWorker(QObject):
    snapshot_ready = pyqtSignal(object)
    failed = pyqtSignal(str, str)
    finished = pyqtSignal()

    def __init__(self, service, symbols: list[str]) -> None:
        super().__init__()
        self.service = service
        self.symbols = symbols

    def run(self) -> None:
        for symbol in self.symbols:
            if QThread.currentThread().isInterruptionRequested():
                break
            try:
                self.snapshot_ready.emit(self.service.freeze(symbol))
            except Exception as exc:
                self.failed.emit(symbol, str(exc))
        self.finished.emit()


class _TopDownBatchWorker(QObject):
    score_ready = pyqtSignal(object, object)
    failed = pyqtSignal(str, str)
    finished = pyqtSignal()

    def __init__(self, service, jobs: list[dict]) -> None:
        super().__init__()
        self.service = service
        self.jobs = jobs

    def run(self) -> None:
        for job in self.jobs:
            if QThread.currentThread().isInterruptionRequested():
                break
            try:
                result = self.service.build_context(**job)
                score = self.service.scoring.evaluate(result.context)
                if result.data_gaps:
                    score = score.model_copy(update={
                        "data_gaps": list(dict.fromkeys([
                            *score.data_gaps, *result.data_gaps,
                        ])),
                    })
                self.score_ready.emit(score, result.closed_stock_bar)
            except Exception as exc:  # noqa: BLE001
                self.failed.emit(job["symbol"], str(exc))
        self.finished.emit()


class _MarketSentimentWorker(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, service, store, hs300_breadth_pct: float | None, captured_at) -> None:
        super().__init__()
        self.service = service
        self.store = store
        self.hs300_breadth_pct = hs300_breadth_pct
        self.captured_at = captured_at

    def run(self) -> None:
        try:
            self.finished.emit(self.service.capture_for_store(
                store=self.store,
                hs300_breadth_pct=self.hs300_breadth_pct,
                captured_at=self.captured_at,
            ))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class _ScoreLabel(QLabel):
    clicked = pyqtSignal(str)

    def __init__(self, key: str, text: str) -> None:
        super().__init__(text)
        self.key = key

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.key)
        super().mousePressEvent(event)


class _UniverseWorker(QObject):
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, service) -> None:
        super().__init__()
        self.service = service

    def run(self) -> None:
        try:
            snapshot = self.service.generate(
                progress=lambda current, total, symbol: self.progress.emit(
                    current, total, symbol
                )
            )
            self.finished.emit(snapshot)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class _DailyCandidateWorker(QObject):
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, scanner, pool_snapshot: dict) -> None:
        super().__init__()
        self.scanner = scanner
        self.pool_snapshot = pool_snapshot

    def run(self) -> None:
        try:
            result = self.scanner.scan(
                self.pool_snapshot,
                progress=lambda current, total, symbol: self.progress.emit(
                    current, total, symbol
                ),
            )
            self.finished.emit(result)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class _LifecycleDailySyncWorker(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, service) -> None:
        super().__init__()
        self.service = service

    def run(self) -> None:
        try:
            self.finished.emit(self.service.sync_open_daily())
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class _OosBacktestWorker(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, engine, bundle_path: Path) -> None:
        super().__init__()
        self.engine = engine
        self.bundle_path = bundle_path

    def run(self) -> None:
        try:
            self.finished.emit(self.engine.run(self.bundle_path))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class TradeLedgerWindow(QWidget):
    """Embedded quant-management page kept under its legacy public name."""

    return_to_analysis_requested = pyqtSignal()

    def __init__(self, ctx, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.store = ctx.trade_store
        self._quant_runtime = getattr(ctx, "quant_runtime", None)
        self._broker_snapshot = (
            getattr(self._quant_runtime, "broker_snapshot", None)
            if self._quant_runtime is not None else None
        )
        self._stock_profile_thread: QThread | None = None
        self._hotspot_thread: QThread | None = None
        self._topdown_thread: QThread | None = None
        self._sentiment_thread: QThread | None = None
        self._universe_thread: QThread | None = None
        self._daily_candidate_thread: QThread | None = None
        self._lifecycle_sync_thread: QThread | None = None
        self._oos_backtest_thread: QThread | None = None
        self._oos_backtest_worker: _OosBacktestWorker | None = None
        self._validated_oos_bundle_path = ""
        self._last_lifecycle_sync: dict = {}
        self._last_topdown_slot = ""
        self._reconciliation_timer = QTimer(self)
        self._reconciliation_timer.setInterval(2000)
        self._reconciliation_timer.timeout.connect(self._poll_reconciliation)
        self._reconciliation_order = None
        self._reconciliation_attempts = 0
        self._reconciliation_matched = False
        self.setObjectName("quantTradingManagementPage")
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(8)
        root_layout.addWidget(self._build_dashboard_header())
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setElideMode(Qt.TextElideMode.ElideRight)
        self.tabs.tabBar().setUsesScrollButtons(True)
        self.tabs.setAccessibleName("量化交易管理导航")
        root_layout.addWidget(self.tabs, 1)
        self.today_page = self._build_today_page()
        self.universe = self._build_universe_page()
        self.stock_page = self._build_stock_page()
        self.broker_page = self._build_broker_page()
        self.pending = self._table_tab([
            "计划ID", "策略/来源", "股票", "池版本", "方向", "触发价", "最高价",
            "止损", "总分", "连续确认", "风控/计划状态", "有效期",
        ])
        self.open_positions = self._table_tab([
            "计划ID", "品种", "成交价", "数量", "当前保护止损", "当前风险", "浮动R", "退出状态",
        ])
        self.closed = self._table_tab([
            "计划ID", "数据集", "品种", "结果", "毛收益", "净收益", "R", "MFE(R)", "MAE(R)", "持有K线",
        ])
        self.monthly_page = self._build_monthly_page()
        self.validation_page = self._build_validation_page()
        self.audit_settings_page = self._build_audit_settings_page()
        self.tabs.addTab(self.today_page, "今日工作台")
        self.tabs.addTab(self.universe[0], "交易股票池")
        self.tabs.addTab(self.stock_page, "股票详情")
        self.tabs.addTab(self.pending[0], "交易计划")
        self.tabs.addTab(self.open_positions[0], "持仓与退出")
        self.tabs.addTab(self.monthly_page, "月度表现")
        self.tabs.addTab(self.broker_page, "同花顺与账户")
        self.tabs.addTab(self.validation_page, "策略验证")
        self.tabs.addTab(self.audit_settings_page, "审计与设置")
        self.tabs.currentChanged.connect(self._on_navigation_changed)
        self._page_shortcuts: list[QShortcut] = []
        for index in range(self.tabs.count()):
            shortcut = QShortcut(QKeySequence(f"Ctrl+{index + 1}"), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(
                lambda tab_index=index: self.tabs.setCurrentIndex(tab_index)
            )
            self._page_shortcuts.append(shortcut)
        self._add_actions()
        for _, table in (self.pending, self.open_positions, self.closed):
            table.doubleClicked.connect(self._show_selected_audit)
        self.universe[1].doubleClicked.connect(
            lambda: self._open_stock_from_table(self.universe[1], 1)
        )
        self.candidate_table.doubleClicked.connect(
            lambda: self._open_stock_from_table(self.candidate_table, 0)
        )
        self.action_table.doubleClicked.connect(
            lambda: self._open_stock_from_table(self.action_table, 1)
        )
        if self._quant_runtime is not None:
            self._quant_runtime.updated.connect(self.refresh)
            self._quant_runtime.broker_snapshot_changed.connect(
                self._apply_broker_snapshot
            )
            self._quant_runtime.status_changed.connect(self._runtime_status_changed)
        else:
            # Lightweight/test contexts may intentionally omit the application
            # runtime.  Preserve one read-only snapshot for those views without
            # recreating any long-lived window-owned collection loop.
            QTimer.singleShot(0, self._sync_broker)
        self.refresh()

    def _build_dashboard_header(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("quantDashboardHeader")
        frame.setStyleSheet(
            "QFrame#quantDashboardHeader {background:#111820; border:1px solid #27313b; "
            "border-radius:8px;} QLabel {background:transparent;}"
        )
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(7)
        status_row = QHBoxLayout()
        status_row.setSpacing(10)
        back_button = QPushButton("← 返回行情分析")
        back_button.setObjectName("returnToAnalysisButton")
        back_button.setToolTip("在当前PA Agent窗口中返回行情与AI分析（Esc）")
        back_button.setMaximumWidth(172)
        back_button.clicked.connect(self.return_to_analysis_requested.emit)
        status_row.addWidget(back_button)
        title = QLabel("量化交易工作台")
        title.setObjectName("quantWorkbenchTitle")
        title.setStyleSheet("font-size:16px; font-weight:700; color:#f0f6fc;")
        status_row.addWidget(title)
        self.activity_status_label = QLabel("数据监控运行中")
        self.activity_status_label.setObjectName("quantActivityStatus")
        self.activity_status_label.setStyleSheet(
            "color:#8b949e; padding:4px 8px; background:#18222c; border-radius:4px;"
        )
        self.activity_status_label.setMaximumWidth(300)
        self.activity_status_label.setToolTip("常驻量化采集不会因页面切换而停止")
        status_row.addWidget(self.activity_status_label)
        self.header_refresh_button = QPushButton("刷新视图")
        self.header_refresh_button.setObjectName("quantRefreshButton")
        self.header_refresh_button.setToolTip("刷新当前数据库、评分和账户视图")
        self.header_refresh_button.setMaximumWidth(96)
        self.header_refresh_button.clicked.connect(self.refresh)
        status_row.addWidget(self.header_refresh_button)
        layout.addLayout(status_row)

        system_row = QHBoxLayout()
        self.system_status_line = QLabel("同花顺 ● 检测中 | 策略 CANDIDATE | 股票池尚未加载")
        self.system_status_line.setWordWrap(True)
        self.system_status_line.setStyleSheet("font-weight:600; color:#e6edf3;")
        system_row.addWidget(self.system_status_line, 1)
        layout.addLayout(system_row)

        score_row = QHBoxLayout()
        score_row.setSpacing(6)
        self.score_labels: dict[str, QLabel] = {}
        for key, text, stretch in (
            ("index", "指数 —/40", 4),
            ("sentiment", "情绪 —/30", 3),
            ("theme", "题材 —/20", 2),
            ("stock", "个股 —/10", 1),
        ):
            label = _ScoreLabel(key, text)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setMinimumHeight(34)
            label.setStyleSheet(
                "background:#18222c; border-left:3px solid #38bdf8; border-radius:4px; "
                "padding:5px; font-family:'Cascadia Mono','Consolas'; font-weight:600;"
            )
            label.setToolTip("点击展开评分依据、数据时间和未得分原因")
            label.setCursor(Qt.CursorShape.PointingHandCursor)
            label.clicked.connect(self._toggle_score_details)
            score_row.addWidget(label, stretch)
            self.score_labels[key] = label
        self.total_score_label = QLabel("综合 —/100 | 数据未就绪")
        self.total_score_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.total_score_label.setStyleSheet(
            "background:#17252a; color:#5eead4; border:1px solid #24534d; "
            "border-radius:4px; padding:5px 10px; font-weight:700;"
        )
        score_row.addWidget(self.total_score_label, 3)
        layout.addLayout(score_row)
        self.score_detail_panel = QTextEdit()
        self.score_detail_panel.setReadOnly(True)
        self.score_detail_panel.setMaximumHeight(180)
        self.score_detail_panel.setVisible(False)
        layout.addWidget(self.score_detail_panel)

        self.risk_status_line = QLabel(
            "本月收益 — | 高点回撤 — | 开放风险 — | 持仓 —/3 | 待处理 —"
        )
        self.risk_status_line.setStyleSheet(
            "color:#8b949e; font-family:'Cascadia Mono','Consolas';"
        )
        layout.addWidget(self.risk_status_line)
        return frame

    def _toggle_score_details(self, component: str) -> None:
        score_record = self.store.latest_topdown_score() if self.store.available else None
        score = (score_record or {}).get("snapshot") or {}
        if (
            self.score_detail_panel.isVisible()
            and self.score_detail_panel.property("component") == component
        ):
            self.score_detail_panel.setVisible(False)
            return
        self.score_detail_panel.setPlainText(json.dumps({
            "评分层": _score_name(component),
            "得分依据": (score.get("component_details") or {}).get(component)
            or "暂无完整评分输入",
            "数据时间": score.get("source_timestamps") or {},
            "未得分/数据缺口": score.get("data_gaps") or [],
            "硬阻断": score.get("hard_blocks") or [],
            "输入哈希": score.get("input_hash") or "—",
        }, ensure_ascii=False, indent=2))
        self.score_detail_panel.setProperty("component", component)
        self.score_detail_panel.setVisible(True)

    def _build_universe_page(self) -> tuple[QWidget, QTableWidget]:
        page = QWidget()
        layout = QVBoxLayout(page)
        controls = QHBoxLayout()
        generate = QPushButton("生成/刷新新云算力股票池")
        generate.clicked.connect(self._generate_current_universe)
        self.universe_generate_button = generate
        self.universe_status = QLabel(
            "尚未生成新云算力股票池；固定 11 只成员，以行情与制度数据校验交易资格。"
        )
        self.universe_status.setWordWrap(True)
        controls.addWidget(generate)
        controls.addWidget(self.universe_status, 1)
        layout.addLayout(controls)
        tabs = QTabWidget()
        current_page, current_table = self._table_tab([
            "排名", "代码", "名称", "行业/题材", "20日均成交额", "最新价", "涨跌幅",
            "池身份", "日线预选",
            "指数", "情绪", "题材", "个股", "总分", "热点/未通过原因", "交易状态",
        ])
        candidate_page, self.candidate_table = self._table_tab([
            "代码", "名称", "日线候选", "总分", "连续确认", "状态", "下一步",
        ])
        self.candidate_empty_label = QLabel(
            "今日没有股票通过日线预选；这不是股票池无数据，基础池仍会保留并展示。"
        )
        self.candidate_empty_label.setWordWrap(True)
        self.candidate_empty_label.setStyleSheet(
            "padding:10px; color:#8b949e; background:#111820; "
            "border:1px solid #27313b; border-radius:4px;"
        )
        candidate_page.layout().insertWidget(0, self.candidate_empty_label)
        excluded_page, self.excluded_table = self._table_tab([
            "代码", "排除原因", "股票池版本", "生效日期",
        ])
        manual_page = QWidget()
        manual_layout = QVBoxLayout(manual_page)
        manual_hint = QLabel(
            "池外查询不会修改基础股票池；通过全部检查后仍需当前计划单独批准，"
            "并固定使用半风险、最多同时1只。"
        )
        manual_hint.setWordWrap(True)
        manual_row = QHBoxLayout()
        self.manual_universe_symbol = QLineEdit()
        self.manual_universe_symbol.setPlaceholderText("输入股票代码或名称")
        manual_button = QPushButton("打开专业评估")
        manual_button.clicked.connect(self._open_manual_stock_assessment)
        manual_row.addWidget(self.manual_universe_symbol, 1)
        manual_row.addWidget(manual_button)
        manual_layout.addWidget(manual_hint)
        manual_layout.addLayout(manual_row)
        manual_layout.addStretch(1)
        history_page, self.universe_history_table = self._table_tab([
            "版本", "生效日期", "成员数", "排除数", "数据完整", "来源更新时间",
        ])
        tabs.addTab(current_page, "当前基础池")
        tabs.addTab(candidate_page, "今日候选")
        tabs.addTab(excluded_page, "排除记录")
        tabs.addTab(manual_page, "手工查询与专业评估")
        tabs.addTab(history_page, "历史股票池")
        self.universe_tabs = tabs
        layout.addWidget(tabs)
        return page, current_table

    def _ensure_current_universe(self) -> None:
        if self._quant_runtime is not None:
            self._quant_runtime.ensure_current_universe(force=True)
            return
        if not self.store.available or getattr(self.ctx, "universe_service", None) is None:
            return
        service = getattr(self.ctx, "universe_service", None)
        current_version = (
            service.current_version(datetime.now().astimezone())
            if service is not None and hasattr(service, "current_version")
            else f"hs300-{datetime.now().astimezone():%Y-%m}"
        )
        if any(
            item.get("version") == current_version
            for item in self.store.list_universe_snapshots(limit=24)
        ):
            return
        self._generate_current_universe()

    def _generate_current_universe(self) -> None:
        service = getattr(self.ctx, "universe_service", None)
        if service is None:
            self.universe_status.setText("股票池生成服务不可用，实盘授权保持关闭。")
            return
        if self._universe_thread is not None and self._universe_thread.isRunning():
            return
        self.universe_generate_button.setEnabled(False)
        self.universe_status.setText("正在校验新云算力 11 股的行情、上市日期与交易制度…")
        thread = QThread(self)
        worker = _UniverseWorker(service)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._universe_progress)
        worker.finished.connect(self._universe_generated)
        worker.failed.connect(self._universe_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._universe_thread = thread
        self._universe_worker = worker
        thread.start()

    def _universe_progress(self, current: int, total: int, symbol: str) -> None:
        self.universe_status.setText(
            f"正在校验固定池行情与上市日期 {current}/{total} | 当前 {symbol}"
        )

    def _universe_generated(self, snapshot) -> None:
        self.store.upsert_universe_snapshot(
            snapshot,
            source_updated_at=(
                snapshot.source_as_of.isoformat() if snapshot.source_as_of else ""
            ),
            data_complete=snapshot.data_complete,
        )
        self.universe_generate_button.setEnabled(True)
        if snapshot.data_complete:
            self.universe_status.setText(
                f"{snapshot.version} 已生成：{len(snapshot.symbols)}只 | "
                f"用户固定成员日期 {snapshot.source_as_of} | 输入 {snapshot.input_member_count}只"
            )
        else:
            self.universe_status.setText(
                f"{snapshot.version} 数据不完整，禁止授权："
                + ", ".join(snapshot.completeness_reasons)
            )
        self.refresh()
        QTimer.singleShot(0, self._ensure_daily_candidates)

    def _universe_failed(self, error: str) -> None:
        self.universe_generate_button.setEnabled(True)
        self.universe_status.setText(f"股票池生成失败，禁止授权：{error}")
        self.ctx.logger.warning("当前固定股票池生成失败: %s", error)

    def _ensure_daily_candidates(self) -> None:
        if self._quant_runtime is not None:
            self._quant_runtime.ensure_daily_candidates()
            return
        scanner = getattr(self.ctx, "daily_candidate_scanner", None)
        if scanner is None or not self.store.available:
            return
        if (
            self._daily_candidate_thread is not None
            and self._daily_candidate_thread.isRunning()
        ):
            return
        universes = self.store.list_universe_snapshots(limit=1)
        if not universes or not universes[0].get("data_complete"):
            return
        pool = universes[0]["snapshot"]
        today = datetime.now().astimezone()
        expected_day = today.date()
        if today.hour < 15:
            expected_day = expected_day.fromordinal(expected_day.toordinal() - 1)
        while expected_day.weekday() >= 5:
            expected_day = expected_day.fromordinal(expected_day.toordinal() - 1)
        already_scanned = any(
            item.get("pool_version") == pool.get("version")
            and str(item.get("signal_time") or "")[:10] == expected_day.isoformat()
            for item in self.store.list_quant_signals(
                strategy_id=self.ctx.settings.strategy.strategy_id,
                limit=max(100, len(pool.get("symbols") or []) * 4),
            )
        )
        if already_scanned:
            return
        self.universe_status.setText(
            f"{pool.get('version', '')} 已加载；正在扫描收盘后日线候选 0/"
            f"{len(pool.get('symbols') or [])}"
        )
        thread = QThread(self)
        worker = _DailyCandidateWorker(scanner, pool)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._daily_candidate_progress)
        worker.finished.connect(self._daily_candidates_finished)
        worker.failed.connect(self._daily_candidates_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._daily_candidate_thread = thread
        self._daily_candidate_worker = worker
        thread.start()

    def _daily_candidate_progress(self, current: int, total: int, symbol: str) -> None:
        self.universe_status.setText(
            f"正在扫描收盘后日线候选 {current}/{total} | 当前 {symbol}"
        )

    def _daily_candidates_finished(self, result) -> None:
        for decision in result.decisions:
            self.store.add_quant_signal(decision)
        if result.data_complete:
            self.universe_status.setText(
                f"{result.pool_version} | 日线扫描 {len(result.decisions)}只 | "
                f"候选 {len(result.allowed)}只 | 市场宽度 "
                f"{result.market_breadth_pct:.1f}% | 信号日 {result.signal_date}"
            )
        else:
            self.universe_status.setText(
                "日线候选数据不完整，禁止授权：" + ", ".join(result.data_gaps)
            )
        self.refresh()
        QTimer.singleShot(0, self._refresh_hotspots)
        QTimer.singleShot(0, self._refresh_topdown_scores)

    def _daily_candidates_failed(self, error: str) -> None:
        self.universe_status.setText(f"日线候选扫描失败，禁止授权：{error}")
        self.ctx.logger.warning("收盘后日线候选扫描失败: %s", error)

    def _open_manual_stock_assessment(self) -> None:
        value = self.manual_universe_symbol.text().strip()
        if not value:
            return
        self.stock_symbol.setText(value)
        self.tabs.setCurrentIndex(2)
        self._load_stock_detail()

    def _open_stock_from_table(self, table: QTableWidget, symbol_column: int) -> None:
        """Open the selected stock in-place without introducing another window."""
        row = table.currentRow()
        item = table.item(row, symbol_column) if row >= 0 else None
        symbol = (item.text().strip().split() or [""])[0] if item else ""
        if not symbol:
            return
        self.stock_symbol.setText(symbol)
        self.tabs.setCurrentIndex(2)
        self._load_stock_detail()

    def _on_navigation_changed(self, index: int) -> None:
        if index < 0:
            return
        label = self.tabs.tabText(index)
        self.tabs.setToolTip(
            f"当前位置：{label} · Ctrl+{index + 1} 可直接打开此页"
        )

    def _build_today_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        title = QLabel("今日行动")
        title.setStyleSheet("font-size:18px; font-weight:700;")
        hint = QLabel("按风险优先级排列；只有通过日线预选、四层评分和组合风控的计划才可预填。")
        hint.setObjectName("mutedLabel")
        self.action_table = QTableWidget(0, 7)
        self.action_table.setHorizontalHeaderLabels([
            "优先级", "股票", "事项", "四层评分", "状态", "下一步", "阻断原因",
        ])
        self.action_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.action_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.action_table.horizontalHeader().setStretchLastSection(True)
        self.today_empty = QLabel(
            "当前没有同时通过日线形态、四层评分和风险条件的机会，系统保持空仓。"
        )
        self.today_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.today_empty.setStyleSheet(
            "padding:30px; color:#8b949e; border:1px dashed #30363d; border-radius:6px;"
        )
        layout.addWidget(title)
        layout.addWidget(hint)
        layout.addWidget(self.action_table, 1)
        layout.addWidget(self.today_empty)
        return page

    def _build_stock_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        row = QHBoxLayout()
        self.stock_symbol = QLineEdit()
        self.stock_symbol.setPlaceholderText("输入A股代码或名称，例如 600519 / 贵州茅台")
        load = QPushButton("加载股票信息")
        load.clicked.connect(self._load_stock_detail)
        row.addWidget(self.stock_symbol, 1)
        row.addWidget(load)
        layout.addLayout(row)
        self.stock_tabs = QTabWidget()
        self.stock_detail_texts: dict[str, QTextEdit] = {}
        for key, label in (
            ("quant", "行情与量化"),
            ("hotspot", "热点与题材"),
            ("plan", "交易计划"),
            ("company", "公司资料"),
            ("finance", "财务估值"),
            ("risk", "公告新闻与风险"),
        ):
            text = QTextEdit()
            text.setReadOnly(True)
            text.setPlainText("尚未加载。所有数据将显示来源和更新时间；AI摘要不参与评分或交易授权。")
            self.stock_tabs.addTab(text, label)
            self.stock_detail_texts[key] = text
        layout.addWidget(self.stock_tabs, 1)
        return page

    def _build_monthly_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        note = QLabel(
            "月度目标：扣除费用和出入金影响后保持正收益。风险限制用于控制损失，不保证收益结果。"
        )
        note.setWordWrap(True)
        note.setStyleSheet("padding:8px; color:#fbbf24; background:#28230f; border-radius:5px;")
        self.monthly_summary = QTextEdit()
        self.monthly_summary.setReadOnly(True)
        layout.addWidget(note)
        layout.addWidget(self.monthly_summary, 1)
        return page

    def _build_validation_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.validation_summary = QTextEdit()
        self.validation_summary.setReadOnly(True)
        self.small_live_button = QPushButton("批准进入0.25%风险小资金实盘")
        self.small_live_button.setEnabled(False)
        self.small_live_button.setToolTip(
            "必须先满足12周、80笔、全部完整自然月为正和PF≥1.15，"
            "并完成同花顺账户绑定；批准后仍不会自动点击委托。"
        )
        self.small_live_button.clicked.connect(self._approve_small_live)
        self.import_oos_button = QPushButton("导入并校验样本外数据包")
        self.import_oos_button.clicked.connect(self._import_oos_bundle)
        self.run_oos_button = QPushButton("运行日线+15分钟组合样本外回测")
        self.run_oos_button.setEnabled(False)
        self.run_oos_button.setToolTip(
            "必须先导入并通过 pa_oos_bundle_v1 校验; 回测在后台运行, "
            "只有完整证据达到全部门槛才会进入 SHADOW。"
        )
        self.run_oos_button.clicked.connect(self._run_oos_backtest)
        layout.addWidget(QLabel("策略晋级路线：CANDIDATE → SHADOW → ACTIVE → REDUCED → PAUSED → RETIRED"))
        layout.addWidget(self.import_oos_button)
        layout.addWidget(self.run_oos_button)
        layout.addWidget(self.small_live_button)
        layout.addWidget(self.validation_summary, 1)
        return page

    def _build_audit_settings_page(self) -> QWidget:
        tabs = QTabWidget()
        tabs.addTab(self._build_audit(), "计划审计")
        tabs.addTab(self.closed[0], "已结束交易")
        tabs.addTab(self._build_statistics(), "策略统计")
        tabs.addTab(self._build_config(), "风险与实盘设置")
        return tabs

    def _table_tab(self, headers: list[str]) -> tuple[QWidget, QTableWidget]:
        page = QWidget()
        layout = QVBoxLayout(page)
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(table)
        return page, table

    def _build_broker_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        banner = QLabel(
            "安全边界：系统只读取账户数据并预填委托字段；不会输入密码、验证码，"
            "不会点击买入、卖出、撤单或最终确认。"
        )
        banner.setWordWrap(True)
        banner.setStyleSheet("padding: 8px; background: #332b00; color: #ffd866;")
        layout.addWidget(banner)
        binding = self.ctx.settings.ths
        form = QFormLayout()
        self.broker_name = QLineEdit(binding.broker_name)
        self.masked_account = QLineEdit(binding.masked_account)
        self.ths_install_path = QLineEdit(binding.install_path)
        self.ths_install_path.setPlaceholderText("同花顺远航版安装目录")
        self.masked_account.setPlaceholderText("仅填写脱敏账号，例如 ****1234")
        self.prefill_enabled = QCheckBox("完成校准后允许安全预填（仍需人工确认）")
        self.prefill_enabled.setChecked(binding.allow_prefill and not binding.read_only)
        strategy_state = self.store.current_strategy_state(TOPDOWN_STRATEGY_ID)
        prefill_state_allowed = strategy_state in {
            StrategyState.ACTIVE.value,
            StrategyState.REDUCED.value,
        }
        self.prefill_enabled.setEnabled(prefill_state_allowed)
        if not prefill_state_allowed:
            self.prefill_enabled.setChecked(False)
            self.prefill_enabled.setToolTip(
                "策略完成样本外和影子验证并由用户批准进入ACTIVE前，禁止预填。"
            )
        form.addRow("券商名称", self.broker_name)
        form.addRow("脱敏资金账号", self.masked_account)
        install_row = QWidget()
        install_layout = QHBoxLayout(install_row)
        install_layout.setContentsMargins(0, 0, 0, 0)
        choose_install = QPushButton("选择目录")
        choose_install.clicked.connect(self._choose_ths_install_path)
        install_layout.addWidget(self.ths_install_path, 1)
        install_layout.addWidget(choose_install)
        form.addRow("安装目录", install_row)
        form.addRow("预填权限", self.prefill_enabled)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        detect = QPushButton("检测同花顺")
        detect.clicked.connect(self._detect_broker)
        launch = QPushButton("启动同花顺（登录由用户完成）")
        launch.clicked.connect(self._launch_broker_clients)
        bind = QPushButton("确认绑定此账户")
        bind.clicked.connect(self._confirm_binding)
        sync = QPushButton("立即只读同步")
        sync.clicked.connect(self._sync_broker)
        cash_flow_sync = QPushButton("读取同花顺当前资金流水页")
        cash_flow_sync.setToolTip(
            "请先由用户在同花顺手工打开并查询本月资金流水；"
            "PA Agent只复制当前结果，不点击查询、转账或确认。"
        )
        cash_flow_sync.clicked.connect(self._sync_current_cash_flow_page)
        manual_link = QPushButton("人工关联所选同花顺委托")
        manual_link.clicked.connect(self._manually_link_broker_order)
        buttons.addWidget(detect)
        buttons.addWidget(launch)
        buttons.addWidget(bind)
        buttons.addWidget(sync)
        buttons.addWidget(cash_flow_sync)
        buttons.addWidget(manual_link)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.broker_status = QLabel("尚未检测")
        self.broker_status.setWordWrap(True)
        self.broker_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.broker_status)

        self.broker_funds = QLabel("总资产 — | 可用资金 — | 持仓市值 — | 当日盈亏 —")
        self.broker_funds.setStyleSheet(
            "font-family:'Cascadia Mono','Consolas'; font-weight:600; padding:8px; "
            "background:#18222c; border-radius:4px;"
        )
        layout.addWidget(self.broker_funds)
        account_tabs = QTabWidget()
        self.broker_positions = self._broker_table(
            ["代码", "名称", "持仓数量", "可卖数量", "成本价", "最新价", "市值", "行业"]
        )
        self.broker_orders = self._broker_table(
            ["委托号", "代码", "方向", "委托价", "数量", "成交数量", "状态", "委托时间"]
        )
        self.broker_fills = self._broker_table(
            ["成交号", "委托号", "代码", "方向", "成交价", "数量", "费用", "成交时间"]
        )
        self.broker_cash_flows = self._broker_table(
            ["流水号", "方向", "金额", "发生时间", "状态", "说明", "来源"]
        )
        account_tabs.addTab(self.broker_positions, "持仓")
        account_tabs.addTab(self.broker_orders, "当日委托")
        account_tabs.addTab(self.broker_fills, "当日成交")
        account_tabs.addTab(self.broker_cash_flows, "资金流水")
        self.broker_summary = QTextEdit()
        self.broker_summary.setReadOnly(True)
        account_tabs.addTab(self.broker_summary, "开发诊断")
        layout.addWidget(account_tabs, 1)
        return page

    @staticmethod
    def _broker_table(headers: list[str]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setStretchLastSection(True)
        return table

    def _add_actions(self) -> None:
        layout = self.pending[0].layout()
        row = QHBoxLayout()
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh)
        audit = QPushButton("查看所选审计")
        audit.clicked.connect(self._show_selected_audit)
        prefill = QPushButton("所选量化计划预填到同花顺")
        prefill.clicked.connect(self._prefill_selected)
        row.addWidget(refresh)
        row.addWidget(audit)
        row.addWidget(prefill)
        row.addStretch(1)
        layout.insertLayout(0, row)

        open_layout = self.open_positions[0].layout()
        exit_button = QPushButton("查看所选持仓退出与成交")
        exit_button.clicked.connect(self._show_selected_audit)
        open_layout.insertWidget(0, exit_button)

        closed_layout = self.closed[0].layout()
        exports = QHBoxLayout()
        for dataset, label in (("actual", "导出实际交易 CSV"), ("shadow", "导出影子交易 CSV")):
            button = QPushButton(label)
            button.clicked.connect(lambda _=False, d=dataset: self._export(d))
            exports.addWidget(button)
        exports.addStretch(1)
        closed_layout.insertLayout(0, exports)

    def _build_statistics(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        note = QLabel("actual 与 shadow 严格分开统计；旧AI研究计划不计入量化策略绩效。")
        filters = QHBoxLayout()
        self.stats_filters: dict[str, QLineEdit] = {}
        for key, hint in (
            ("asset_class", "资产类别"), ("symbol", "品种"), ("timeframe", "周期"),
            ("market_state", "市场状态"), ("order_type", "订单类型"),
            ("strategy_version", "策略版本"),
        ):
            edit = QLineEdit()
            edit.setPlaceholderText(hint)
            self.stats_filters[key] = edit
            filters.addWidget(edit)
        apply_button = QPushButton("应用筛选")
        apply_button.clicked.connect(self.refresh)
        filters.addWidget(apply_button)
        self.stats_text = QTextEdit()
        self.stats_text.setReadOnly(True)
        layout.addWidget(note)
        layout.addLayout(filters)
        layout.addWidget(self.stats_text)
        return page

    def _build_audit(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.audit_text = QTextEdit()
        self.audit_text.setReadOnly(True)
        from pa_agent.gui.chart_widget import ChartWidget

        self.audit_chart = ChartWidget()
        self.audit_chart.setMinimumHeight(320)
        layout.addWidget(self.audit_text, 1)
        layout.addWidget(self.audit_chart, 2)
        return page

    def _build_config(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QFormLayout()
        risk = self.ctx.settings.risk
        portfolio = self.ctx.settings.portfolio_risk
        self.equity = _pct_spin(risk.account_equity or 0, maximum=1_000_000_000)
        self.cash = _pct_spin(risk.available_cash or 0, maximum=1_000_000_000)
        self.per_trade = _pct_spin(risk.per_trade_risk_pct)
        self.max_open = _pct_spin(risk.max_open_risk_pct)
        self.initial_per_trade = _pct_spin(portfolio.initial_per_trade_risk_pct)
        self.initial_max_open = _pct_spin(portfolio.initial_max_open_risk_pct)
        self.daily = _pct_spin(risk.daily_loss_warning_pct)
        self.weekly = _pct_spin(risk.weekly_loss_warning_pct)
        self.live_enabled = QCheckBox("显式允许实盘授权（仍受策略状态和同花顺完整快照约束）")
        self.live_enabled.setChecked(portfolio.live_trading_enabled)
        strategy_state = self.store.current_strategy_state(TOPDOWN_STRATEGY_ID)
        live_state_allowed = strategy_state in {
            StrategyState.ACTIVE.value,
            StrategyState.REDUCED.value,
        }
        self.live_enabled.setEnabled(live_state_allowed)
        if not live_state_allowed:
            self.live_enabled.setChecked(False)
            self.live_enabled.setToolTip(
                "策略完成样本外和影子验证并由用户批准进入ACTIVE前，实盘入口关闭。"
            )
        form.addRow("账户权益（同花顺完整同步后以同花顺为准）", self.equity)
        form.addRow("可用资金", self.cash)
        form.addRow("升级后单笔风险 %", self.per_trade)
        form.addRow("升级后最大开放风险 %", self.max_open)
        form.addRow("首批单笔风险 %", self.initial_per_trade)
        form.addRow("首批最大开放风险 %", self.initial_max_open)
        form.addRow("单日亏损警戒 %", self.daily)
        form.addRow("单周亏损警戒 %", self.weekly)
        form.addRow("实盘总开关", self.live_enabled)
        layout.addLayout(form)
        save = QPushButton("保存账户与风险配置")
        save.clicked.connect(self._save_risk)
        layout.addWidget(save)
        profile_row = QHBoxLayout()
        self.profile_symbol = QLineEdit()
        self.profile_symbol.setPlaceholderText("输入品种代码，例如 600519")
        edit_profile = QPushButton("编辑品种制度与真实费用")
        edit_profile.clicked.connect(self._edit_profile)
        profile_row.addWidget(self.profile_symbol)
        profile_row.addWidget(edit_profile)
        layout.addLayout(profile_row)
        self.health_label = QLabel()
        self.health_label.setWordWrap(True)
        layout.addWidget(self.health_label)
        layout.addStretch(1)
        return page

    def _detect_broker(self) -> None:
        state = self.ctx.broker_adapter.connect(self.ctx.settings.ths)
        self._show_connection(state)

    def _choose_ths_install_path(self) -> None:
        initial = self.ths_install_path.text().strip() or str(Path.home())
        selected = QFileDialog.getExistingDirectory(self, "选择同花顺远航版安装目录", initial)
        if selected:
            self.ths_install_path.setText(selected)

    def _launch_broker_clients(self) -> None:
        try:
            state = self.ctx.broker_adapter.launch_clients(
                install_path=self.ths_install_path.text().strip()
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "同花顺启动失败", str(exc))
            return
        self._show_connection(state)
        QMessageBox.information(
            self,
            "同花顺已启动",
            "客户端已启动。请由你本人完成登录；PA Agent未输入账号、密码或验证码，"
            "也未点击任何查询或交易按钮。登录后请点“检测同花顺”。",
        )

    def _confirm_binding(self) -> None:
        broker_name = self.broker_name.text().strip()
        masked = self.masked_account.text().strip()
        if not broker_name or not masked or "*" not in masked:
            QMessageBox.warning(self, "不能绑定", "请填写券商名称和脱敏资金账号（例如 ****1234）。")
            return
        try:
            binding = self.ctx.broker_adapter.confirmed_binding(
                broker_name=broker_name, masked_account=masked
            )
            strategy_state = self.store.current_strategy_state(TOPDOWN_STRATEGY_ID)
            allow_prefill = (
                self.prefill_enabled.isEnabled()
                and self.prefill_enabled.isChecked()
                and strategy_state
                in {StrategyState.ACTIVE.value, StrategyState.REDUCED.value}
            )
            binding = binding.model_copy(update={
                "read_only": not allow_prefill,
                "allow_prefill": allow_prefill,
            })
            self.ctx.settings.ths = binding
            save_settings(self.ctx.settings, SETTINGS_JSON_PATH)
            self.ctx.broker_adapter = ThsBrokerAdapter(binding)
            if getattr(self.ctx, "broker_trade_lifecycle", None) is not None:
                self.ctx.broker_trade_lifecycle.broker = self.ctx.broker_adapter
            self._sync_broker()
            QMessageBox.information(
                self,
                "绑定已保存",
                "只保存安装路径、客户端版本、券商和脱敏账号指纹，不保存密码或令牌。",
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "绑定失败", str(exc))

    def _sync_broker(self) -> None:
        if self._quant_runtime is not None:
            self._quant_runtime.sync_broker()
            return
        adapter = getattr(self.ctx, "broker_adapter", None)
        if adapter is None:
            return
        snapshot = adapter.snapshot()
        if self.store.available:
            self.store.add_broker_snapshot(snapshot)
            if snapshot.complete and snapshot.total_equity is not None:
                self.store.record_broker_financial_snapshot(snapshot)
            self._refresh_linked_broker_orders(snapshot)
            self._record_external_manual_fills(snapshot)
        self._apply_broker_snapshot(snapshot)

    def _apply_broker_snapshot(self, snapshot) -> None:
        self._broker_snapshot = snapshot
        self._refresh_monthly()
        self._show_connection(snapshot.connection)
        self.broker_funds.setText(
            f"总资产 {_money(snapshot.total_equity)} | 可用资金 {_money(snapshot.available_cash)} | "
            f"持仓市值 {_money(snapshot.position_value)} | 当日盈亏 {_signed_money(snapshot.daily_pnl)} | "
            f"快照 {'完整' if snapshot.complete else '不完整'}"
        )
        self._fill(self.broker_positions, [[
            item.symbol, item.name, item.quantity, item.sellable_quantity,
            item.cost_price, item.last_price, item.market_value, item.industry,
        ] for item in snapshot.positions])
        self._fill(self.broker_orders, [[
            item.broker_order_id, item.symbol, item.direction, item.price, item.quantity,
            item.filled_quantity, item.status, item.submitted_at,
        ] for item in snapshot.orders])
        self._fill(self.broker_fills, [[
            item.broker_fill_id, item.broker_order_id, item.symbol, item.direction,
            item.price, item.quantity, item.fees, item.filled_at,
        ] for item in snapshot.fills])
        self._fill(self.broker_cash_flows, [[
            item.broker_flow_id, "入金" if item.direction == "deposit" else "出金",
            item.amount, item.occurred_at, item.status, item.description, item.source,
        ] for item in snapshot.cash_flows])
        self.broker_summary.setPlainText(json.dumps({
            "同步时间": snapshot.captured_at,
            "快照完整": snapshot.complete,
            "总资产": snapshot.total_equity,
            "可用资金": snapshot.available_cash,
            "持仓市值": snapshot.position_value,
            "当日盈亏": snapshot.daily_pnl,
            "持仓": [item.model_dump(mode="json") for item in snapshot.positions],
            "当日委托": [item.model_dump(mode="json") for item in snapshot.orders],
            "当日成交": [item.model_dump(mode="json") for item in snapshot.fills],
            "本月资金流水": [
                item.model_dump(mode="json") for item in snapshot.cash_flows
            ],
            "资金流水历史完整": snapshot.cash_flow_complete,
            "资金流水范围": [
                snapshot.cash_flow_range_start, snapshot.cash_flow_range_end,
            ],
            "当前行情": snapshot.quote.model_dump(mode="json") if snapshot.quote else None,
            "告警": snapshot.warnings,
        }, ensure_ascii=False, indent=2))

    def _runtime_status_changed(self, task: str, detail: str) -> None:
        friendly_task = {
            "broker": "同花顺只读同步",
            "universe": "股票池",
            "daily_candidates": "日线候选",
            "hotspots": "热点",
            "sentiment": "市场情绪",
            "topdown": "四层评分",
            "lifecycle": "持仓生命周期",
        }.get(task, task)
        self.activity_status_label.setText(f"{friendly_task}：{detail}")
        busy = detail.startswith("正在")
        self.header_refresh_button.setEnabled(not busy)
        if task in {"universe", "daily_candidates"}:
            self.universe_status.setText(detail)
            self.universe_generate_button.setEnabled(not busy)

    def _sync_current_cash_flow_page(self) -> None:
        adapter = getattr(self.ctx, "broker_adapter", None)
        if adapter is None:
            return
        try:
            snapshot = adapter.snapshot(read_current_cash_flow_page=True)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "资金流水读取失败", str(exc))
            return
        self._broker_snapshot = snapshot
        if not snapshot.cash_flow_complete:
            QMessageBox.warning(
                self,
                "资金流水未核验",
                "当前页面不能证明本月完整资金流水，月度收益继续保持不可用。",
            )
            return
        if self.store.available:
            self.store.add_broker_snapshot(snapshot)
            if snapshot.complete and snapshot.total_equity is not None:
                self.store.record_broker_financial_snapshot(snapshot)
        self._fill(self.broker_cash_flows, [[
            item.broker_flow_id, "入金" if item.direction == "deposit" else "出金",
            item.amount, item.occurred_at, item.status, item.description, item.source,
        ] for item in snapshot.cash_flows])
        self._refresh_monthly()
        QMessageBox.information(
            self,
            "资金流水已核验",
            f"已保存 {len(snapshot.cash_flows)} 条本月资金流水；未执行任何转账或委托操作。",
        )

    def _show_connection(self, state) -> None:
        color = "#3fb950" if state.usable else "#f85149"
        self.broker_status.setStyleSheet(f"color: {color}; font-weight: 600;")
        self.broker_status.setText(
            f"状态: {state.status.value} | {state.message}\n"
            f"行情PID: {state.market_pid or '-'} | 交易PID: {state.trading_pid or '-'} | "
            f"版本: {state.client_version or '-'}\n"
            f"安装目录: {state.detected_install_path or '-'}\n"
            f"账户指纹: {state.account_fingerprint or '-'}"
        )
        self._refresh_header()

    def _prefill_selected(self) -> None:
        row = self.pending[1].currentRow()
        if row < 0:
            QMessageBox.information(self, "请选择计划", "请先选择一条确定性量化计划。")
            return
        plan_id = self.pending[1].item(row, 0).text()
        plan = self.store.get_plan(plan_id)
        if not plan or not _prefill_strategy_is_supported(
            str(plan.get("strategy_version") or "")
        ):
            QMessageBox.warning(
                self,
                "禁止预填",
                "只有完成四层评分的新策略或显式池外例外计划可以进入实盘授权；"
                "历史基线策略仅用于研究对照。",
            )
            return
        decision = self.store.get_decision(plan["decision_event_id"])
        if not decision:
            return
        signal = SignalDecision.model_validate(decision["final_decision"])
        profile = self.store.get_profile(plan["symbol"])
        if profile is None or not profile.confirmed:
            QMessageBox.warning(self, "缺少品种参数", "请先确认该股票的交易制度和真实费用。")
            return
        snapshot = self.ctx.broker_adapter.snapshot()
        self.store.add_broker_snapshot(snapshot)
        state_text = self.store.current_strategy_state(signal.strategy_id)
        topdown_score = None
        if signal.strategy_id in {TOPDOWN_STRATEGY_ID, "manual_exception_4321_v1"}:
            stored_score = self.store.latest_topdown_score(plan["symbol"])
            if stored_score:
                topdown_score = TopDownScoreSnapshot.model_validate(stored_score["snapshot"])
            if topdown_score is None or not topdown_score.eligible_for_risk:
                reason = "缺少四层评分" if topdown_score is None else (
                    f"当前评分状态为 {topdown_score.status.value}，需要连续两根已收盘15分钟评分通过"
                )
                QMessageBox.warning(self, "四层评分未放行", reason)
                return
        outside_approval_valid = False
        outside_pool_position_count = 0
        if signal.strategy_id == "manual_exception_4321_v1":
            approval = self.store.valid_outside_pool_approval(
                plan_id=plan_id,
                account_fingerprint=snapshot.account_fingerprint,
            )
            if approval is None:
                answer = QMessageBox.question(
                    self,
                    "批准本次池外例外计划",
                    "该股票不属于当前既定股票池。本计划使用半风险、最多同时1只，"
                    "并与正常策略分开统计。批准只在当前计划有效期内生效，是否继续？",
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
                self.store.add_outside_pool_approval(
                    review_id=f"manual-{plan_id}",
                    plan_id=plan_id,
                    account_fingerprint=snapshot.account_fingerprint,
                    effective_risk_pct=self.ctx.settings.risk.per_trade_risk_pct * 0.5,
                    valid_until=plan["valid_until"],
                    audit_reason="user_approved_current_outside_pool_plan_half_risk",
                )
            outside_approval_valid = True
            for position in snapshot.positions:
                if any(
                    item.get("strategy_version") == "manual_exception_4321_v1"
                    for item in self.store.list_plans(symbol=position.symbol)
                ):
                    outside_pool_position_count += 1
        risk = self.ctx.portfolio_risk.authorize(
            plan_id=plan_id,
            signal=signal,
            broker=snapshot,
            portfolio=self._portfolio_snapshot(snapshot),
            strategy_state=StrategyState(state_text),
            profile=profile,
            external_quote_price=plan.get("last_price") or signal.trigger_price,
            topdown_score=topdown_score,
            trading_channel=(
                "outside_pool_exception"
                if signal.strategy_id == "manual_exception_4321_v1" else "normal_pool"
            ),
            outside_pool_approval_valid=outside_approval_valid,
            outside_pool_position_count=outside_pool_position_count,
        )
        self.store.append_event(
            plan_id, "risk_authorization", details=risk.model_dump(mode="json")
        )
        if risk.order is None:
            QMessageBox.warning(self, "风控阻断", "\n".join(risk.reasons))
            return
        receipt = self.ctx.broker_adapter.prefill(risk.order)
        self.store.append_event(
            plan_id, "broker_prefill", details=receipt.model_dump(mode="json")
        )
        if receipt.status == "awaiting_user_confirmation":
            self.store.update_plan(plan_id, status="awaiting_user_confirmation")
            self.store.append_event(
                plan_id,
                "awaiting_user_confirmation",
                details={"authorized_order": risk.order.model_dump(mode="json")},
            )
            self._start_reconciliation(risk.order)
            self.refresh()
            QMessageBox.information(self, "等待人工确认", receipt.message)
        else:
            QMessageBox.warning(self, "预填未完成", receipt.message)

    def _portfolio_snapshot(self, snapshot):
        from pa_agent.trading.equity import portfolio_snapshot_from_store

        return portfolio_snapshot_from_store(self.store, snapshot)

    def _start_reconciliation(self, order) -> None:
        self._reconciliation_order = order
        if self._quant_runtime is not None:
            self._quant_runtime.begin_reconciliation(order.plan_id)
        self._reconciliation_attempts = 0
        self._reconciliation_matched = False
        self._reconciliation_timer.start()

    def _poll_reconciliation(self) -> None:
        order = self._reconciliation_order
        if order is None:
            self._reconciliation_timer.stop()
            return
        self._reconciliation_attempts += 1
        try:
            snapshot = self.ctx.broker_adapter.snapshot()
            self._broker_snapshot = snapshot
            self.store.add_broker_snapshot(snapshot)
            reconciliation = self.ctx.broker_adapter.reconcile(order, snapshot)
            if reconciliation.status == "matched":
                self._apply_reconciliation(order, snapshot, reconciliation)
                self._reconciliation_matched = True
                broker_order = next(
                    item for item in snapshot.orders
                    if item.broker_order_id == reconciliation.matched_order_ids[0]
                )
                status, _ = self.ctx.broker_trade_lifecycle.broker_order_status(
                    broker_order.status, broker_order.filled_quantity, broker_order.quantity
                )
                if status in {"filled", "cancelled", "rejected"}:
                    self._reconciliation_timer.stop()
                    if self._quant_runtime is not None:
                        self._quant_runtime.end_reconciliation(order.plan_id)
                    self._reconciliation_order = None
                self.refresh()
                if self._reconciliation_order is None:
                    return
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("同花顺成交对账轮询失败: %s", exc)
        if self._reconciliation_attempts >= 30:
            self._reconciliation_timer.stop()
            if not self._reconciliation_matched:
                self.store.update_plan(order.plan_id, status="reconciliation_required")
                self.store.append_event(
                    order.plan_id,
                    "reconciliation_required",
                    details={"poll_attempts": self._reconciliation_attempts, "window_seconds": 60},
                )
            if self._quant_runtime is not None:
                self._quant_runtime.end_reconciliation(order.plan_id)
            self._reconciliation_order = None
            self.refresh()

    def _apply_reconciliation(self, order, snapshot, reconciliation) -> None:
        broker_order_id = reconciliation.matched_order_ids[0]
        broker_order = next(
            item for item in snapshot.orders if item.broker_order_id == broker_order_id
        )
        matched_ids = set(reconciliation.matched_fill_ids)
        fills = [item for item in snapshot.fills if item.broker_fill_id in matched_ids]
        status, event_type = self.ctx.broker_trade_lifecycle.broker_order_status(
            broker_order.status, broker_order.filled_quantity, broker_order.quantity
        )
        plan_status = "executed_open" if status == "filled" else status
        self.store.link_broker_order(
            reconciliation,
            details={
                "order": broker_order.model_dump(mode="json"),
                "authorized_order": order.model_dump(mode="json"),
                "poll_attempts": self._reconciliation_attempts,
            },
        )
        self.store.upsert_broker_execution(
            plan_id=order.plan_id,
            fills=fills,
            plan_status=plan_status,
            event_type=event_type,
            broker_order_id=broker_order_id,
        )
        self._record_external_manual_fills(snapshot)

    def _refresh_linked_broker_orders(self, snapshot) -> None:
        from pa_agent.trading.broker_models import ReconciliationResult

        for link in self.store.list_broker_order_links():
            broker_order = next(
                (
                    item for item in snapshot.orders
                    if item.broker_order_id == link["broker_order_id"]
                ),
                None,
            )
            if broker_order is None:
                continue
            fills = [
                item for item in snapshot.fills
                if item.broker_order_id == broker_order.broker_order_id
            ]
            self.store.link_broker_order(
                ReconciliationResult(
                    status="matched",
                    plan_id=link["plan_id"],
                    matched_order_ids=[broker_order.broker_order_id],
                    matched_fill_ids=[item.broker_fill_id for item in fills if item.broker_fill_id],
                    message="周期同步更新已关联委托成交",
                ),
                details={
                    **link.get("details", {}),
                    "order": broker_order.model_dump(mode="json"),
                    "periodic_sync": True,
                },
            )
            status, event_type = self.ctx.broker_trade_lifecycle.broker_order_status(
                broker_order.status, broker_order.filled_quantity, broker_order.quantity
            )
            self.store.upsert_broker_execution(
                plan_id=link["plan_id"],
                fills=fills,
                plan_status="executed_open" if status == "filled" else status,
                event_type=event_type,
                broker_order_id=broker_order.broker_order_id,
            )

    def _manually_link_broker_order(self) -> None:
        if self._broker_snapshot is None or self.broker_orders.currentRow() < 0:
            QMessageBox.information(self, "请选择委托", "请先同步并选择一条同花顺当日委托。")
            return
        broker_order_id = self.broker_orders.item(
            self.broker_orders.currentRow(), 0
        ).text()
        plans = self.store.list_plans(statuses=["reconciliation_required"])
        labels = [f"{item['id']} | {item['symbol']} | {item['direction']}" for item in plans]
        if not labels:
            QMessageBox.information(self, "无需关联", "当前没有等待人工对账的交易计划。")
            return
        selected, ok = QInputDialog.getItem(
            self, "人工关联委托", "选择对应量化计划：", labels, 0, False
        )
        if not ok:
            return
        plan = plans[labels.index(selected)]
        broker_order = next(
            item for item in self._broker_snapshot.orders
            if item.broker_order_id == broker_order_id
        )
        if broker_order.symbol != plan["symbol"] or broker_order.direction != plan["direction"]:
            QMessageBox.warning(self, "字段不一致", "委托的代码或方向与计划不一致，禁止关联。")
            return
        from pa_agent.trading.broker_models import ReconciliationResult

        fills = [
            item for item in self._broker_snapshot.fills
            if item.broker_order_id == broker_order_id
        ]
        reconciliation = ReconciliationResult(
            status="matched",
            plan_id=plan["id"],
            matched_order_ids=[broker_order_id],
            matched_fill_ids=[item.broker_fill_id for item in fills if item.broker_fill_id],
            message="用户人工核对后关联",
        )
        self.store.link_broker_order(
            reconciliation,
            details={"manual_review": True, "order": broker_order.model_dump(mode="json")},
        )
        status, event_type = self.ctx.broker_trade_lifecycle.broker_order_status(
            broker_order.status, broker_order.filled_quantity, broker_order.quantity
        )
        self.store.upsert_broker_execution(
            plan_id=plan["id"], fills=fills,
            plan_status="executed_open" if status == "filled" else status,
            event_type=event_type, broker_order_id=broker_order_id,
        )
        self.store.append_event(
            plan["id"], "manual_reconciliation_confirmed",
            details={"broker_order_id": broker_order_id},
        )
        self.refresh()

    def _record_external_manual_fills(self, snapshot) -> None:
        # A just-prefilled order may appear between the 2-second reconciliation ticks.
        # Wait until its bounded reconciliation window ends before classifying unknown fills.
        if self._reconciliation_order is not None:
            return
        linked = self.store.linked_broker_fill_ids()
        pending_matches = {
            (item["symbol"], item["direction"])
            for item in self.store.list_plans(
                statuses=["awaiting_user_confirmation", "reconciliation_required"]
            )
        }
        for fill in snapshot.fills:
            if not fill.broker_fill_id or fill.broker_fill_id in linked:
                continue
            if (fill.symbol, fill.direction) in pending_matches:
                continue
            if self.store.add_external_broker_trade(
                fill, account_fingerprint=snapshot.account_fingerprint
            ):
                self.ctx.logger.info(
                    "EXTERNAL_MANUAL_TRADE %s %s %s",
                    fill.broker_fill_id, fill.symbol, fill.direction,
                )

    def refresh(self) -> None:
        health = self.store.health()
        self.health_label.setText(
            f"SQLite: {'可用' if health['available'] else '故障'} | {health['path']}"
            + (f" | {health['error']}" if health["error"] else "")
        )
        if not health["available"]:
            return
        plans = self.store.list_plans()
        score_records = self.store.list_topdown_scores(limit=1000)
        scores_by_symbol: dict[str, dict] = {}
        for record in score_records:
            scores_by_symbol.setdefault(record["symbol"], record["snapshot"])
        universes = self.store.list_universe_snapshots()
        current_universe = universes[0]["snapshot"] if universes else {}
        members = list(current_universe.get("symbols") or [])
        daily_signals = [
            item for item in self.store.list_quant_signals(
                strategy_id=self.ctx.settings.strategy.strategy_id,
                limit=max(500, len(members) * 4),
            )
            if item.get("pool_version") == current_universe.get("version")
        ]
        latest_signal_day = max(
            (str(item.get("signal_time") or "")[:10] for item in daily_signals),
            default="",
        )
        latest_daily_signals: dict[str, dict] = {}
        for item in daily_signals:
            if str(item.get("signal_time") or "")[:10] != latest_signal_day:
                continue
            latest_daily_signals.setdefault(item.get("symbol", ""), item)
        daily_allowed = {
            symbol for symbol, item in latest_daily_signals.items()
            if str(item.get("status") or "").lower() == "allow"
        }
        pool_members = {
            item.get("symbol", ""): item for item in current_universe.get("members") or []
        }
        broker_positions = {
            item.symbol: item for item in (self._broker_snapshot.positions if self._broker_snapshot else [])
        }
        plans_by_symbol: dict[str, dict] = {}
        for plan in plans:
            plans_by_symbol.setdefault(plan["symbol"], plan)
        universe_rows = []
        for rank, symbol in enumerate(members, 1):
            score = scores_by_symbol.get(symbol) or {}
            plan = plans_by_symbol.get(symbol) or {}
            hotspot = self.store.latest_hotspot_snapshot(symbol)
            hotspot_data = (hotspot or {}).get("snapshot") or {}
            themes = hotspot_data.get("concepts") or hotspot_data.get("industries") or []
            titles = [item.get("title", "") for item in hotspot_data.get("items", [])[:1]]
            blocks = score.get("hard_blocks") or score.get("data_gaps") or []
            pool_member = pool_members.get(symbol) or {}
            daily_signal = latest_daily_signals.get(symbol)
            daily_reasons = list((daily_signal or {}).get("decision", {}).get("reasons") or [])
            daily_status = (
                "只分析" if not pool_member.get("authorization_eligible", True)
                else "待扫描" if daily_signal is None
                else "通过" if symbol in daily_allowed
                else "未通过"
            )
            universe_rows.append([
                rank, symbol,
                pool_member.get("name") or self._broker_name_for(symbol),
                " / ".join(filter(None, [
                    pool_member.get("tier"), pool_member.get("theme")
                ])) or " / ".join(themes[:2]) or pool_member.get("industry", ""),
                _large_money(pool_member.get("average_amount_20")),
                pool_member.get("latest_price", ""),
                _pct_value(pool_member.get("latest_pct_chg")),
                (
                    "分析池（暂不授权）"
                    if not pool_member.get("authorization_eligible", True)
                    else "正式股票池"
                ), daily_status,
                _score_text(score.get("index_score"), 40),
                _score_text(score.get("sentiment_score"), 30),
                _score_text(score.get("theme_score"), 20),
                _score_text(score.get("stock_score"), 10),
                _score_text(score.get("total_score"), 100),
                (
                    titles[0]
                    if titles
                    else ", ".join(blocks[:2] or daily_reasons[:2])
                    or "无有效热点快照"
                ),
                "持仓中" if symbol in broker_positions else plan.get("status", "无计划"),
            ])
        self._fill(self.universe[1], universe_rows)
        if universes:
            record = universes[0]
            if record.get("data_complete"):
                self.universe_status.setText(
                    f"{current_universe.get('version', '')} | 基础池 {len(members)}只 | "
                    f"日线扫描 {len(latest_daily_signals)}只 | "
                    f"今日候选 {len(daily_allowed)}只 | "
                    f"固定成员日期 {current_universe.get('source_as_of', '—')} | "
                    f"数据更新时间 {record.get('source_updated_at', '—')}"
                )
            else:
                self.universe_status.setText(
                    "股票池数据不完整，禁止授权："
                    + ", ".join(current_universe.get("completeness_reasons") or [])
                )
        candidate_rows = []
        for symbol in members:
            if symbol not in daily_allowed:
                continue
            score = scores_by_symbol.get(symbol) or {}
            plan = plans_by_symbol.get(symbol) or {}
            candidate_rows.append([
                symbol, (pool_members.get(symbol) or {}).get("name")
                or self._broker_name_for(symbol), "通过",
                _score_text(score.get("total_score"), 100),
                score.get("consecutive_pass_count", 0), score.get("status", "数据不完整"),
                "进入组合风控" if score.get("status") == "eligible_for_risk"
                else "继续观察",
            ])
        self._fill(self.candidate_table, candidate_rows)
        self.universe_tabs.setTabText(0, f"当前基础池（{len(members)}）")
        self.universe_tabs.setTabText(1, f"今日候选（{len(candidate_rows)}）")
        self.candidate_empty_label.setVisible(not candidate_rows)
        if not candidate_rows:
            scan_text = (
                f"已扫描 {len(latest_daily_signals)}只，全部未通过当前日线买点条件。"
                if latest_daily_signals else "日线候选扫描尚未完成。"
            )
            self.candidate_empty_label.setText(
                f"基础池有 {len(members)}只；今日候选 0只。{scan_text}"
                "这不是股票池无数据，系统不会为凑候选而放宽策略。"
            )
        rejected = current_universe.get("rejected") or {}
        self._fill(self.excluded_table, [[
            symbol, ", ".join(reasons), current_universe.get("version", ""),
            current_universe.get("as_of", ""),
        ] for symbol, reasons in sorted(rejected.items())])
        self._fill(self.universe_history_table, [[
            item["snapshot"].get("version", item.get("version", "")),
            item.get("as_of", ""), len(item["snapshot"].get("symbols") or []),
            len(item["snapshot"].get("rejected") or {}),
            "完整" if item.get("data_complete") else "数据不完整",
            item.get("source_updated_at", ""),
        ] for item in universes])
        pending = [
            p
            for p in plans
            if _prefill_strategy_is_supported(
                str(p.get("strategy_version") or "")
            )
            and p["status"]
            in {
                "proposed",
                "triggered",
                "awaiting_user_confirmation",
                "reconciliation_required",
                "submitted",
                "partially_filled",
                "cancelled",
                "rejected",
                "ignored",
                "expired",
                "invalidated",
            }
        ]
        self._fill(self.pending[1], [[
            p["id"], _strategy_label(p["strategy_version"]),
            f"{p['symbol']} {self._broker_name_for(p['symbol'])}",
            (p.get("risk_snapshot") or {}).get("pool_version", ""), p["direction"], p["entry_price"],
            (p.get("risk_snapshot") or {}).get("max_entry_price", ""), p["stop_loss_price"],
            _score_text((scores_by_symbol.get(p["symbol"]) or {}).get("total_score"), 100),
            (scores_by_symbol.get(p["symbol"]) or {}).get("consecutive_pass_count", 0),
            p["status"], p["valid_until"],
        ] for p in pending])
        actual = [p for p in plans if p["status"] in {
            "partially_filled", "executed_open", "exit_detected",
        }]
        actual_rows = []
        for p in actual:
            execution = self.store.get_execution(p["id"]) or {}
            quantity = float(execution.get("quantity") or 0)
            entry = float(execution.get("price") or 0)
            initial_stop_distance = (
                abs(entry - float(p["stop_loss_price"])) if entry else 0
            )
            active_stop = float(p.get("actual_active_stop") or p["stop_loss_price"])
            current_risk = max(0.0, entry - active_stop) * quantity if entry else 0
            last_price = p.get("last_price")
            floating_r = (
                (float(last_price) - entry) / initial_stop_distance
                if last_price is not None and initial_stop_distance > 0 else None
            )
            exit_events = [
                event for event in self.store.list_events(p["id"])
                if event.get("dataset") == "actual"
                and event.get("event_type") in {
                    "stop_detected", "trailing_stop_detected", "time_exit_detected",
                    "tp1_detected", "tp2_detected", "t1_locked_breach",
                }
            ]
            exit_status = (
                exit_events[-1]["event_type"] if exit_events
                else "未触发"
            )
            if p["status"] == "exit_detected":
                exit_status = f"待人工确认：{exit_status}"
            actual_rows.append([
                p["id"], p["symbol"], execution.get("price", ""), execution.get("quantity", ""),
                active_stop, current_risk,
                "-" if floating_r is None else f"{floating_r:.2f}R", exit_status,
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
            "基线策略状态": self.store.current_strategy_state(
                self.ctx.settings.strategy.strategy_id
            ),
            "4:3:2:1策略状态": self.store.current_strategy_state(TOPDOWN_STRATEGY_ID),
        }, ensure_ascii=False, indent=2))
        self._refresh_actions(plans, scores_by_symbol)
        self._refresh_header()
        self._refresh_monthly()
        self._refresh_validation()

    def _show_selected_audit(self, _index=None) -> None:
        current = self.tabs.currentIndex()
        table = self.pending[1] if current == 3 else self.open_positions[1] if current == 4 else None
        if table is None and self.pending[1].currentRow() >= 0:
            table = self.pending[1]
        if table is None or table.currentRow() < 0:
            return
        plan_id = table.item(table.currentRow(), 0).text()
        plan = self.store.get_plan(plan_id)
        if not plan:
            return
        decision = self.store.get_decision(plan["decision_event_id"])
        events = self.store.list_events(plan_id)
        signals = [item for item in self.store.list_quant_signals() if item.get("plan_id") == plan_id]
        self.audit_text.setPlainText(json.dumps({
            "plan": plan, "quant_signals": signals, "decision": decision, "events": events,
        }, ensure_ascii=False, indent=2, default=str))
        record_path = Path(plan.get("analysis_record_ref") or "")
        if record_path.is_file():
            try:
                from pa_agent.demo.record_loader import (
                    frame_from_record_klines,
                    load_analysis_record,
                )

                record = load_analysis_record(record_path)
                frame = frame_from_record_klines(
                    record.kline_data, symbol=record.meta.symbol, timeframe=record.meta.timeframe,
                    snapshot_ts_local_ms=record.meta.timestamp_local_ms,
                )
                self.audit_chart.set_frame(frame)
                self.audit_chart.fit_view()
            except Exception:  # noqa: BLE001
                self.audit_chart.clear_decision_overlay()
        self.tabs.setCurrentIndex(8)

    def _refresh_header(self) -> None:
        state = getattr(getattr(self.ctx, "broker_adapter", None), "connection", None)
        usable = bool(state and state.usable)
        masked = getattr(self.ctx.settings.ths, "masked_account", "") or "未确认账户"
        strategy_state = self.store.current_strategy_state(TOPDOWN_STRATEGY_ID) if self.store.available else "candidate"
        universes = self.store.list_universe_snapshots(limit=1) if self.store.available else []
        universe = universes[0]["snapshot"] if universes else {}
        pool_version = universe.get("version") or "股票池未加载"
        captured = self._broker_snapshot.captured_at if self._broker_snapshot else "尚未同步"
        self.system_status_line.setText(
            f"同花顺 ● {'已连接' if usable else '未就绪'} | 账户 {masked} | 策略 {strategy_state.upper()} | "
            f"股票池 {pool_version} | 最近同步 {captured}"
        )
        score_record = self.store.latest_topdown_score() if self.store.available else None
        score = (score_record or {}).get("snapshot") or {}
        for key, maximum in (("index", 40), ("sentiment", 30), ("theme", 20), ("stock", 10)):
            self.score_labels[key].setText(f"{_score_name(key)} {_score_text(score.get(key + '_score'), maximum)}")
        total = score.get("total_score")
        if total is None:
            self.total_score_label.setText("综合 —/100 | 数据不完整，禁止授权")
            self.total_score_label.setStyleSheet(
                "background:#2b1818; color:#fca5a5; border:1px solid #5d2b2b; border-radius:4px; padding:5px;"
            )
        else:
            self.total_score_label.setStyleSheet(
                "background:#17252a; color:#5eead4; border:1px solid #24534d; "
                "border-radius:4px; padding:5px 10px; font-weight:700;"
            )
            self.total_score_label.setText(
                f"综合 {float(total):.1f}/100 | 连续{score.get('consecutive_pass_count', 0)}根 | {score.get('status', '')}"
            )

    def _refresh_actions(self, plans: list[dict], scores: dict[str, dict]) -> None:
        rows = []
        priority = {
            "invalidated": 1, "exit_detected": 2, "reconciliation_required": 3,
            "awaiting_user_confirmation": 4, "authorized": 5, "proposed": 7,
        }
        for plan in plans:
            # The action queue is operational.  Legacy AI/imported research is
            # retained in audit/history, but must never look like a pending
            # deterministic 4:3:2:1 trading action.
            if not _prefill_strategy_is_supported(
                str(plan.get("strategy_version") or "")
            ):
                continue
            status = plan.get("status", "")
            if status in {"closed", "ignored", "expired"}:
                continue
            score = scores.get(plan["symbol"]) or {}
            blocks = score.get("hard_blocks") or score.get("data_gaps") or []
            events = self.store.list_events(plan["id"])
            has_t1_risk = any(
                event.get("event_type") == "t1_locked_breach" for event in events
            )
            next_step = (
                "处理退出" if status == "exit_detected" else
                "T+1锁定，下一交易日优先处理" if has_t1_risk else
                "人工对账" if status == "reconciliation_required" else
                "在同花顺确认" if status == "awaiting_user_confirmation" else
                "核对并预填" if score.get("status") == "eligible_for_risk" else
                "等待15分钟评分确认"
            )
            rows.append([
                3 if has_t1_risk else priority.get(status, 8),
                f"{plan['symbol']} {self._broker_name_for(plan['symbol'])}",
                _strategy_label(plan.get("strategy_version", "")),
                _score_text(score.get("total_score"), 100), status, next_step,
                ", ".join(blocks[:3]) or "—",
            ])
        rows.sort(key=lambda item: item[0])
        self._fill(self.action_table, rows)
        self.action_table.setVisible(bool(rows))
        self.today_empty.setVisible(not rows)

    def _refresh_monthly(self) -> None:
        account = (
            self._broker_snapshot.account_fingerprint
            if self._broker_snapshot else ""
        )
        snapshots = (
            self.store.list_equity_snapshots(account_fingerprint=account)
            if self.store.available else []
        )
        from pa_agent.trading.equity import (
            monthly_equity_peak_drawdown_pct,
            monthly_return_pct,
        )
        latest = snapshots[-1] if snapshots else {}
        current_month = str(latest.get("captured_at") or "")[:7]
        month_snapshots = [
            item for item in snapshots
            if str(item.get("captured_at") or "").startswith(current_month)
        ]
        monthly = monthly_return_pct(month_snapshots)
        flows = (
            self.store.list_broker_cash_flows(
                account_fingerprint=account,
                start_at=f"{current_month}-01T00:00:00+08:00" if current_month else "",
                end_at=str(latest.get("captured_at") or ""),
            )
            if account and current_month else []
        )
        deposits = sum(
            float(item["amount"]) for item in flows if item["direction"] == "deposit"
        )
        withdrawals = sum(
            float(item["amount"]) for item in flows if item["direction"] == "withdrawal"
        )
        month_start = (
            f"{current_month}-01T00:00:00+08:00" if current_month else ""
        )
        cash_flow_complete = bool(
            account and month_start and latest
            and self.store.cash_flow_history_complete(
                account,
                range_start=month_start,
                range_end=str(latest.get("captured_at") or ""),
            )
        )
        peak_drawdown = monthly_equity_peak_drawdown_pct(month_snapshots)
        data_gaps = []
        if len(month_snapshots) < 2:
            data_gaps.append("月度权益基线不足")
        if not cash_flow_complete:
            data_gaps.append("同花顺本月资金流水尚未核验完整")
        trusted_monthly = monthly if not data_gaps else None
        self.monthly_summary.setPlainText(
            f"月初权益：{month_snapshots[0].get('total_equity', '—') if month_snapshots else '—'}\n"
            f"最新总资产：{latest.get('total_equity', '—')}\n"
            f"本月入金：{deposits:.2f}\n"
            f"本月出金：{withdrawals:.2f}\n"
            f"净外部现金流：{deposits - withdrawals:+.2f}\n"
            f"扣除出入金后的月度收益："
            f"{'—' if trusted_monthly is None else f'{trusted_monthly:+.2f}%'}\n"
            f"本月高点回撤：{'—' if peak_drawdown is None else f'{peak_drawdown:.2f}%'}\n"
            f"权益快照数：{len(month_snapshots)}\n"
            f"资金流水条数：{len(flows)}\n"
            f"资金流水完整性：{'已核验' if cash_flow_complete else '未核验'}\n"
            f"数据缺口：{'；'.join(data_gaps) if data_gaps else '无'}\n\n"
            "正常股票池策略、池外例外与外部手工交易必须分开归因；账户净值仍统一纳入。"
        )
        position_count = len(self._broker_snapshot.positions) if self._broker_snapshot else 0
        actual_trade_count = len(self.store.list_results(dataset="actual"))
        portfolio = self.ctx.settings.portfolio_risk
        initial_stage = actual_trade_count < portfolio.initial_live_trade_count
        effective_max_open = self.ctx.settings.risk.max_open_risk_pct
        if initial_stage:
            effective_max_open = min(
                effective_max_open,
                portfolio.initial_max_open_risk_pct,
            )
        risk_stage = (
            f"首批阶段 {actual_trade_count}/{portfolio.initial_live_trade_count}笔"
            if initial_stage else "升级后"
        )
        self.risk_status_line.setText(
            f"本月收益 {'—' if trusted_monthly is None else f'{trusted_monthly:+.2f}%'} | "
            f"高点回撤 {'—' if peak_drawdown is None else f'{peak_drawdown:.2f}%'} | "
            f"开放风险 —/{effective_max_open:.2f}%（{risk_stage}） | "
            f"持仓 {position_count}/{portfolio.max_positions} | "
            f"待处理 {self.action_table.rowCount()}"
        )

    def _refresh_validation(self) -> None:
        state = self.store.current_strategy_state(TOPDOWN_STRATEGY_ID)
        scores = self.store.list_topdown_scores(limit=1000)
        eligible = sum(1 for item in scores if item["status"] == "eligible_for_risk")
        validation_runs = self.store.list_validation_runs(
            strategy_version=TOPDOWN_STRATEGY_ID,
            limit=20,
        )
        fixed = next(
            (item for item in validation_runs if item["dataset"] == "fixed_replay"),
            None,
        )
        fixed_report = (fixed or {}).get("report") or {}
        fixed_checks = fixed_report.get("checks") or []
        fixed_passed = sum(bool(item.get("passed")) for item in fixed_checks)
        promotion_runs = [
            item for item in validation_runs if item.get("promotion_eligible")
        ]
        oos_bundle = next(
            (
                item for item in validation_runs
                if item.get("dataset") == "out_of_sample_data_bundle"
            ),
            None,
        )
        oos_bundle_report = (oos_bundle or {}).get("report") or {}
        persisted_bundle_path = str(oos_bundle_report.get("bundle_path") or "")
        if (
            not self._validated_oos_bundle_path
            and oos_bundle_report.get("status") == "complete"
            and persisted_bundle_path
            and Path(persisted_bundle_path).is_file()
        ):
            self._validated_oos_bundle_path = persisted_bundle_path
        oos_backtest = next(
            (
                item for item in validation_runs
                if item.get("dataset") == "out_of_sample"
            ),
            None,
        )
        oos_backtest_report = (oos_backtest or {}).get("report") or {}
        oos_evidence = oos_backtest_report.get("performance_evidence") or {}
        self.run_oos_button.setEnabled(
            bool(
                self._validated_oos_bundle_path
                and oos_bundle_report.get("status") == "complete"
            )
            and not (
                self._oos_backtest_thread is not None
                and self._oos_backtest_thread.isRunning()
            )
        )
        sentiment_days = self.store.market_daily_price_dates(limit=30)
        historical_pool_count = sum(
            1
            for item in self.store.list_universe_snapshots(limit=120)
            if (item.get("snapshot") or {}).get("source_kind")
            == "historical_constituents"
        )
        shadow_evidence, shadow_gaps = build_shadow_performance_evidence(
            self.store, strategy_id=TOPDOWN_STRATEGY_ID
        )
        shadow_transition = self.ctx.strategy_stability.evaluate(
            StrategyState.SHADOW, shadow_evidence
        ) if getattr(self.ctx, "strategy_stability", None) else None
        shadow_gate_passed = bool(
            shadow_transition
            and shadow_transition.reasons
            == ["awaiting_explicit_live_activation_approval"]
            and not shadow_gaps
        )
        broker_binding = self.ctx.settings.ths
        account_bound = bool(
            broker_binding.confirmed
            and broker_binding.account_fingerprint
            and broker_binding.masked_account
        )
        self.small_live_button.setEnabled(
            state == StrategyState.SHADOW.value
            and shadow_gate_passed
            and account_bound
        )
        shadow_pf = shadow_evidence.profit_factor
        shadow_pf_text = "—" if shadow_pf is None else (
            "∞" if shadow_pf == float("inf") else f"{shadow_pf:.2f}"
        )
        self.validation_summary.setPlainText(
            f"当前策略：{TOPDOWN_STRATEGY_ID}\n"
            f"当前状态：{state.upper()}\n"
            f"冻结15分钟评分快照：{len(scores)}\n"
            f"达到连续确认并可进入风控：{eligible}\n\n"
            f"固定机制回放：{fixed_report.get('status', '尚未运行')} | "
            f"通过 {fixed_passed}/{len(fixed_checks)} 项 | 不可用于晋级\n"
            f"历史沪深300股票池版本：{historical_pool_count}个\n"
            f"市场情绪日级历史积累：{len(sentiment_days)}/20个完整交易日\n"
            f"可用于晋级的样本外/影子验证记录：{len(promotion_runs)}条\n\n"
            f"样本外数据包：{oos_bundle_report.get('status', '尚未导入')} | "
            f"缺口 {len(oos_bundle_report.get('data_gaps') or [])}项 | "
            "数据包本身不可用于晋级\n\n"
            "日线+15分钟组合样本外回测："
            f"{oos_backtest_report.get('status', '尚未运行')} | "
            f"交易 {oos_evidence.get('trade_count', 0)}/200笔 | "
            f"期望R {oos_evidence.get('expectancy_r', '—')} | "
            f"PF {oos_evidence.get('profit_factor', '—')} | "
            f"最大回撤 {oos_evidence.get('max_drawdown_pct', '—')}% | "
            f"盈利月份比例 {oos_evidence.get('profitable_month_ratio', '—')}\n"
            f"- 晋级结果：{'可进入SHADOW' if (oos_backtest or {}).get('promotion_eligible') else '未达到门槛'}\n"
            f"- 数据缺口：{', '.join(oos_backtest_report.get('data_gaps') or []) or '无'}\n"
            f"- 门槛缺口：{', '.join(oos_backtest_report.get('gate_failures') or []) or '无'}\n\n"
            "影子交易晋级门槛（系统从真实shadow结果自动计算）：\n"
            f"- 交易笔数：{shadow_evidence.trade_count}/80\n"
            f"- 观察周期：{shadow_evidence.weeks:.2f}/12周\n"
            f"- 完整自然月：{shadow_evidence.complete_months}个 | "
            f"全部为正：{'是' if shadow_evidence.all_complete_months_profitable else '否'}\n"
            f"- Profit Factor：{shadow_pf_text}/1.15\n"
            f"- 评分来源可追溯：{'是' if shadow_evidence.source_time_alignment_verified else '否'}\n"
            f"- 费用/T+1/涨跌停机制：{'已验证' if shadow_evidence.execution_rules_verified else '未验证'}\n"
            f"- 当前缺口：{', '.join(shadow_gaps) if shadow_gaps else '无'}\n"
            f"- 小资金实盘批准：{'可操作' if self.small_live_button.isEnabled() else '未开放'}\n\n"
            "新策略初始为 CANDIDATE。完成无未来数据回放、样本外验证和至少12周/80笔影子交易前，"
            "不得启用真实交易；池外例外交易不计入策略晋级样本。"
        )

    def _approve_small_live(self) -> None:
        if not self.small_live_button.isEnabled():
            QMessageBox.warning(
                self,
                "暂不能批准",
                "影子交易门槛、历史证据或同花顺账户绑定尚未全部完成。",
            )
            return
        answer = QMessageBox.question(
            self,
            "确认进入小资金实盘",
            "确认仅以0.25%单笔风险进入首批30笔小资金实盘。"
            "这不会开启自动下单，任何委托仍必须由你在同花顺最终确认。",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        from pa_agent.trading.stability import LiveActivationApproval

        binding = self.ctx.settings.ths
        approval = LiveActivationApproval(
            approved_at=datetime.now().astimezone().isoformat(),
            account_fingerprint=binding.account_fingerprint,
            initial_risk_pct=0.25,
            acknowledgment_version="small_live_v1",
        )
        service = getattr(self.ctx, "strategy_promotion", None) or StrategyPromotionService(
            self.store
        )
        try:
            service.activate_small_live(approval)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "批准失败", str(exc))
            return
        self.refresh()
        QMessageBox.information(
            self,
            "策略已进入ACTIVE",
            "策略状态已进入小资金实盘阶段；全局实盘开关和同花顺安全预填仍是独立硬闸门，"
            "系统不会自动点击任何委托按钮。",
        )

    def _import_oos_bundle(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "选择样本外验证数据包",
            "",
            "PA OOS Bundle (*.zip)",
        )
        if not filename:
            return
        from pa_agent.trading.oos_bundle import validate_oos_bundle

        report = validate_oos_bundle(Path(filename))
        self.store.add_validation_run(
            report,
            dataset="out_of_sample_data_bundle",
            promotion_eligible=False,
        )
        self._validated_oos_bundle_path = (
            str(Path(filename).resolve()) if report.status == "complete" else ""
        )
        self.refresh()
        if report.status == "complete":
            QMessageBox.information(
                self,
                "数据包校验通过",
                "历史成分、日线、15分钟、情绪和热点文件的来源时间与哈希校验通过。"
                "数据包本身不能晋级；仍需运行联合回测并达到不少于200笔的绩效门槛。",
            )
        else:
            QMessageBox.warning(
                self,
                "数据包不完整",
                "\n".join(report.data_gaps[:20]),
            )

    def _run_oos_backtest(self) -> None:
        if not self.run_oos_button.isEnabled() or not self._validated_oos_bundle_path:
            return
        from pa_agent.trading.oos_backtest import OosPortfolioBacktester

        self.run_oos_button.setEnabled(False)
        self.run_oos_button.setText("正在运行组合样本外回测…")
        engine = OosPortfolioBacktester(
            strategy_settings=self.ctx.settings.strategy,
            scoring_settings=self.ctx.settings.topdown_scoring,
        )
        thread = QThread(self)
        worker = _OosBacktestWorker(
            engine, Path(self._validated_oos_bundle_path)
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._oos_backtest_finished)
        worker.failed.connect(self._oos_backtest_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._oos_backtest_thread_finished)
        self._oos_backtest_thread = thread
        self._oos_backtest_worker = worker
        thread.start()

    def _oos_backtest_finished(self, report) -> None:
        service = getattr(self.ctx, "strategy_promotion", None) or StrategyPromotionService(
            self.store
        )
        try:
            service.record_out_of_sample_report(report)
        except Exception as exc:  # noqa: BLE001
            self._oos_backtest_failed(str(exc))
            return
        self.run_oos_button.setText("运行日线+15分钟组合样本外回测")
        self.refresh()
        if report.promotion_eligible:
            QMessageBox.information(
                self,
                "样本外门槛通过",
                "组合样本外回测达到全部门槛, 策略状态已进入 SHADOW。"
                "真实交易仍关闭; 下一阶段必须完成至少12周和80笔影子交易。",
            )
        else:
            QMessageBox.information(
                self,
                "样本外回测已完成",
                f"交易 {report.performance_evidence.get('trade_count', 0)}笔; "
                "当前未达到晋级门槛。系统不会放宽参数或伪造历史数据。",
            )

    def _oos_backtest_failed(self, error: str) -> None:
        self.run_oos_button.setText("运行日线+15分钟组合样本外回测")
        self.run_oos_button.setEnabled(bool(self._validated_oos_bundle_path))
        QMessageBox.warning(self, "样本外回测失败", error)

    def _oos_backtest_thread_finished(self) -> None:
        self._oos_backtest_thread = None
        self._oos_backtest_worker = None
        self.run_oos_button.setText("运行日线+15分钟组合样本外回测")
        self.refresh()

    def _broker_name_for(self, symbol: str) -> str:
        if self._broker_snapshot:
            if self._broker_snapshot.quote and self._broker_snapshot.quote.symbol == symbol:
                return self._broker_snapshot.quote.name
            for position in self._broker_snapshot.positions:
                if position.symbol == symbol:
                    return position.name
        return ""

    def _load_stock_detail(self) -> None:
        raw = self.stock_symbol.text().strip()
        if not raw:
            return
        symbol = raw
        if not raw.isdigit():
            try:
                from pa_agent.data.tv_symbol_lookup import resolve_tv_symbol_name

                _, symbol = resolve_tv_symbol_name(raw)
            except Exception as exc:  # noqa: BLE001
                self._show_stock_profile_error(raw, str(exc))
                return
        symbol = symbol[-6:].zfill(6)
        self.stock_symbol.setText(symbol)
        score_record = self.store.latest_topdown_score(symbol) if self.store.available else None
        score = (score_record or {}).get("snapshot") or {}
        hotspot_record = self.store.latest_hotspot_snapshot(symbol) if self.store.available else None
        hotspot = (hotspot_record or {}).get("snapshot") or {}
        plans = self.store.list_plans(symbol=symbol) if self.store.available else []
        self.stock_detail_texts["quant"].setPlainText(json.dumps({
            "股票": symbol,
            "最新冻结四层评分": score or "数据不完整，禁止授权",
            "说明": "盘中评分只使用已收盘15分钟快照；AI不参与分数。",
        }, ensure_ascii=False, indent=2))
        self.stock_detail_texts["hotspot"].setPlainText(json.dumps(
            hotspot or {"状态": "尚无冻结热点快照", "交易影响": "题材数据不完整，禁止授权"},
            ensure_ascii=False, indent=2,
        ))
        self.stock_detail_texts["plan"].setPlainText(json.dumps(
            plans or {"状态": "当前没有交易计划"}, ensure_ascii=False, indent=2, default=str,
        ))
        for key in ("company", "finance", "risk"):
            self.stock_detail_texts[key].setPlainText("正在从东方财富加载完整公司资料…")
        if self._stock_profile_thread is not None and self._stock_profile_thread.isRunning():
            self._stock_profile_thread.requestInterruption()
            self._stock_profile_thread.quit()
            self._stock_profile_thread.wait(1000)
        thread = QThread(self)
        worker = _StockProfileWorker(symbol)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._apply_stock_profile)
        worker.failed.connect(self._show_stock_profile_error)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._stock_profile_thread = thread
        self._stock_profile_worker = worker
        thread.start()

    def _apply_stock_profile(self, symbol: str, profile: object) -> None:
        data = profile if isinstance(profile, dict) else {}
        company = {
            "股票": symbol,
            "公司概况": data.get("company_survey"),
            "主营业务": data.get("business_analysis"),
            "经营必读": data.get("operations_required"),
            "管理层": data.get("company_management"),
            "股东研究": data.get("shareholder_research"),
            "来源": "东方财富F10/数据中心",
        }
        finance = {
            "估值字段": data.get("valuation_fields"),
            "估值摘要": data.get("valuation_summary"),
            "历史估值": data.get("valuation"),
            "主要财务指标": data.get("finance_main"),
            "股本结构": data.get("capital_structure"),
            "来源": "东方财富F10/数据中心",
        }
        risk = {
            "重大事项": data.get("company_big_news"),
            "公告": data.get("announcements"),
            "财经新闻": data.get("news"),
            "研究报告": data.get("research_reports"),
            "说明": "这些信息用于研究展示；只有结构化且验证后的官方重大负面事件可触发硬阻断。",
        }
        self.stock_detail_texts["company"].setPlainText(
            json.dumps(company, ensure_ascii=False, indent=2, default=str)
        )
        self.stock_detail_texts["finance"].setPlainText(
            json.dumps(finance, ensure_ascii=False, indent=2, default=str)
        )
        self.stock_detail_texts["risk"].setPlainText(
            json.dumps(risk, ensure_ascii=False, indent=2, default=str)
        )

    def _show_stock_profile_error(self, symbol: str, error: str) -> None:
        message = (
            f"{symbol} 公司资料加载失败：{error}\n"
            "量化评分和交易授权不使用未加载的公司资料；请检查数据源后重试。"
        )
        for key in ("company", "finance", "risk"):
            self.stock_detail_texts[key].setPlainText(message)

    def _refresh_hotspots(self) -> None:
        if self._quant_runtime is not None:
            self._quant_runtime.refresh_hotspots()
            return
        service = getattr(self.ctx, "hotspot_service", None)
        if service is None or not self.store.available:
            return
        if self._hotspot_thread is not None and self._hotspot_thread.isRunning():
            return
        symbols = {
            item["symbol"] for item in self.store.list_plans(lifecycle_open=True)
            if item.get("symbol")
        }
        symbols.update(
            item["symbol"] for item in self.store.list_quant_signals(limit=1000)
            if item.get("status") == "allow" and item.get("symbol")
        )
        if self._broker_snapshot:
            symbols.update(item.symbol for item in self._broker_snapshot.positions)
        if not symbols:
            return
        thread = QThread(self)
        worker = _HotspotBatchWorker(service, sorted(symbols))
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.snapshot_ready.connect(self._store_hotspot_snapshot)
        worker.failed.connect(self._hotspot_failed)
        worker.finished.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self.refresh)
        self._hotspot_thread = thread
        self._hotspot_worker = worker
        thread.start()

    def _refresh_topdown_scores(self) -> None:
        if self._quant_runtime is not None:
            self._quant_runtime.refresh_topdown_scores()
            return
        service = getattr(self.ctx, "topdown_market_data_service", None)
        if service is None or not self.store.available or self._broker_snapshot is None:
            return
        if self._topdown_thread is not None and self._topdown_thread.isRunning():
            return
        now = datetime.now().astimezone()
        if now.weekday() >= 5 or not (
            (9, 30) <= (now.hour, now.minute) <= (11, 30)
            or (13, 0) <= (now.hour, now.minute) <= (15, 0)
        ):
            return
        slot = now.replace(
            minute=now.minute - now.minute % 15, second=0, microsecond=0
        ).isoformat()
        if slot == self._last_topdown_slot:
            return
        if now.minute % 15 > 4:
            return
        universes = self.store.list_universe_snapshots(limit=1)
        if not universes or not universes[0].get("data_complete"):
            return
        universe = universes[0]["snapshot"]
        self._last_topdown_slot = slot
        self._capture_market_sentiment(universe, now)

    def _capture_market_sentiment(self, universe: dict, now: datetime) -> None:
        service = getattr(self.ctx, "market_sentiment_service", None)
        if service is None:
            self._build_topdown_jobs(universe, now, None)
            return
        if self._sentiment_thread is not None and self._sentiment_thread.isRunning():
            return
        signals = self.store.list_quant_signals(limit=1000)
        breadth = next((
            float((item.get("decision") or {}).get("condition_snapshot", {}).get(
                "market_breadth_pct"
            ))
            for item in signals
            if (item.get("decision") or {}).get("condition_snapshot", {}).get(
                "market_breadth_pct"
            ) is not None
        ), None)
        thread = QThread(self)
        worker = _MarketSentimentWorker(service, self.store, breadth, now)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(
            lambda snapshot: self._sentiment_captured(universe, now, snapshot)
        )
        worker.failed.connect(
            lambda error: self._sentiment_failed(universe, now, error)
        )
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._sentiment_thread = thread
        self._sentiment_worker = worker
        thread.start()

    def _sentiment_captured(self, universe: dict, now: datetime, snapshot) -> None:
        self.store.add_market_sentiment_snapshot(snapshot)
        sentiment = snapshot.input if snapshot.data_complete else None
        self._build_topdown_jobs(universe, now, sentiment)

    def _sentiment_failed(self, universe: dict, now: datetime, error: str) -> None:
        self.ctx.logger.warning("市场情绪冻结失败: %s", error)
        self._build_topdown_jobs(universe, now, None)

    def _build_topdown_jobs(self, universe: dict, now: datetime, sentiment) -> None:
        service = getattr(self.ctx, "topdown_market_data_service", None)
        signals = self.store.list_quant_signals(limit=1000)
        latest_by_symbol: dict[str, dict] = {}
        for record in signals:
            if record.get("status") != "allow":
                continue
            latest_by_symbol.setdefault(record["symbol"], record)
        jobs = []
        for symbol in universe.get("symbols") or []:
            record = latest_by_symbol.get(symbol)
            if record is None:
                continue
            try:
                signal = SignalDecision.model_validate(record["decision"])
            except Exception:  # noqa: BLE001
                continue
            hotspot_record = self.store.latest_hotspot_snapshot(symbol)
            hotspot = None
            if hotspot_record:
                from pa_agent.trading.topdown import HotspotSnapshot

                hotspot = HotspotSnapshot.model_validate(hotspot_record["snapshot"])
            theme_metrics = (
                self.ctx.hotspot_service.theme_metrics(hotspot)
                if hotspot is not None and getattr(self.ctx, "hotspot_service", None)
                else None
            )
            previous_record = self.store.latest_topdown_score(symbol)
            previous = (
                TopDownScoreSnapshot.model_validate(previous_record["snapshot"])
                if previous_record else None
            )
            jobs.append({
                "symbol": symbol,
                "daily_signal": signal,
                "pool_snapshot": universe,
                "broker": self._broker_snapshot,
                "hotspot": hotspot,
                "previous_score": previous,
                "sentiment": sentiment,
                "theme_metrics": theme_metrics,
                "captured_at": now,
                "authorization_open": any(
                    item.get("symbol") == symbol and item.get("status")
                    in {"awaiting_user_confirmation", "submitted", "partially_filled"}
                    for item in self.store.list_plans(symbol=symbol)
                ),
            })
        if not jobs:
            return
        thread = QThread(self)
        worker = _TopDownBatchWorker(service, jobs)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.score_ready.connect(self._store_topdown_score)
        worker.failed.connect(self._topdown_failed)
        worker.finished.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self.refresh)
        self._topdown_thread = thread
        self._topdown_worker = worker
        thread.start()

    def _store_topdown_score(self, score, closed_stock_bar=None) -> None:
        # Process plans that already existed before this scoring bar.  A plan
        # created by the score below must wait for a later closed bar, which
        # prevents same-bar look-ahead entry fills.
        if closed_stock_bar is not None:
            self.ctx.trade_lifecycle.process_closed_bar(
                symbol=score.symbol,
                timeframe="15m",
                bar=closed_stock_bar,
            )
        self.store.add_topdown_score(score)
        if score.eligible_for_risk and not score.hard_blocks and not score.data_gaps:
            signals = self.store.list_quant_signals(
                strategy_id=self.ctx.settings.strategy.strategy_id,
                limit=1000,
            )
            record = next((
                item for item in signals
                if item.get("symbol") == score.symbol
                and item.get("pool_version") == score.pool_version
                and item.get("status") == "allow"
            ), None)
            if record is not None:
                daily = SignalDecision.model_validate(record["decision"])
                self.ctx.quant_workflow.create_topdown_plan(daily, score)
        if score.status.value != "authorization_revoked":
            return
        for plan in self.store.list_plans(symbol=score.symbol):
            if plan.get("status") not in {
                "awaiting_user_confirmation", "submitted", "partially_filled",
            }:
                continue
            self.store.update_plan(plan["id"], status="invalidated")
            self.store.append_event(
                plan["id"], "topdown_authorization_revoked",
                details={
                    "score": score.total_score,
                    "hard_blocks": score.hard_blocks,
                    "bar_closed_at": score.bar_closed_at,
                },
            )

    def _topdown_failed(self, symbol: str, error: str) -> None:
        self.ctx.logger.warning("15分钟四层评分采集失败 %s: %s", symbol, error)

    def _store_hotspot_snapshot(self, snapshot) -> None:
        self.store.add_hotspot_snapshot(snapshot)
        if not snapshot.negative_blocks:
            return
        for plan in self.store.list_plans(symbol=snapshot.symbol):
            if plan.get("status") not in {"proposed", "triggered"}:
                continue
            self.store.update_plan(plan["id"], status="invalidated")
            self.store.append_event(
                plan["id"],
                "major_negative_invalidated",
                details={
                    "negative_blocks": snapshot.negative_blocks,
                    "hotspot_source_hash": snapshot.source_hash,
                    "frozen_at": snapshot.frozen_at,
                },
            )

    def _hotspot_failed(self, symbol: str, error: str) -> None:
        self.ctx.logger.warning("热点刷新失败 %s: %s", symbol, error)

    def _sync_daily_lifecycle(self) -> None:
        if self._quant_runtime is not None:
            self._quant_runtime.sync_daily_lifecycle()
            return
        lifecycle = getattr(self.ctx, "trade_lifecycle", None)
        if not self.store.available or lifecycle is None:
            return
        if (
            self._lifecycle_sync_thread is not None
            and self._lifecycle_sync_thread.isRunning()
        ):
            return
        from pa_agent.trading.lifecycle_sync import LifecycleMarketDataSync

        service = LifecycleMarketDataSync(self.store, lifecycle)
        thread = QThread(self)
        worker = _LifecycleDailySyncWorker(service)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._daily_lifecycle_synced)
        worker.failed.connect(self._daily_lifecycle_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._lifecycle_sync_thread = thread
        self._lifecycle_sync_worker = worker
        thread.start()

    def _daily_lifecycle_synced(self, result: object) -> None:
        self._last_lifecycle_sync = result if isinstance(result, dict) else {}
        failures = self._last_lifecycle_sync.get("failures") or {}
        if failures:
            self.ctx.logger.warning("开放计划日线同步部分失败: %s", failures)
        self.refresh()

    def _daily_lifecycle_failed(self, error: str) -> None:
        self._last_lifecycle_sync = {
            "captured_at": datetime.now().astimezone().isoformat(),
            "error": error,
        }
        self.ctx.logger.warning("开放计划日线同步失败: %s", error)

    def shutdown(self) -> None:
        """Stop page-owned transient work without touching the quant runtime."""
        self._reconciliation_timer.stop()
        if self._reconciliation_order is not None and self._quant_runtime is not None:
            self._quant_runtime.end_reconciliation(self._reconciliation_order.plan_id)
        self._reconciliation_order = None
        for thread in (
            self._stock_profile_thread,
            self._oos_backtest_thread,
        ):
            if thread is not None and thread.isRunning():
                thread.requestInterruption()
                thread.quit()
                thread.wait(1500)

    def closeEvent(self, event) -> None:  # noqa: N802
        self.shutdown()
        super().closeEvent(event)

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
        filename, _ = QFileDialog.getSaveFileName(
            self, "导出 CSV", f"trade_{dataset}.csv", "CSV (*.csv)"
        )
        if filename:
            filters = {key: edit.text().strip() for key, edit in self.stats_filters.items()}
            self.store.export_csv(Path(filename), dataset=dataset, **filters)

    def _save_risk(self) -> None:
        risk = self.ctx.settings.risk
        risk.account_equity = self.equity.value() or None
        risk.available_cash = self.cash.value() or None
        risk.per_trade_risk_pct = self.per_trade.value()
        risk.max_open_risk_pct = self.max_open.value()
        self.ctx.settings.portfolio_risk.initial_per_trade_risk_pct = (
            self.initial_per_trade.value()
        )
        self.ctx.settings.portfolio_risk.initial_max_open_risk_pct = (
            self.initial_max_open.value()
        )
        risk.daily_loss_warning_pct = self.daily.value()
        risk.weekly_loss_warning_pct = self.weekly.value()
        strategy_state = self.store.current_strategy_state(TOPDOWN_STRATEGY_ID)
        self.ctx.settings.portfolio_risk.live_trading_enabled = (
            self.live_enabled.isEnabled()
            and self.live_enabled.isChecked()
            and strategy_state
            in {StrategyState.ACTIVE.value, StrategyState.REDUCED.value}
        )
        save_settings(self.ctx.settings, SETTINGS_JSON_PATH)
        self.ctx.trading_service.risk_settings = risk
        self.ctx.portfolio_risk.risk_settings = risk
        self.ctx.portfolio_risk.settings = self.ctx.settings.portfolio_risk
        QMessageBox.information(self, "已保存", "风险配置已保存在本地 settings.json。")

    def _edit_profile(self) -> None:
        symbol = self.profile_symbol.text().strip().upper()
        if not symbol:
            return
        profile = self.store.get_profile(symbol)
        if profile is None:
            profile = default_profile(
                symbol,
                getattr(self.ctx.settings.general, "last_data_source", ""),
                getattr(self.ctx.settings.general, "kline_adjust", ""),
            )
        dialog = InstrumentProfileDialog(profile, self)
        if dialog.exec():
            self.store.upsert_profile(dialog.value())
            self.refresh()

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
    spin = QDoubleSpinBox()
    spin.setRange(0, maximum)
    spin.setDecimals(4)
    spin.setValue(float(value))
    return spin


def _score_text(value, maximum: int) -> str:
    return f"—/{maximum}" if value is None else f"{float(value):.1f}/{maximum}"


def _score_name(key: str) -> str:
    return {"index": "指数", "sentiment": "情绪", "theme": "题材", "stock": "个股"}[key]


def _strategy_label(value: str) -> str:
    if value == TOPDOWN_STRATEGY_ID:
        return "4:3:2:1四层策略"
    if value in {"hs300_daily_pullback_v1", "cloud_ai_daily_pullback_v1"}:
        return "日线回调基线"
    if value == "manual_exception_4321_v1":
        return "池外例外（半风险）"
    return "AI研究/遗留"


def _money(value) -> str:
    return "—" if value is None else f"¥{float(value):,.2f}"


def _signed_money(value) -> str:
    return "—" if value is None else f"{float(value):+,.2f}"


def _large_money(value) -> str:
    if value is None:
        return "—"
    amount = float(value)
    return f"{amount / 100_000_000:.2f}亿" if abs(amount) >= 100_000_000 else f"{amount:,.0f}"


def _pct_value(value) -> str:
    return "—" if value is None else f"{float(value):+.2f}%"

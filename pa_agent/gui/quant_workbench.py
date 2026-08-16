"""Stock-pool-driven, single-window deterministic quant workbench."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from PyQt6.QtCore import QObject, QPointF, QRectF, QSize, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pa_agent.trading.topdown import (
    MANUAL_EXCEPTION_STRATEGY_ID,
    TOPDOWN_STRATEGY_ID,
    TopDownScoreSnapshot,
)
from pa_agent.trading.stock_selection import STRATEGY_LABELS
from pa_agent.trading.workbench_models import (
    PoolRowViewModel,
    QuantWorkbenchViewModel,
    SelectedStockContext,
    SelectedStockContextController,
)

_PANEL = "#111820"
_SURFACE = "#161f29"
_BORDER = "#293643"
_TEXT = "#e8edf2"
_MUTED = "#93a1af"
_ACCENT = "#36a3d9"
_GREEN = "#4eb783"
_AMBER = "#d9a441"
_RED = "#e06464"


class _WatchlistWorker(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, ctx: Any, value: str) -> None:
        super().__init__()
        self.ctx = ctx
        self.value = value

    def run(self) -> None:
        try:
            service = getattr(self.ctx, "universe_service", None)
            if service is None or not hasattr(service, "validate_watchlist_member"):
                raise RuntimeError("A股准入校验服务不可用，未修改我的监控池")
            member = service.validate_watchlist_member(self.value)
            payload = member.model_dump(mode="json")
            row = self.ctx.trade_store.upsert_watchlist_member(
                symbol=member.symbol,
                name=member.name,
                source="user_watchlist",
                metadata=payload,
            )
            self.finished.emit(row)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class MiniKlineChart(QWidget):
    """Small native candle chart for persisted daily/15-minute facts."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._bars: list[dict[str, Any]] = []
        self.setMinimumHeight(205)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setToolTip("只绘制已保存的真实K线；不补造缺失行情")

    def set_bars(self, bars: list[dict[str, Any]]) -> None:
        self._bars = list(bars[-72:])
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("#0e151c"))
        bounds = self.rect().adjusted(12, 12, -12, -22)
        painter.setPen(QPen(QColor("#23303b"), 1))
        for index in range(1, 4):
            y = bounds.top() + bounds.height() * index / 4
            painter.drawLine(bounds.left(), int(y), bounds.right(), int(y))
        bars = [bar for bar in self._bars if _valid_bar(bar)]
        if not bars:
            painter.setPen(QColor(_MUTED))
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                "尚无可回溯K线\n系统不会用模拟行情填充",
            )
            return
        lows = [float(bar["low"]) for bar in bars]
        highs = [float(bar["high"]) for bar in bars]
        low, high = min(lows), max(highs)
        span = max(high - low, max(abs(high), 1.0) * 0.002)
        step = bounds.width() / max(len(bars), 1)
        body_width = max(2.0, min(8.0, step * 0.62))

        def y_of(value: float) -> float:
            return bounds.bottom() - (value - low) / span * bounds.height()

        for index, bar in enumerate(bars):
            open_ = float(bar["open"])
            close = float(bar["close"])
            x = bounds.left() + step * (index + 0.5)
            color = QColor("#d65f5f" if close >= open_ else "#49ad7b")
            painter.setPen(QPen(color, 1))
            painter.drawLine(
                QPointF(x, y_of(float(bar["high"]))),
                QPointF(x, y_of(float(bar["low"]))),
            )
            top = min(y_of(open_), y_of(close))
            height = max(1.5, abs(y_of(open_) - y_of(close)))
            rect = QRectF(x - body_width / 2, top, body_width, height)
            if close >= open_:
                painter.fillRect(rect, color)
            else:
                painter.drawRect(rect)
        painter.setPen(QColor(_MUTED))
        painter.setFont(QFont("Cascadia Mono", 8))
        painter.drawText(
            12,
            self.height() - 6,
            f"{len(bars)}根已冻结K线  {low:.2f} — {high:.2f}",
        )


class _CollapsiblePanel(QFrame):
    """Let the workbench collapse a data-dense panel on narrow windows."""

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        hint = super().minimumSizeHint()
        return QSize(0, hint.height())


class QuantWorkbenchPage(QWidget):
    """Single-window UI centered on a shared selected-stock context."""

    return_to_analysis_requested = pyqtSignal()
    selected_symbol_changed = pyqtSignal(str)

    def __init__(self, ctx: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.store = ctx.trade_store
        self.controller = SelectedStockContextController(ctx, self)
        self._view_model: QuantWorkbenchViewModel | None = None
        self._selected: SelectedStockContext | None = None
        self._pool_rows: list[PoolRowViewModel] = []
        self._watch_thread: QThread | None = None
        self._watch_worker: _WatchlistWorker | None = None
        self._last_runtime_detail = ""
        self._ths_scan_running = False
        self._right_forced_open = False
        self._reconciliation_timer = QTimer(self)
        self._reconciliation_timer.setInterval(2_000)
        self._reconciliation_timer.timeout.connect(self._poll_reconciliation)
        self._reconciliation_order: Any | None = None
        self._reconciliation_attempts = 0
        self._reconciliation_matched = False
        self.setObjectName("stockPoolQuantWorkbench")
        self.setStyleSheet(_stylesheet())
        self._build_ui()
        self.controller.view_model_changed.connect(self._apply_view_model)
        self.controller.context_error.connect(self._show_context_error)
        self.controller.symbol_changed.connect(self.selected_symbol_changed.emit)
        runtime = getattr(ctx, "quant_runtime", None)
        if runtime is not None:
            if hasattr(runtime, "facts_updated"):
                runtime.facts_updated.connect(self._on_facts_updated)
            else:
                runtime.updated.connect(self.refresh)
            runtime.status_changed.connect(self._runtime_status)
            runtime.task_failed.connect(self._runtime_status)
        self._clock = QTimer(self)
        self._clock.setInterval(30_000)
        self._clock.timeout.connect(self.refresh)
        self._clock.start()
        self.refresh()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(8)
        root.addWidget(self._build_navigation())
        root.addWidget(self._build_health_strip())
        self.workspace_stack = QStackedWidget()
        self.workspace_stack.setObjectName("quantWorkspaceStack")
        self.workspace_stack.setAccessibleName("量化交易管理导航")
        # Compatibility for callers that previously addressed the nine-tab
        # container through ``tabs``.  The public hierarchy remains an
        # workspaces, but remains an embedded current-window widget.
        self.tabs = self.workspace_stack
        self.monitor_workspace = self._build_monitor_workspace()
        self.account_workspace = self._build_account_workspace()
        self.selection_workspace = self._build_selection_workspace()
        self.validation_workspace = self._build_validation_workspace()
        for page in (
            self.monitor_workspace,
            self.account_workspace,
            self.selection_workspace,
            self.validation_workspace,
        ):
            self.workspace_stack.addWidget(page)
        root.addWidget(self.workspace_stack, 1)

    def _build_navigation(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("workbenchNavigation")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(14, 8, 10, 8)
        layout.setSpacing(7)
        brand = QLabel("PA / QUANT")
        brand.setObjectName("quantBrand")
        layout.addWidget(brand)
        title = QLabel("私人A股量化工作台")
        title.setObjectName("quantTitle")
        layout.addWidget(title)
        layout.addSpacing(20)
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons: list[QPushButton] = []
        for index, label in enumerate(("实时监控", "交易账户", "智能选股", "系统验证")):
            button = QPushButton(label)
            button.setCheckable(True)
            button.setObjectName("workspaceNavButton")
            button.clicked.connect(lambda _checked=False, i=index: self.navigate(i))
            self.nav_group.addButton(button, index)
            self.nav_buttons.append(button)
            layout.addWidget(button)
        self.nav_buttons[0].setChecked(True)
        layout.addStretch(1)
        research = QPushButton("研究分析  ↗")
        # Preserve the legacy automation id while presenting the clearer
        # research-only label.
        research.setObjectName("returnToAnalysisButton")
        research.setToolTip("打开独立K线与Codex研究页；研究结论不进入量化授权")
        research.clicked.connect(self.return_to_analysis_requested.emit)
        layout.addWidget(research)
        return bar

    def _build_health_strip(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("globalHealthStrip")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(12, 7, 12, 7)
        layout.setSpacing(9)
        self.health_labels: dict[str, QLabel] = {}
        for key in ("market", "data", "pool", "strategy", "broker", "feishu", "sync"):
            label = QLabel("检查中")
            label.setObjectName("healthItem")
            label.setMinimumWidth(0)
            label.setSizePolicy(
                QSizePolicy.Policy.Ignored,
                QSizePolicy.Policy.Preferred,
            )
            self.health_labels[key] = label
            layout.addWidget(label, 1)
        layout.addStretch(1)
        self.health_issue_button = QPushButton("查看状态")
        self.health_issue_button.setObjectName("healthIssueButton")
        self.health_issue_button.clicked.connect(lambda: self.navigate("validation"))
        layout.addWidget(self.health_issue_button)
        refresh = QPushButton("立即同步")
        refresh.setObjectName("quantRefreshButton")
        refresh.setToolTip("发起账户、股票池、候选、热点和评分的完整刷新")
        refresh.clicked.connect(self._refresh_all_now)
        layout.addWidget(refresh)
        return frame

    def _build_monitor_workspace(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        compact_actions = QHBoxLayout()
        compact_actions.addStretch(1)
        self.compact_plan_button = QPushButton("查看交易计划  →")
        self.compact_plan_button.setObjectName("compactPlanButton")
        self.compact_plan_button.setToolTip("在当前窗口内切换股票沙箱与交易计划")
        self.compact_plan_button.clicked.connect(self._toggle_compact_plan)
        self.compact_plan_button.hide()
        compact_actions.addWidget(self.compact_plan_button)
        layout.addLayout(compact_actions)
        self.monitor_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.monitor_splitter.setObjectName("monitorThreeColumnSplitter")
        self.pool_panel = self._build_pool_panel()
        self.stock_panel = self._build_stock_panel()
        self.plan_panel = self._build_plan_panel()
        for panel in (self.pool_panel, self.stock_panel, self.plan_panel):
            panel.setMinimumWidth(0)
            panel.setSizePolicy(
                QSizePolicy.Policy.Ignored,
                QSizePolicy.Policy.Expanding,
            )
        self.monitor_splitter.addWidget(self.pool_panel)
        self.monitor_splitter.addWidget(self.stock_panel)
        self.monitor_splitter.addWidget(self.plan_panel)
        self.monitor_splitter.setStretchFactor(0, 25)
        self.monitor_splitter.setStretchFactor(1, 45)
        self.monitor_splitter.setStretchFactor(2, 30)
        sizes = (
            self.store.get_workbench_preference("monitor_splitter_sizes", [280, 520, 340])
            if self.store.available
            else [280, 520, 340]
        )
        if isinstance(sizes, list) and len(sizes) == 3:
            self.monitor_splitter.setSizes([int(item) for item in sizes])
        self.monitor_splitter.splitterMoved.connect(self._save_splitter_sizes)
        layout.addWidget(self.monitor_splitter)
        return page

    def _build_pool_panel(self) -> QWidget:
        panel, layout = _panel("股票池 / 行动队列")
        controls = QHBoxLayout()
        self.pool_view_combo = QComboBox()
        self.pool_view_combo.addItem("系统策略池", "system")
        self.pool_view_combo.addItem("我的监控池", "watchlist")
        self.pool_view_combo.addItem("同花顺自选", "ths_watchlist")
        saved_view = (
            self.store.get_workbench_preference("pool_view", "system")
            if self.store.available
            else "system"
        )
        index = self.pool_view_combo.findData(saved_view)
        self.pool_view_combo.setCurrentIndex(max(index, 0))
        self.pool_view_combo.currentIndexChanged.connect(self._pool_filter_changed)
        controls.addWidget(self.pool_view_combo, 1)
        self.pool_status_combo = QComboBox()
        for label, value in (
            ("全部状态", "all"),
            ("候选", "candidate"),
            ("可交易", "tradeable"),
            ("持仓", "position"),
            ("退出", "exit"),
            ("风险", "risk"),
        ):
            self.pool_status_combo.addItem(label, value)
        self.pool_status_combo.currentIndexChanged.connect(self._pool_filter_changed)
        controls.addWidget(self.pool_status_combo, 1)
        layout.addLayout(controls)
        self.pool_search = QLineEdit()
        self.pool_search.setClearButtonEnabled(True)
        self.pool_search.setPlaceholderText("搜索A股代码或名称")
        self.pool_search.textChanged.connect(self._refresh_pool_tree)
        layout.addWidget(self.pool_search)
        self.pool_summary = QLabel("正在加载股票池")
        self.pool_summary.setObjectName("sectionHint")
        layout.addWidget(self.pool_summary)
        ths_row = QHBoxLayout()
        self.ths_watchlist_status = QLabel("同花顺自选尚未同步")
        self.ths_watchlist_status.setObjectName("sectionHint")
        self.ths_watchlist_status.setWordWrap(True)
        ths_row.addWidget(self.ths_watchlist_status, 1)
        self.ths_watchlist_scan_button = QPushButton("同步并扫描")
        self.ths_watchlist_scan_button.setObjectName("tertiaryButton")
        self.ths_watchlist_scan_button.setToolTip(
            "只读导入同花顺全部自选分类中的沪深A股，并运行确定性日线策略"
        )
        self.ths_watchlist_scan_button.clicked.connect(self._scan_ths_watchlist)
        ths_row.addWidget(self.ths_watchlist_scan_button)
        layout.addLayout(ths_row)
        self.pool_tree = QTreeWidget()
        self.pool_tree.setObjectName("stockPoolActionTree")
        self.pool_tree.setHeaderLabels(["股票", "现价", "状态", "评分"])
        self.pool_tree.setRootIsDecorated(False)
        self.pool_tree.setAlternatingRowColors(True)
        self.pool_tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.pool_tree.setUniformRowHeights(True)
        header = self.pool_tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.pool_tree.itemSelectionChanged.connect(self._pool_selection_changed)
        layout.addWidget(self.pool_tree, 1)
        action_row = QHBoxLayout()
        self.watch_input = QLineEdit()
        self.watch_input.setPlaceholderText("6位代码或完整名称")
        self.watch_input.returnPressed.connect(self._add_watchlist)
        action_row.addWidget(self.watch_input, 1)
        self.watch_add_button = QPushButton("加入关注")
        self.watch_add_button.setObjectName("primaryButton")
        self.watch_add_button.clicked.connect(self._add_watchlist)
        action_row.addWidget(self.watch_add_button)
        layout.addLayout(action_row)
        self.watch_remove_button = QPushButton("从我的监控池移除")
        self.watch_remove_button.setObjectName("tertiaryButton")
        self.watch_remove_button.clicked.connect(self._remove_watchlist)
        self.watch_remove_button.hide()
        layout.addWidget(self.watch_remove_button)
        self.watch_feedback = QLabel("")
        self.watch_feedback.setObjectName("inlineFeedback")
        self.watch_feedback.setWordWrap(True)
        self.watch_feedback.hide()
        layout.addWidget(self.watch_feedback)
        return panel

    def _build_stock_panel(self) -> QWidget:
        panel, layout = _panel("当前股票 / 独立沙箱")
        heading = QHBoxLayout()
        stock_box = QVBoxLayout()
        self.stock_title = QLabel("请选择股票")
        self.stock_title.setObjectName("selectedStockTitle")
        self.stock_meta = QLabel("股票池中的每只股票拥有独立状态")
        self.stock_meta.setObjectName("sectionHint")
        stock_box.addWidget(self.stock_title)
        stock_box.addWidget(self.stock_meta)
        heading.addLayout(stock_box, 1)
        self.stock_price = QLabel("—")
        self.stock_price.setObjectName("selectedStockPrice")
        heading.addWidget(self.stock_price)
        layout.addLayout(heading)
        tf_row = QHBoxLayout()
        self.tf_group = QButtonGroup(self)
        for label, key in (("15分钟", "15m"), ("日线", "1d")):
            button = QPushButton(label)
            button.setCheckable(True)
            button.setProperty("timeframe", key)
            button.setObjectName("timeframeButton")
            button.clicked.connect(self._switch_chart_timeframe)
            self.tf_group.addButton(button)
            tf_row.addWidget(button)
            if key == "15m":
                button.setChecked(True)
        tf_row.addStretch(1)
        self.chart_fact_time = QLabel("行情时间 —")
        self.chart_fact_time.setObjectName("sectionHint")
        tf_row.addWidget(self.chart_fact_time)
        layout.addLayout(tf_row)
        self.kline_chart = MiniKlineChart()
        layout.addWidget(self.kline_chart, 1)
        self.lifecycle = QLabel("加入监控 → 日线观察 → 15分钟评分 → 风控 → 同花顺 → 持仓 → 退出")
        self.lifecycle.setObjectName("lifecycleStrip")
        self.lifecycle.setWordWrap(True)
        layout.addWidget(self.lifecycle)
        scores = QHBoxLayout()
        self.score_cards: dict[str, QLabel] = {}
        for key, label, maximum, stretch in (
            ("index", "指数", 40, 4),
            ("sentiment", "情绪", 30, 3),
            ("theme", "题材", 20, 2),
            ("stock", "个股", 10, 1),
        ):
            card = QLabel(f"{label}\n—/{maximum}")
            card.setAlignment(Qt.AlignmentFlag.AlignCenter)
            card.setObjectName("scoreCard")
            card.setMinimumHeight(54)
            self.score_cards[key] = card
            scores.addWidget(card, stretch)
        layout.addLayout(scores)
        self.score_summary = QLabel("综合评分不可用 · 等待完整数据")
        self.score_summary.setObjectName("scoreSummary")
        layout.addWidget(self.score_summary)
        details_tabs = QTabWidget()
        details_tabs.setDocumentMode(True)
        self.score_details = QTextBrowser()
        self.hotspot_details = QTextBrowser()
        details_tabs.addTab(self.score_details, "评分依据")
        details_tabs.addTab(self.hotspot_details, "热点与公告")
        details_tabs.setMaximumHeight(190)
        layout.addWidget(details_tabs)
        return panel

    def _build_plan_panel(self) -> QWidget:
        panel, layout = _panel("交易计划 / 下一步")
        self.stage_badge = QLabel("等待选择股票")
        self.stage_badge.setObjectName("stageBadge")
        layout.addWidget(self.stage_badge)
        self.stage_explanation = QLabel("从左侧股票池选择一只股票，查看它的确定性交易状态。")
        self.stage_explanation.setWordWrap(True)
        self.stage_explanation.setObjectName("stageExplanation")
        layout.addWidget(self.stage_explanation)
        self.next_condition = QLabel("下一步条件将在选择股票后显示")
        self.next_condition.setWordWrap(True)
        self.next_condition.setObjectName("nextCondition")
        layout.addWidget(self.next_condition)
        self.plan_details = QTextBrowser()
        self.plan_details.setObjectName("planDetails")
        layout.addWidget(self.plan_details, 2)
        self.risk_details = QTextBrowser()
        self.risk_details.setObjectName("riskDetails")
        layout.addWidget(self.risk_details, 1)
        self.plan_timeline = QTextBrowser()
        self.plan_timeline.setObjectName("planTimeline")
        layout.addWidget(self.plan_timeline, 1)
        self.primary_action = QPushButton("")
        self.primary_action.setObjectName("primaryActionButton")
        self.primary_action.clicked.connect(self._run_primary_action)
        self.primary_action.hide()
        self._primary_action_name = ""
        layout.addWidget(self.primary_action)
        self.plan_status_feedback = QLabel("")
        self.plan_status_feedback.setObjectName("inlineFeedback")
        self.plan_status_feedback.setWordWrap(True)
        self.plan_status_feedback.hide()
        layout.addWidget(self.plan_status_feedback)
        return panel

    def _build_account_workspace(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self.account_banner = QLabel("同花顺账户事实正在加载")
        self.account_banner.setObjectName("workspaceBanner")
        self.account_banner.setWordWrap(True)
        layout.addWidget(self.account_banner)
        metric_row = QHBoxLayout()
        self.account_metrics: dict[str, QLabel] = {}
        for key, title in (
            ("equity", "总资产"),
            ("cash", "可用资金"),
            ("position", "持仓市值"),
            ("pnl", "当日盈亏"),
        ):
            card = QLabel(f"{title}\n—")
            card.setObjectName("accountMetric")
            metric_row.addWidget(card, 1)
            self.account_metrics[key] = card
        layout.addLayout(metric_row)
        sync_row = QHBoxLayout()
        sync_row.addStretch(1)
        self.broker_sync_button = QPushButton("立即只读同步")
        self.broker_sync_button.setObjectName("primaryButton")
        self.broker_sync_button.clicked.connect(self._sync_broker)
        sync_row.addWidget(self.broker_sync_button)
        layout.addLayout(sync_row)
        tabs = QTabWidget()
        self.positions_table = _table(["代码", "名称", "数量", "可卖", "成本", "现价", "市值"])
        self.orders_table = _table(
            ["委托号", "代码", "方向", "价格", "数量", "已成", "状态", "时间"]
        )
        self.fills_table = _table(["成交号", "代码", "方向", "价格", "数量", "费用", "时间"])
        self.reconcile_table = _table(["计划", "委托号", "状态", "更新时间"])
        self.monthly_text = QTextBrowser()
        tabs.addTab(self.positions_table, "持仓与退出")
        tabs.addTab(self.orders_table, "当日委托")
        tabs.addTab(self.fills_table, "当日成交")
        tabs.addTab(self.reconcile_table, "计划对账")
        tabs.addTab(self.monthly_text, "月度表现")
        for table in (self.positions_table, self.orders_table, self.fills_table):
            table.itemDoubleClicked.connect(self._account_symbol_selected)
        layout.addWidget(tabs, 1)
        return page

    def _build_selection_workspace(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self.selection_banner = QLabel(
            "智能选股只产生观察候选，不直接生成交易计划；重大负面公告为硬过滤条件。"
        )
        self.selection_banner.setObjectName("workspaceBanner")
        self.selection_banner.setWordWrap(True)
        layout.addWidget(self.selection_banner)

        summary_row = QHBoxLayout()
        self.selection_metrics: dict[str, QLabel] = {}
        for key, title in (
            ("scan", "扫描种子"),
            ("candidate", "入选股票"),
            ("negative", "负面新闻过滤"),
            ("time", "最近扫描"),
        ):
            card = QLabel(f"{title}\n—")
            card.setObjectName("accountMetric")
            summary_row.addWidget(card, 1)
            self.selection_metrics[key] = card
        self.selection_refresh_button = QPushButton("重新扫描")
        self.selection_refresh_button.setObjectName("primaryButton")
        self.selection_refresh_button.clicked.connect(self._refresh_stock_selection)
        summary_row.addWidget(self.selection_refresh_button)
        layout.addLayout(summary_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("selectionThreeColumnSplitter")

        filter_panel, filter_layout = _panel("选股策略")
        self.selection_filter_tree = QTreeWidget()
        self.selection_filter_tree.setHeaderHidden(True)
        self.selection_filter_tree.setRootIsDecorated(False)
        for label, value in (
            ("综合候选", "all"),
            ("近期热点题材", "hot_theme"),
            ("主力关注题材", "main_force_theme"),
            ("量能窒息", "volume_suffocation"),
            ("趋势启动", "trend_start"),
        ):
            item = QTreeWidgetItem([label])
            item.setData(0, Qt.ItemDataRole.UserRole, value)
            self.selection_filter_tree.addTopLevelItem(item)
        self.selection_filter_tree.setCurrentItem(self.selection_filter_tree.topLevelItem(0))
        self.selection_filter_tree.itemSelectionChanged.connect(self._render_selection_rows)
        filter_layout.addWidget(self.selection_filter_tree, 1)
        filter_note = QLabel(
            "所有通道均要求：仅A股、行情完整、公告时间可核验、没有官方确认的重大负面事件。"
        )
        filter_note.setObjectName("sectionHint")
        filter_note.setWordWrap(True)
        filter_layout.addWidget(filter_note)

        table_panel, table_layout = _panel("候选股票")
        self.selection_table = _table(
            ["代码", "名称", "现价", "涨跌", "入选策略", "题材", "评分"]
        )
        self.selection_table.itemSelectionChanged.connect(self._selection_row_changed)
        table_layout.addWidget(self.selection_table, 1)
        self.selection_empty = QLabel("尚无完成硬过滤的候选股票")
        self.selection_empty.setObjectName("inlineFeedback")
        self.selection_empty.setWordWrap(True)
        table_layout.addWidget(self.selection_empty)

        detail_panel, detail_layout = _panel("入选依据 / 下一步")
        self.selection_detail_title = QLabel("请选择一只候选股票")
        self.selection_detail_title.setObjectName("selectedStockTitle")
        detail_layout.addWidget(self.selection_detail_title)
        self.selection_detail = QTextBrowser()
        self.selection_detail.setObjectName("planDetails")
        detail_layout.addWidget(self.selection_detail, 1)
        self.selection_add_button = QPushButton("加入我的监控池")
        self.selection_add_button.setObjectName("primaryButton")
        self.selection_add_button.clicked.connect(self._add_selected_candidate)
        self.selection_add_button.hide()
        detail_layout.addWidget(self.selection_add_button)
        self.selection_monitor_button = QPushButton("在实时监控中查看")
        self.selection_monitor_button.setObjectName("tertiaryButton")
        self.selection_monitor_button.clicked.connect(self._open_selected_candidate)
        self.selection_monitor_button.hide()
        detail_layout.addWidget(self.selection_monitor_button)
        self.selection_feedback = QLabel("")
        self.selection_feedback.setObjectName("inlineFeedback")
        self.selection_feedback.setWordWrap(True)
        self.selection_feedback.hide()
        detail_layout.addWidget(self.selection_feedback)

        for panel, stretch in ((filter_panel, 18), (table_panel, 52), (detail_panel, 30)):
            panel.setMinimumWidth(0)
            splitter.addWidget(panel)
            splitter.setStretchFactor(splitter.count() - 1, stretch)
        splitter.setSizes([210, 620, 360])
        layout.addWidget(splitter, 1)
        return page

    def _build_validation_workspace(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self.validation_banner = QLabel("验证状态正在加载")
        self.validation_banner.setObjectName("workspaceBanner")
        self.validation_banner.setWordWrap(True)
        layout.addWidget(self.validation_banner)
        tabs = QTabWidget()
        self.validation_progress = QTextBrowser()
        self.validation_data = QTextBrowser()
        self.validation_pool_table = _table(
            ["排名", "代码", "名称", "行业/题材", "资格", "更新时间"]
        )
        self.notification_table = _table(["时间", "股票", "事件", "计划", "结果", "尝试"])
        tabs.addTab(self.validation_progress, "策略与晋级")
        tabs.addTab(self.validation_data, "数据与同花顺")
        tabs.addTab(self.validation_pool_table, "系统股票池版本")
        tabs.addTab(self.notification_table, "飞书与审计")
        layout.addWidget(tabs, 1)
        return page

    def navigate(self, index: int | str) -> None:
        mapping = {"monitor": 0, "account": 1, "selection": 2, "validation": 3}
        target = mapping.get(index, index) if isinstance(index, str) else index
        target = max(0, min(int(target), 3))
        self.workspace_stack.setCurrentIndex(target)
        self.nav_buttons[target].setChecked(True)
        if target == 0 and self._selected is not None:
            self._render_selected(self._selected)

    def refresh(self) -> None:
        self.controller.reload(scope="all")

    def shutdown(self) -> None:
        self._clock.stop()
        self._reconciliation_timer.stop()
        if self._reconciliation_order is not None:
            runtime = getattr(self.ctx, "quant_runtime", None)
            if runtime is not None:
                runtime.end_reconciliation(self._reconciliation_order.plan_id)
        self._reconciliation_order = None
        if self._watch_thread is not None and self._watch_thread.isRunning():
            self._watch_thread.requestInterruption()
            self._watch_thread.quit()
            self._watch_thread.wait(2000)

    def _on_facts_updated(self, _scope: str, symbol: object, _revision: int) -> None:
        if _scope == "ths_watchlist":
            self._ths_scan_running = False
            self.ths_watchlist_scan_button.setEnabled(True)
            self.ths_watchlist_scan_button.setText("重新同步扫描")
        self.controller.reload(scope=_scope, symbol=str(symbol or "") or None)

    def _runtime_status(self, task: str, detail: str) -> None:
        self._last_runtime_detail = f"{task}：{detail}"
        self.health_issue_button.setToolTip(self._last_runtime_detail)
        if task == "ths_watchlist" and hasattr(self, "ths_watchlist_status"):
            self.ths_watchlist_status.setText(detail)
            running = detail.startswith("正在")
            self._ths_scan_running = running
            self.ths_watchlist_scan_button.setEnabled(not running)
            self.ths_watchlist_scan_button.setText(
                "扫描中…" if running else "重新同步扫描"
            )
        if task == "selection" and hasattr(self, "selection_banner"):
            self.selection_banner.setText(detail)
            running = detail.startswith("正在")
            self.selection_refresh_button.setEnabled(not running)
            self.selection_refresh_button.setText("扫描中…" if running else "重新扫描")

    def _apply_view_model(self, view_model: QuantWorkbenchViewModel) -> None:
        self._view_model = view_model
        self._pool_rows = list(view_model.pool_rows)
        self._selected = view_model.selected
        self._render_health(view_model)
        self._refresh_pool_tree()
        self._render_selected(view_model.selected)
        self._render_account(view_model)
        self._render_selection(view_model)
        self._render_validation(view_model)

    def _render_health(self, view_model: QuantWorkbenchViewModel) -> None:
        health = view_model.global_health
        values = {
            "market": f"A股 · {health.market_session}",
            "data": f"数据 · {health.data_status}",
            "pool": f"股票池 · {_short(health.pool_version, 24)}",
            "strategy": f"策略 · {health.strategy_state} / {health.mode}",
            "broker": f"同花顺 · {health.broker_status}",
            "feishu": f"飞书 · {health.feishu_status}",
            "sync": f"同步 · {_time_only(health.last_sync)}",
        }
        for key, value in values.items():
            self.health_labels[key].setText(value)
        issues = health.issues
        self.health_issue_button.setText(f"{len(issues)}项需处理" if issues else "系统状态正常")
        self.health_issue_button.setProperty("hasIssues", bool(issues))
        self.health_issue_button.style().unpolish(self.health_issue_button)
        self.health_issue_button.style().polish(self.health_issue_button)
        self.health_issue_button.setToolTip("\n".join(issues) or "关键数据链路正常")

    def _refresh_pool_tree(self) -> None:
        if not hasattr(self, "pool_tree"):
            return
        selected_symbol = self.controller.selected_symbol
        view = str(self.pool_view_combo.currentData() or "system")
        status_filter = str(self.pool_status_combo.currentData() or "all")
        query = self.pool_search.text().strip().casefold()
        rows = [row for row in self._pool_rows if _row_in_view(row, view)]
        rows = [row for row in rows if _row_in_status(row, status_filter)]
        if query:
            rows = [
                row
                for row in rows
                if query in row.symbol.casefold() or query in row.name.casefold()
            ]
        self.pool_tree.blockSignals(True)
        self.pool_tree.clear()
        selected_item = None
        for row in rows:
            price = "—" if row.latest_price is None else f"{row.latest_price:.2f}"
            if row.pct_change is not None:
                price += f"\n{row.pct_change:+.2f}%"
            score = "—" if row.total_score is None else f"{row.total_score:.0f}"
            category = f" · {'/'.join(row.categories[:2])}" if row.categories else ""
            title = f"{row.name}\n{row.symbol} · {row.membership}{category}"
            item = QTreeWidgetItem([title, price, row.state_label, score])
            item.setData(0, Qt.ItemDataRole.UserRole, row.symbol)
            item.setToolTip(
                0,
                "\n".join(
                    part
                    for part in (
                        row.action,
                        "同花顺分类：" + "、".join(row.categories) if row.categories else "",
                        row.scan_reason,
                    )
                    if part
                ),
            )
            item.setToolTip(2, row.primary_issue or row.action)
            color = _state_color(row.state)
            item.setForeground(2, QColor(color))
            if row.state in {"exit_required", "quant_tradeable", "authorized"}:
                font = item.font(0)
                font.setBold(True)
                item.setFont(0, font)
            self.pool_tree.addTopLevelItem(item)
            if row.symbol == selected_symbol:
                selected_item = item
        if selected_item is not None:
            self.pool_tree.setCurrentItem(selected_item)
        self.pool_tree.blockSignals(False)
        counts = self._view_model.action_counts if self._view_model else {}
        self.pool_summary.setText(
            f"当前 {len(rows)}只 · 候选{counts.get('candidate', 0)} · "
            f"可交易{counts.get('tradeable', 0)} · 退出{counts.get('exit', 0)} · "
            f"风险{counts.get('risk', 0)}"
        )
        latest_sync = self.store.latest_ths_watchlist_sync()
        if latest_sync and not self._ths_scan_running:
            snapshot = dict(latest_sync.get("snapshot") or {})
            latest_results = self.store.latest_ths_watchlist_scan_results()
            candidates = sum(
                item.get("actionable_stage") == "next_session_candidate"
                for item in latest_results
            )
            self.ths_watchlist_status.setText(
                f"同花顺 {len(snapshot.get('members') or [])}只A股 · "
                f"{len(snapshot.get('categories') or [])}个分类 · "
                f"下次观察{candidates}只 · {_time_only(str(snapshot.get('captured_at') or ''))}"
            )
        self._sync_watchlist_button()

    def _pool_selection_changed(self) -> None:
        items = self.pool_tree.selectedItems()
        if not items:
            return
        symbol = str(items[0].data(0, Qt.ItemDataRole.UserRole) or "")
        if symbol:
            self.controller.select_symbol(symbol)

    def _pool_filter_changed(self) -> None:
        if self.store.available:
            self.store.save_workbench_preference(
                "pool_view", str(self.pool_view_combo.currentData() or "system")
            )
        self._refresh_pool_tree()

    def _render_selected(self, selected: SelectedStockContext) -> None:
        if not selected.symbol:
            self.stock_title.setText("股票池暂无可显示股票")
            self.stock_meta.setText("系统不会用示例数据填充")
            self.stock_price.setText("—")
            self.kline_chart.set_bars([])
            self._render_plan(selected)
            return
        sandbox = selected.sandbox or {}
        self.stock_title.setText(f"{selected.name}  {selected.symbol}")
        self.stock_meta.setText(
            f"{selected.membership} · 池版本 {_short(selected.pool_version or '—', 28)} · "
            f"状态时间 {_time_only(str(sandbox.get('observed_at') or ''))}"
        )
        latest = sandbox.get("latest_price")
        self.stock_price.setText("—" if latest is None else f"¥ {float(latest):.2f}")
        self._switch_chart_timeframe()
        self._render_lifecycle(str(sandbox.get("state") or "analysis_only"))
        score = selected.score or {}
        for key, label, maximum in (
            ("index", "指数", 40),
            ("sentiment", "情绪", 30),
            ("theme", "题材", 20),
            ("stock", "个股", 10),
        ):
            value = score.get(f"{key}_score")
            text = "—" if value is None else f"{float(value):.1f}"
            self.score_cards[key].setText(f"{label}\n{text}/{maximum}")
        total = score.get("total_score")
        if total is None:
            self.score_summary.setText("综合评分不可用 · 数据缺失不会按0分处理")
        else:
            self.score_summary.setText(
                f"综合 {float(total):.1f}/100 · 连续"
                f"{int(score.get('consecutive_pass_count') or 0)}根 · "
                f"{_score_status(str(score.get('status') or ''))}"
            )
        details = {
            "本根评分": score.get("component_details") or {},
            "硬阻断": score.get("hard_blocks") or sandbox.get("hard_blocks") or [],
            "数据缺口": score.get("data_gaps") or sandbox.get("data_gaps") or [],
            "数据时间": score.get("source_timestamps") or {},
            "输入哈希": score.get("input_hash") or "—",
        }
        pool_row = next(
            (item for item in self._pool_rows if item.symbol == selected.symbol),
            None,
        )
        if pool_row and pool_row.in_ths_watchlist:
            details["同花顺自选分类"] = pool_row.categories
            details["同花顺日线扫描结论"] = pool_row.scan_reason or "等待扫描"
        if selected.previous_score:
            details["上一根评分"] = {
                "时间": selected.previous_score.get("bar_closed_at"),
                "总分": selected.previous_score.get("total_score"),
                "状态": selected.previous_score.get("status"),
            }
        self.score_details.setPlainText(
            json.dumps(details, ensure_ascii=False, indent=2, default=str)
        )
        hotspot = selected.hotspot or {}
        items = hotspot.get("items") or []
        hotspot_lines = []
        for item in items[:10]:
            hotspot_lines.append(
                f"{item.get('published_at') or item.get('captured_at') or '—'}\n"
                f"{item.get('title') or '未命名信息'}\n"
                f"来源：{item.get('source_name') or item.get('source') or '—'}"
            )
        negatives = hotspot.get("negative_blocks") or []
        if negatives:
            hotspot_lines.insert(0, "重大风险阻断：" + "、".join(map(str, negatives)))
        self.hotspot_details.setPlainText(
            "\n\n".join(hotspot_lines)
            if hotspot_lines
            else "尚无可回溯热点快照。热点缺失不会被当作正向加分。"
        )
        self._render_plan(selected)
        self._sync_watchlist_button()

    def _switch_chart_timeframe(self) -> None:
        if self._selected is None:
            return
        checked = self.tf_group.checkedButton()
        timeframe = str(checked.property("timeframe") if checked else "15m")
        bars = self._selected.daily_bars if timeframe == "1d" else self._selected.intraday_bars
        self.kline_chart.set_bars(bars)
        last_time = str((bars[-1] if bars else {}).get("time") or "")
        self.chart_fact_time.setText(
            f"{timeframe} · {len(bars)}根 · {_time_only(last_time) if last_time else '无数据'}"
        )

    def _render_lifecycle(self, state: str) -> None:
        stages = [
            ("daily", "日线观察"),
            ("score", "15分钟评分"),
            ("risk", "组合风控"),
            ("broker", "同花顺"),
            ("position", "持仓"),
            ("exit", "退出"),
        ]
        active = _lifecycle_index(state)
        chunks = []
        for index, (_key, label) in enumerate(stages):
            color = _ACCENT if index == active else _GREEN if index < active else _MUTED
            chunks.append(f"<span style='color:{color};font-weight:600'>{label}</span>")
        self.lifecycle.setText("&nbsp; → &nbsp;".join(chunks))

    def _render_plan(self, selected: SelectedStockContext) -> None:
        sandbox = selected.sandbox or {}
        plan = selected.plan or {}
        risk = selected.risk or {}
        state = str(sandbox.get("state") or "analysis_only")
        self.stage_badge.setText(str(sandbox.get("state_label") or "等待选择股票"))
        self.stage_badge.setStyleSheet(
            f"color:{_state_color(state)};font-weight:700;padding:7px 9px;"
            f"background:{_SURFACE};border-left:3px solid {_state_color(state)};"
        )
        self.stage_explanation.setText(str(sandbox.get("action") or "当前没有可执行交易动作"))
        blocks = list(sandbox.get("hard_blocks") or []) or list(sandbox.get("data_gaps") or [])
        self.next_condition.setText(_next_condition(state, blocks))
        self.plan_details.setPlainText(
            "\n".join(
                (
                    f"计划编号  {plan.get('id') or '尚未生成'}",
                    f"策略      {plan.get('strategy_version') or '—'}",
                    f"方向      {plan.get('direction') or '—'}",
                    f"触发价    {_number(sandbox.get('trigger_price') or plan.get('entry_price'))}",
                    "最高价    "
                    + _number(sandbox.get("max_entry_price") or risk.get("max_entry_price")),
                    "初始止损  "
                    + _number(sandbox.get("initial_stop") or plan.get("stop_loss_price")),
                    f"有效期    {sandbox.get('valid_until') or plan.get('valid_until') or '—'}",
                    f"计划状态  {plan.get('status') or 'none'}",
                )
            )
        )
        broker = selected.broker or {}
        self.risk_details.setPlainText(
            "\n".join(
                (
                    "风险与账户事实",
                    f"授权状态  {risk.get('authorization_status') or '尚未评估'}",
                    f"单笔风险  {risk.get('risk_pct') or risk.get('effective_risk_pct') or '—'}",
                    f"计划数量  {risk.get('quantity') or '—'}",
                    f"可用资金  {_money(broker.get('available_cash'))}",
                    f"账户快照  {_time_only(str(broker.get('captured_at') or ''))}",
                    f"阻断原因  {'；'.join(map(str, blocks[:3])) if blocks else '无'}",
                )
            )
        )
        events = self.store.list_events(str(plan.get("id"))) if plan.get("id") else []
        self.plan_timeline.setPlainText(
            "\n".join(
                f"{_time_only(str(event.get('event_at') or ''))}  {event.get('event_type')}"
                for event in events[-8:]
            )
            or "交易时间轴将在计划生成后出现"
        )
        self._configure_primary_action(state, selected)

    def _configure_primary_action(self, state: str, selected: SelectedStockContext) -> None:
        self.primary_action.hide()
        self._primary_action_name = ""
        health = self._view_model.global_health if self._view_model else None
        strategy_active = bool(health and health.strategy_state.casefold() in {"active", "reduced"})
        broker_complete = bool((selected.broker or {}).get("complete"))
        if state == "data_incomplete":
            self._show_primary("立即重新同步", "refresh")
        elif state in {"quant_tradeable", "authorized"} and not strategy_active:
            self._show_primary("查看策略验证进度", "validation")
        elif state == "quant_tradeable" and not broker_complete:
            self._show_primary("检查同花顺账户", "account")
        elif state == "authorized":
            allow_prefill = bool(
                getattr(self.ctx.settings.ths, "allow_prefill", False)
                and not getattr(self.ctx.settings.ths, "read_only", True)
            )
            if allow_prefill and broker_complete:
                self._show_primary("安全预填到同花顺", "prefill")
            else:
                self._show_primary("检查同花顺预填条件", "account")
        elif state == "exit_required":
            self._show_primary("进入账户处理退出", "account")
        elif state in {"account_risk_blocked", "waiting_user_confirmation"}:
            self._show_primary("查看交易账户", "account")
        elif state in {"major_risk_blocked", "invalidated"}:
            self._show_primary("查看风险与验证", "validation")

    def _show_primary(self, label: str, action: str) -> None:
        self.primary_action.setText(label)
        self._primary_action_name = action
        self.primary_action.show()

    def _run_primary_action(self) -> None:
        if self._primary_action_name == "refresh":
            self._refresh_all_now()
        elif self._primary_action_name == "account":
            self.navigate("account")
        elif self._primary_action_name == "validation":
            self.navigate("validation")
        elif self._primary_action_name == "prefill":
            self._prefill_selected_plan()

    def _prefill_selected_plan(self) -> None:
        selected = self._selected
        if selected is None or not selected.plan:
            return
        plan = selected.plan
        plan_id = str(plan.get("id") or "")
        try:
            from pa_agent.trading.equity import portfolio_snapshot_from_store
            from pa_agent.trading.quant import SignalDecision
            from pa_agent.trading.stability import StrategyState

            decision = self.store.get_decision(str(plan.get("decision_event_id") or ""))
            if not decision:
                raise ValueError("计划缺少确定性信号快照")
            signal = SignalDecision.model_validate(decision["final_decision"])
            profile = self.store.get_profile(str(plan.get("symbol") or ""))
            if profile is None or not profile.confirmed:
                raise ValueError("请先在系统验证中确认股票交易制度和真实费用")
            snapshot = self.ctx.broker_adapter.snapshot()
            self.store.add_broker_snapshot(snapshot)
            score = TopDownScoreSnapshot.model_validate(selected.score) if selected.score else None
            if score is None or not score.eligible_for_risk:
                raise ValueError("最新两根15分钟评分尚未完成确定性放行")
            state_owner = (
                TOPDOWN_STRATEGY_ID
                if signal.strategy_id == MANUAL_EXCEPTION_STRATEGY_ID
                else signal.strategy_id
            )
            state = StrategyState(self.store.current_strategy_state(state_owner))
            outside_approval_valid = signal.strategy_id != MANUAL_EXCEPTION_STRATEGY_ID
            if signal.strategy_id == MANUAL_EXCEPTION_STRATEGY_ID:
                approval = self.store.valid_outside_pool_approval(
                    plan_id=plan_id,
                    account_fingerprint=snapshot.account_fingerprint,
                )
                if approval is None:
                    answer = QMessageBox.question(
                        self,
                        "批准本次池外例外计划",
                        "该计划固定使用半风险、最多同时1只且不加仓。批准只对当前计划有效，是否继续？",
                    )
                    if answer != QMessageBox.StandardButton.Yes:
                        return
                    self.store.add_outside_pool_approval(
                        review_id=f"manual-{plan_id}",
                        plan_id=plan_id,
                        account_fingerprint=snapshot.account_fingerprint,
                        effective_risk_pct=self.ctx.settings.risk.per_trade_risk_pct * 0.5,
                        valid_until=str(plan.get("valid_until") or ""),
                        audit_reason="user_approved_current_outside_pool_plan_half_risk",
                    )
                outside_approval_valid = True
            risk = self.ctx.portfolio_risk.authorize(
                plan_id=plan_id,
                signal=signal,
                broker=snapshot,
                portfolio=portfolio_snapshot_from_store(self.store, snapshot),
                strategy_state=state,
                profile=profile,
                external_quote_price=plan.get("last_price") or signal.trigger_price,
                topdown_score=score,
                trading_channel=(
                    "outside_pool_exception"
                    if signal.strategy_id == MANUAL_EXCEPTION_STRATEGY_ID
                    else "normal_pool"
                ),
                outside_pool_approval_valid=outside_approval_valid,
                outside_pool_position_count=0,
                expected_security_name=selected.name,
            )
            self.store.append_event(
                plan_id, "risk_authorization", details=risk.model_dump(mode="json")
            )
            if risk.order is None:
                raise ValueError("；".join(risk.reasons) or "组合风控未授权")
            receipt = self.ctx.broker_adapter.prefill(risk.order)
            self.store.append_event(
                plan_id, "broker_prefill", details=receipt.model_dump(mode="json")
            )
            if receipt.status != "awaiting_user_confirmation":
                raise ValueError(receipt.message)
            self.store.update_plan(plan_id, status="awaiting_user_confirmation")
            self.store.append_event(
                plan_id,
                "awaiting_user_confirmation",
                details={"authorized_order": risk.order.model_dump(mode="json")},
            )
            self._start_reconciliation(risk.order)
            self.plan_status_feedback.setText(
                "字段已回读一致，正在等待你在同花顺中最终确认。PA Agent没有点击委托或确认按钮。"
            )
            self.plan_status_feedback.show()
            self.refresh()
        except Exception as exc:  # noqa: BLE001
            self.plan_status_feedback.setText(f"安全预填未执行：{exc}")
            self.plan_status_feedback.show()

    def _start_reconciliation(self, order: Any) -> None:
        """Start the bounded read-only broker reconciliation window."""
        self._reconciliation_order = order
        runtime = getattr(self.ctx, "quant_runtime", None)
        if runtime is not None:
            runtime.begin_reconciliation(order.plan_id)
        self._reconciliation_attempts = 0
        self._reconciliation_matched = False
        self._reconciliation_timer.start()

    def _finish_reconciliation(self) -> None:
        order = self._reconciliation_order
        self._reconciliation_timer.stop()
        if order is not None:
            runtime = getattr(self.ctx, "quant_runtime", None)
            if runtime is not None:
                runtime.end_reconciliation(order.plan_id)
        self._reconciliation_order = None

    def _poll_reconciliation(self) -> None:
        """Read orders/fills every two seconds; never submit, cancel or confirm."""
        order = self._reconciliation_order
        if order is None:
            self._reconciliation_timer.stop()
            return
        plan = self.store.get_plan(order.plan_id)
        if plan is None or str(plan.get("status") or "") == "invalidated":
            self._finish_reconciliation()
            self._reconciliation_matched = False
            self.plan_status_feedback.setText(
                "计划已失效，对账已安全停止；如已在同花顺手工确认，请核查真实委托和成交。"
            )
            self.plan_status_feedback.show()
            self.refresh()
            return
        self._reconciliation_attempts += 1
        try:
            snapshot = self.ctx.broker_adapter.snapshot()
            self.store.add_broker_snapshot(snapshot)
            reconciliation = self.ctx.broker_adapter.reconcile(order, snapshot)
            if reconciliation.status == "matched":
                self._apply_reconciliation(order, snapshot, reconciliation)
                self._reconciliation_matched = True
                broker_order = next(
                    item
                    for item in snapshot.orders
                    if item.broker_order_id == reconciliation.matched_order_ids[0]
                )
                status, _event_type = self.ctx.broker_trade_lifecycle.broker_order_status(
                    broker_order.status,
                    broker_order.filled_quantity,
                    broker_order.quantity,
                )
                self.plan_status_feedback.setText(
                    "已唯一匹配同花顺委托，正在同步部分成交或最终状态。"
                )
                self.plan_status_feedback.show()
                if status in {"filled", "cancelled", "rejected"}:
                    self._finish_reconciliation()
                self.refresh()
                if self._reconciliation_order is None:
                    return
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("同花顺成交对账轮询失败: %s", exc)
        if self._reconciliation_attempts >= 30:
            if not self._reconciliation_matched:
                self.store.update_plan(order.plan_id, status="reconciliation_required")
                self.store.append_event(
                    order.plan_id,
                    "reconciliation_required",
                    details={"poll_attempts": 30, "window_seconds": 60},
                )
                self.plan_status_feedback.setText(
                    "60秒内未能唯一匹配委托，已转入人工对账；系统没有猜测或代替确认。"
                )
                self.plan_status_feedback.show()
            self._finish_reconciliation()
            self.refresh()

    def _apply_reconciliation(self, order: Any, snapshot: Any, reconciliation: Any) -> None:
        broker_order_id = reconciliation.matched_order_ids[0]
        broker_order = next(
            item for item in snapshot.orders if item.broker_order_id == broker_order_id
        )
        matched_ids = set(reconciliation.matched_fill_ids)
        fills = [
            item for item in snapshot.fills if item.broker_fill_id in matched_ids
        ]
        status, event_type = self.ctx.broker_trade_lifecycle.broker_order_status(
            broker_order.status,
            broker_order.filled_quantity,
            broker_order.quantity,
        )
        self.store.link_broker_order(
            reconciliation,
            account_fingerprint=snapshot.account_fingerprint,
            details={
                "order": broker_order.model_dump(mode="json"),
                "authorized_order": order.model_dump(mode="json"),
                "poll_attempts": self._reconciliation_attempts,
            },
        )
        self.store.upsert_broker_execution(
            plan_id=order.plan_id,
            fills=fills,
            plan_status="executed_open" if status == "filled" else status,
            event_type=event_type,
            broker_order_id=broker_order_id,
            account_fingerprint=snapshot.account_fingerprint,
        )

    def _render_account(self, view_model: QuantWorkbenchViewModel) -> None:
        account = view_model.account_summary
        self.account_banner.setText(
            (
                f"账户事实已完整核验 · {_time_only(account.captured_at)} · "
                "所有金额和成交以同花顺为准"
                if account.complete
                else (
                    f"账户事实未完整核验 · {_time_only(account.captured_at)} · "
                    "历史快照仅供查看，预填保持关闭"
                )
            )
        )
        for key, title, value in (
            ("equity", "总资产", account.total_equity),
            ("cash", "可用资金", account.available_cash),
            ("position", "持仓市值", account.position_value),
            ("pnl", "当日盈亏", account.daily_pnl),
        ):
            self.account_metrics[key].setText(f"{title}\n{_money(value)}")
        _fill_table(
            self.positions_table,
            [
                [
                    item.get("symbol"),
                    item.get("name"),
                    item.get("quantity"),
                    item.get("sellable_quantity"),
                    item.get("cost_price"),
                    item.get("last_price"),
                    item.get("market_value"),
                ]
                for item in account.positions
            ],
            symbol_column=0,
        )
        _fill_table(
            self.orders_table,
            [
                [
                    item.get("broker_order_id"),
                    item.get("symbol"),
                    item.get("direction"),
                    item.get("price"),
                    item.get("quantity"),
                    item.get("filled_quantity"),
                    item.get("status"),
                    item.get("submitted_at"),
                ]
                for item in account.orders
            ],
            symbol_column=1,
        )
        _fill_table(
            self.fills_table,
            [
                [
                    item.get("broker_fill_id"),
                    item.get("symbol"),
                    item.get("direction"),
                    item.get("price"),
                    item.get("quantity"),
                    item.get("fees"),
                    item.get("filled_at"),
                ]
                for item in account.fills
            ],
            symbol_column=1,
        )
        links = self.store.list_broker_order_links()
        _fill_table(
            self.reconcile_table,
            [
                [
                    item.get("plan_id"),
                    item.get("broker_order_id"),
                    item.get("match_status"),
                    item.get("updated_at"),
                ]
                for item in links
            ],
        )
        self.monthly_text.setPlainText(self._monthly_account_summary(view_model))

    def _monthly_account_summary(self, view_model: QuantWorkbenchViewModel) -> str:
        """Render cash-flow-adjusted account performance without inventing gaps."""
        from pa_agent.trading.equity import (
            monthly_equity_peak_drawdown_pct,
            monthly_return_pct,
        )

        broker = view_model.selected.broker or {}
        account = str(broker.get("account_fingerprint") or "")
        snapshots = (
            self.store.list_equity_snapshots(account_fingerprint=account)
            if account else []
        )
        latest = snapshots[-1] if snapshots else {}
        current_month = str(
            latest.get("captured_at") or view_model.account_summary.captured_at or ""
        )[:7]
        month_snapshots = [
            item
            for item in snapshots
            if str(item.get("captured_at") or "").startswith(current_month)
        ]
        flows = (
            self.store.list_broker_cash_flows(
                account_fingerprint=account,
                start_at=f"{current_month}-01T00:00:00+08:00",
                end_at=str(latest.get("captured_at") or ""),
            )
            if account and current_month and latest else []
        )
        deposits = sum(
            float(item.get("amount") or 0)
            for item in flows if item.get("direction") == "deposit"
        )
        withdrawals = sum(
            float(item.get("amount") or 0)
            for item in flows if item.get("direction") == "withdrawal"
        )
        cash_flow_complete = bool(
            account
            and current_month
            and latest
            and self.store.cash_flow_history_complete(
                account,
                range_start=f"{current_month}-01T00:00:00+08:00",
                range_end=str(latest.get("captured_at") or ""),
            )
        )
        monthly = monthly_return_pct(month_snapshots)
        peak_drawdown = monthly_equity_peak_drawdown_pct(month_snapshots)
        gaps: list[str] = []
        if len(month_snapshots) < 2:
            gaps.append("月度权益基线不足")
        if not cash_flow_complete:
            gaps.append("同花顺本月资金流水尚未核验完整")
        trusted_monthly = monthly if not gaps else None

        actual_results = [
            item for item in self.store.list_results(dataset="actual")
            if not current_month or str(item.get("closed_at") or "").startswith(current_month)
        ]
        normal = [
            item for item in actual_results
            if item.get("strategy_version") == TOPDOWN_STRATEGY_ID
        ]
        exceptions = [
            item for item in actual_results
            if item.get("strategy_version") == MANUAL_EXCEPTION_STRATEGY_ID
        ]
        external = [
            item for item in self.store.list_external_broker_trades()
            if (not account or item.get("account_fingerprint") == account)
            and (not current_month or str(item.get("filled_at") or "").startswith(current_month))
        ]

        def attribution(rows: list[dict[str, Any]]) -> str:
            pnl = sum(float(item.get("net_pnl") or 0) for item in rows)
            return f"{len(rows)}笔 / 已实现净收益 {pnl:+.2f}"

        return (
            f"统计月份：{current_month or '尚无可核验月份'}\n"
            f"月初权益：{month_snapshots[0].get('total_equity', '—') if month_snapshots else '—'}\n"
            f"最新总资产：{latest.get('total_equity', '—')}\n"
            f"本月入金：{deposits:.2f}\n"
            f"本月出金：{withdrawals:.2f}\n"
            f"净外部现金流：{deposits - withdrawals:+.2f}\n"
            "扣除出入金后的月度收益："
            f"{'—' if trusted_monthly is None else f'{trusted_monthly:+.2f}%'}\n"
            "本月高点回撤："
            f"{'—' if peak_drawdown is None else f'{peak_drawdown:.2f}%'}\n"
            f"资金流水完整性：{'已核验' if cash_flow_complete else '未核验'}\n"
            f"数据缺口：{'；'.join(gaps) if gaps else '无'}\n\n"
            f"正常策略：{attribution(normal)}\n"
            f"池外例外：{attribution(exceptions)}\n"
            f"外部手工交易：{len(external)}笔（纳入账户净值，不计入策略绩效）\n"
            "影子交易与真实账户继续使用隔离的数据集。"
        )

    def _render_selection(self, view_model: QuantWorkbenchViewModel) -> None:
        if not hasattr(self, "selection_table"):
            return
        snapshot = dict(view_model.selection_snapshot or {})
        self._selection_snapshot = snapshot
        scanned = int(snapshot.get("scanned_count") or 0)
        candidates = list(snapshot.get("candidates") or [])
        generated = str(snapshot.get("generated_at") or "")
        self.selection_metrics["scan"].setText(f"扫描种子\n{scanned}只")
        self.selection_metrics["candidate"].setText(f"入选股票\n{len(candidates)}只")
        self.selection_metrics["negative"].setText("负面新闻过滤\n已强制执行")
        self.selection_metrics["time"].setText(
            f"最近扫描\n{_time_only(generated) if generated else '尚未运行'}"
        )
        if not snapshot:
            self.selection_banner.setText(
                "尚未生成智能选股快照。点击“重新扫描”后，系统会先扫描全A候选种子，"
                "再逐只核验日线、题材资金与重大负面公告。"
            )
        elif snapshot.get("data_gaps"):
            self.selection_banner.setText(
                "本次扫描存在数据源缺口；缺失数据的股票不会入选。"
                + " · ".join(str(item) for item in snapshot.get("data_gaps") or [])
            )
        else:
            counts = dict(snapshot.get("strategy_counts") or {})
            self.selection_banner.setText(
                f"选股完成 · 热点题材{counts.get('hot_theme', 0)}只 · "
                f"主力题材{counts.get('main_force_theme', 0)}只 · "
                f"量能窒息{counts.get('volume_suffocation', 0)}只 · "
                f"趋势启动{counts.get('trend_start', 0)}只 · "
                "所有入选股票已通过重大负面公告硬过滤"
            )
        self._render_selection_rows()

    def _render_selection_rows(self) -> None:
        if not hasattr(self, "selection_table"):
            return
        snapshot = dict(getattr(self, "_selection_snapshot", {}) or {})
        candidates = list(snapshot.get("candidates") or [])
        current_filter = "all"
        selected_filters = self.selection_filter_tree.selectedItems()
        if selected_filters:
            current_filter = str(
                selected_filters[0].data(0, Qt.ItemDataRole.UserRole) or "all"
            )
        if current_filter != "all":
            candidates = [
                item for item in candidates
                if current_filter in (item.get("strategy_tags") or [])
            ]
        previous_symbol = ""
        current = self.selection_table.currentRow()
        if current >= 0:
            item = self.selection_table.item(current, 0)
            previous_symbol = str(item.data(Qt.ItemDataRole.UserRole) or "") if item else ""
        self.selection_table.blockSignals(True)
        self.selection_table.setRowCount(len(candidates))
        selected_row = -1
        for row_index, candidate in enumerate(candidates):
            tags = [
                STRATEGY_LABELS.get(str(item), str(item))
                for item in candidate.get("strategy_tags") or []
            ]
            values = (
                str(candidate.get("symbol") or ""),
                str(candidate.get("name") or ""),
                _number(candidate.get("latest_price")),
                (
                    f"{float(candidate['pct_change']):+.2f}%"
                    if candidate.get("pct_change") is not None else "—"
                ),
                " / ".join(tags),
                " / ".join(str(item) for item in (candidate.get("themes") or [])[:3]),
                _number(candidate.get("score")),
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                if column == 0:
                    cell.setData(Qt.ItemDataRole.UserRole, str(candidate.get("symbol") or ""))
                self.selection_table.setItem(row_index, column, cell)
            if str(candidate.get("symbol") or "") == previous_symbol:
                selected_row = row_index
        self.selection_table.blockSignals(False)
        self.selection_empty.setVisible(not candidates)
        if candidates:
            self.selection_table.selectRow(selected_row if selected_row >= 0 else 0)
            self._selection_row_changed()
        else:
            self.selection_detail_title.setText("当前筛选没有候选股票")
            self.selection_detail.setText(
                "系统不会用示例数据填充。可以重新扫描，或切换其他选股策略。"
            )
            self.selection_add_button.hide()
            self.selection_monitor_button.hide()

    def _selection_row_changed(self) -> None:
        if not hasattr(self, "selection_table"):
            return
        row = self.selection_table.currentRow()
        item = self.selection_table.item(row, 0) if row >= 0 else None
        symbol = str(item.data(Qt.ItemDataRole.UserRole) or "") if item else ""
        snapshot = dict(getattr(self, "_selection_snapshot", {}) or {})
        candidate = next(
            (dict(value) for value in snapshot.get("candidates") or []
             if str(value.get("symbol") or "") == symbol),
            None,
        )
        self._selected_candidate = candidate
        if candidate is None:
            self.selection_add_button.hide()
            self.selection_monitor_button.hide()
            return
        if self.controller.selected_symbol != symbol:
            self.controller.select_symbol(symbol)
        tags = [
            STRATEGY_LABELS.get(str(value), str(value))
            for value in candidate.get("strategy_tags") or []
        ]
        evidence = dict(candidate.get("evidence") or {})
        sources = dict(candidate.get("source_timestamps") or {})
        lines = [
            f"股票：{candidate.get('name')}  {symbol}",
            f"入选策略：{'、'.join(tags)}",
            f"所属题材：{'、'.join(candidate.get('themes') or []) or '未提供'}",
            f"综合排序分：{_number(candidate.get('score'))}",
            "",
            "重大负面核验：已通过",
            "未发现官方确认的立案处罚、财务造假、显著业绩恶化、重大减持、",
            "诉讼违约、ST/退市、重大停产或停牌风险。",
            "",
            "量能与趋势证据：",
            f"5日/前20日成交量比：{evidence.get('volume_ratio_5_to_previous20', '—')}",
            f"ATR收缩比：{evidence.get('atr_contraction_ratio', '—')}",
            f"振幅收缩比：{evidence.get('range_contraction_ratio', '—')}",
            f"MA20 / MA60：{evidence.get('ma20', '—')} / {evidence.get('ma60', '—')}",
            f"最新量比：{evidence.get('latest_volume_ratio_20', '—')}",
            "",
            "题材与主力证据：",
            f"题材强度分位：{evidence.get('theme_relative_strength_percentile', '—')}",
            f"热点持续天数：{evidence.get('theme_persistence_days', '—')}",
            f"题材主力净流入占比：{evidence.get('theme_main_net_inflow_pct', '—')}%",
            f"热点快照：{sources.get('hotspot') or '—'}",
            f"日线快照：{sources.get('daily_bar') or '—'}",
            "",
            "下一步：加入监控池后进入独立股票沙箱。池外股票仍需走",
            "manual_exception_4321_v1，不会因入选而直接获得交易授权。",
        ]
        self.selection_detail_title.setText(
            f"{candidate.get('name')}  {symbol} · {' / '.join(tags)}"
        )
        self.selection_detail.setPlainText("\n".join(lines))
        pool_row = next((value for value in self._pool_rows if value.symbol == symbol), None)
        already_watched = bool(pool_row and pool_row.in_personal_watchlist)
        self.selection_add_button.setVisible(not already_watched)
        self.selection_monitor_button.show()
        self.selection_feedback.setVisible(already_watched)
        if already_watched:
            self.selection_feedback.setText("这只股票已在我的监控池中")

    def _refresh_stock_selection(self) -> None:
        runtime = getattr(self.ctx, "quant_runtime", None)
        if runtime is None or not hasattr(runtime, "ensure_stock_selection"):
            self.selection_banner.setText(
                "量化后台未运行，无法扫描。请到系统验证检查运行状态。"
            )
            return
        self.selection_refresh_button.setEnabled(False)
        self.selection_refresh_button.setText("扫描中…")
        runtime.ensure_stock_selection(force=True)

    def _add_selected_candidate(self) -> None:
        candidate = dict(getattr(self, "_selected_candidate", {}) or {})
        symbol = str(candidate.get("symbol") or "")
        if not symbol:
            return
        self.store.upsert_watchlist_member(
            symbol=symbol,
            name=str(candidate.get("name") or symbol),
            source="user_watchlist",
            metadata={
                "selection_strategy_version": candidate.get("strategy_version"),
                "selection_tags": list(candidate.get("strategy_tags") or []),
                "selection_input_hash": candidate.get("input_hash"),
            },
        )
        self.selection_feedback.setText("已加入我的监控池，独立股票沙箱将持续跟踪")
        self.selection_feedback.show()
        self.selection_add_button.hide()
        self.controller.reload(scope="watchlist", symbol=symbol)

    def _open_selected_candidate(self) -> None:
        candidate = dict(getattr(self, "_selected_candidate", {}) or {})
        symbol = str(candidate.get("symbol") or "")
        if not symbol:
            return
        self.controller.select_symbol(symbol)
        self.navigate("monitor")

    def _render_validation(self, view_model: QuantWorkbenchViewModel) -> None:
        summary = view_model.validation_summary
        self.validation_banner.setText(
            f"4:3:2:1策略 {summary.strategy_state.upper()} · "
            f"验证记录 {summary.validation_runs} · "
            f"{'；'.join(summary.blockers) if summary.blockers else '已满足当前阶段条件'}"
        )
        self.validation_progress.setPlainText(
            json.dumps(
                {
                    "状态链": "CANDIDATE → SHADOW → ACTIVE → REDUCED → PAUSED → RETIRED",
                    "当前状态": summary.strategy_state.upper(),
                    "当前验证纪元": summary.current_epoch or "尚未建立",
                    "验证记录数": summary.validation_runs,
                    "前置条件缺口": summary.blockers,
                    "实盘边界": "验证未通过前不显示实盘预填动作",
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        health = view_model.global_health
        self.validation_data.setPlainText(
            json.dumps(
                {
                    "数据状态": health.data_status,
                    "股票池": health.pool_version,
                    "同花顺": health.broker_status,
                    "飞书": health.feishu_status,
                    "最近账户同步": health.last_sync,
                    "运行时诊断": self._last_runtime_detail or "暂无新的运行时错误",
                    "安全原则": "任何数据缺失、账户不匹配或客户端弹窗都会阻断预填",
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        universes = self.store.list_universe_snapshots(limit=1)
        universe = dict((universes[0] if universes else {}).get("snapshot") or {})
        _fill_table(
            self.validation_pool_table,
            [
                [
                    member.get("rank"),
                    member.get("symbol"),
                    member.get("name"),
                    member.get("industry") or member.get("theme"),
                    "允许" if member.get("authorization_eligible", True) else "仅分析",
                    member.get("data_updated_at") or universe.get("source_as_of"),
                ]
                for member in universe.get("members") or []
            ],
            symbol_column=1,
        )
        notifications = self.store.list_quant_notifications(limit=200)
        _fill_table(
            self.notification_table,
            [
                [
                    item.get("created_at"),
                    item.get("symbol"),
                    item.get("event_type"),
                    item.get("plan_id"),
                    item.get("status"),
                    (item.get("details") or {}).get("attempt_count", 1),
                ]
                for item in notifications
            ],
            symbol_column=1,
        )

    def _account_symbol_selected(self, item: QTableWidgetItem) -> None:
        table = item.tableWidget()
        if table is self.positions_table:
            symbol_column = 0
        elif table in {self.orders_table, self.fills_table}:
            symbol_column = 1
        else:
            return
        symbol_item = table.item(item.row(), symbol_column)
        symbol = symbol_item.text() if symbol_item else ""
        if symbol:
            self.controller.select_symbol(symbol)
            self.navigate(0)

    def _sync_broker(self) -> None:
        runtime = getattr(self.ctx, "quant_runtime", None)
        if runtime is None:
            self.account_banner.setText("量化后台未运行，无法执行只读同步")
            return
        self.broker_sync_button.setText("同步中…")
        self.broker_sync_button.setEnabled(False)
        try:
            runtime.sync_broker()
        finally:
            self.broker_sync_button.setText("立即只读同步")
            self.broker_sync_button.setEnabled(True)

    def _refresh_all_now(self) -> None:
        runtime = getattr(self.ctx, "quant_runtime", None)
        if runtime is None:
            self.plan_status_feedback.setText("量化后台未运行，未发起刷新")
            self.plan_status_feedback.show()
            return
        runtime.sync_broker()
        runtime.ensure_current_universe()
        runtime.ensure_daily_candidates()
        runtime.refresh_hotspots()
        runtime.refresh_topdown_scores()
        runtime.refresh_stock_sandboxes()
        self.plan_status_feedback.setText("已发起完整刷新；页面会按事实更新，不会生成模拟数据。")
        self.plan_status_feedback.show()

    def _add_watchlist(self) -> None:
        value = self.watch_input.text().strip()
        if not value or self._watch_thread is not None:
            return
        self.watch_add_button.setEnabled(False)
        self.watch_add_button.setText("校验中…")
        self.watch_feedback.setText("正在校验A股身份、上市时间、20日成交额和交易状态")
        self.watch_feedback.show()
        thread = QThread(self)
        worker = _WatchlistWorker(self.ctx, value)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._watchlist_added)
        worker.failed.connect(self._watchlist_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._watchlist_thread_finished)
        self._watch_thread = thread
        self._watch_worker = worker
        thread.start()

    def _watchlist_added(self, row: object) -> None:
        data = dict(row) if isinstance(row, dict) else {}
        symbol = str(data.get("symbol") or "")
        self.watch_input.clear()
        self.watch_feedback.setText(
            f"已加入 {data.get('name') or symbol}（{symbol}）。"
            + (
                "该股票属于系统池。"
                if self._system_member(symbol)
                else "该股票为池外观察，交易时固定走半风险例外通道。"
            )
        )
        runtime = getattr(self.ctx, "quant_runtime", None)
        if runtime is not None:
            runtime.refresh_stock_sandboxes()
            runtime.refresh_hotspots()
        self.controller.reload(scope="watchlist", symbol=symbol)
        self.controller.select_symbol(symbol)

    def _watchlist_failed(self, error: str) -> None:
        self.watch_feedback.setText(f"未加入：{error}")

    def _watchlist_thread_finished(self) -> None:
        self._watch_thread = None
        self._watch_worker = None
        self.watch_add_button.setEnabled(True)
        self.watch_add_button.setText("加入关注")

    def _scan_ths_watchlist(self) -> None:
        runtime = getattr(self.ctx, "quant_runtime", None)
        service = getattr(self.ctx, "ths_watchlist_service", None)
        if runtime is None or not hasattr(runtime, "ensure_ths_watchlist_scan"):
            self.ths_watchlist_status.setText(
                "量化后台未运行，无法同步同花顺自选；请到系统验证查看运行状态"
            )
            return
        if service is None:
            self.ths_watchlist_status.setText(
                "未定位同花顺自选文件；请在系统验证中检查远航版安装路径"
            )
            return
        self._ths_scan_running = True
        self.ths_watchlist_scan_button.setEnabled(False)
        self.ths_watchlist_scan_button.setText("扫描中…")
        self.ths_watchlist_status.setText("正在只读同步全部分类并逐只运行确定性策略")
        runtime.ensure_ths_watchlist_scan(force=True)

    def _remove_watchlist(self) -> None:
        symbol = self.controller.selected_symbol
        row = next((item for item in self._pool_rows if item.symbol == symbol), None)
        if row is None or not row.in_personal_watchlist:
            return
        deferred = ""
        if row.forced_tracking:
            deferred = "存在持仓、开放计划或待对账记录，继续强制跟踪至生命周期结束"
        removed = self.store.remove_watchlist_member(
            symbol,
            deferred_reason=deferred,
            source="user_watchlist",
        )
        if not removed:
            # Schema 18 and earlier used the generic ``user`` source.
            removed = self.store.remove_watchlist_member(
                symbol,
                deferred_reason=deferred,
                source="user",
            )
        if removed:
            self.watch_feedback.setText(
                "已从我的监控池移除。" + (deferred if deferred else "历史记录仍保留。")
            )
            self.watch_feedback.show()
            runtime = getattr(self.ctx, "quant_runtime", None)
            if runtime is not None:
                runtime.refresh_stock_sandboxes()
            self.controller.reload(scope="watchlist")

    def _sync_watchlist_button(self) -> None:
        symbol = self.controller.selected_symbol
        row = next((item for item in self._pool_rows if item.symbol == symbol), None)
        self.watch_remove_button.setVisible(bool(row and row.in_personal_watchlist))
        if row and row.forced_tracking:
            self.watch_remove_button.setText("移除关注（仍保留强制跟踪）")
        else:
            self.watch_remove_button.setText("从我的监控池移除")
        if row and row.in_ths_watchlist and not row.in_personal_watchlist:
            self.watch_remove_button.setToolTip(
                "该股票来自同花顺自选；请在同花顺中修改，下次同步会保留完整审计记录"
            )
        else:
            self.watch_remove_button.setToolTip("")

    def _system_member(self, symbol: str) -> bool:
        row = next((item for item in self._pool_rows if item.symbol == symbol), None)
        return bool(row and row.in_system_pool)

    def _save_splitter_sizes(self) -> None:
        if self.store.available:
            self.store.save_workbench_preference(
                "monitor_splitter_sizes", self.monitor_splitter.sizes()
            )

    def _toggle_compact_plan(self) -> None:
        if self.width() >= 1080:
            return
        self._right_forced_open = not self._right_forced_open
        self.plan_panel.setVisible(self._right_forced_open)
        self.stock_panel.setVisible(not self._right_forced_open)
        self.compact_plan_button.setText(
            "← 返回股票沙箱" if self._right_forced_open else "查看交易计划  →"
        )

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if not hasattr(self, "plan_panel"):
            return
        compact = self.width() < 1080
        self.compact_plan_button.setVisible(compact)
        if compact:
            self.plan_panel.setVisible(self._right_forced_open)
            self.stock_panel.setVisible(not self._right_forced_open)
        else:
            self._right_forced_open = False
            self.plan_panel.show()
            self.stock_panel.show()
            self.compact_plan_button.setText("查看交易计划  →")

    def _show_context_error(self, symbol: str, error: str) -> None:
        self.health_issue_button.setText("工作台数据故障")
        self.health_issue_button.setToolTip(f"{symbol}：{error}")


def _panel(title: str) -> tuple[QFrame, QVBoxLayout]:
    frame = _CollapsiblePanel()
    frame.setObjectName("workbenchPanel")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(11, 10, 11, 10)
    layout.setSpacing(8)
    label = QLabel(title)
    label.setObjectName("panelTitle")
    layout.addWidget(label)
    return frame, layout


def _table(headers: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    return table


def _fill_table(
    table: QTableWidget,
    rows: list[list[Any]],
    *,
    symbol_column: int | None = None,
) -> None:
    table.setUpdatesEnabled(False)
    table.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            item = QTableWidgetItem("" if value is None else str(value))
            item.setToolTip(item.text())
            if symbol_column is not None and c == symbol_column:
                item.setData(Qt.ItemDataRole.UserRole, str(value or ""))
            table.setItem(r, c, item)
    table.setUpdatesEnabled(True)


def _row_in_view(row: PoolRowViewModel, view: str) -> bool:
    if view == "ths_watchlist":
        return row.in_ths_watchlist
    if view == "watchlist":
        return row.in_personal_watchlist or row.forced_tracking
    return row.in_system_pool


def _row_in_status(row: PoolRowViewModel, status: str) -> bool:
    if status == "all":
        return True
    mapping = {
        "candidate": {"intraday_observing", "wait_confirmation"},
        "tradeable": {"quant_tradeable", "authorized", "waiting_user_confirmation"},
        "position": {"filled", "partially_filled"},
        "exit": {"exit_required"},
        "risk": {"major_risk_blocked", "account_risk_blocked", "invalidated", "data_incomplete"},
    }
    return row.state in mapping.get(status, set())


def _valid_bar(bar: dict[str, Any]) -> bool:
    try:
        return all(float(bar[key]) > 0 for key in ("open", "high", "low", "close"))
    except (KeyError, TypeError, ValueError):
        return False


def _state_color(state: str) -> str:
    if state in {"quant_tradeable", "authorized", "filled"}:
        return _GREEN
    if state in {"exit_required", "major_risk_blocked", "invalidated"}:
        return _RED
    if state in {"wait_confirmation", "data_incomplete", "account_risk_blocked"}:
        return _AMBER
    return _ACCENT


def _lifecycle_index(state: str) -> int:
    if state in {"daily_observing", "daily_rejected", "analysis_only", "data_incomplete"}:
        return 0
    if state in {"intraday_observing", "wait_confirmation", "quant_tradeable"}:
        return 1
    if state in {"account_risk_blocked", "authorized"}:
        return 2
    if state in {"waiting_user_confirmation", "submitted", "partially_filled"}:
        return 3
    if state == "filled":
        return 4
    if state in {"exit_required", "invalidated", "major_risk_blocked"}:
        return 5
    return 0


def _next_condition(state: str, blocks: list[Any]) -> str:
    if blocks:
        return "当前阻断：" + "；".join(str(item) for item in blocks[:3])
    return {
        "analysis_only": "下一步：继续观察；池外股票只有显式例外计划可以进入半风险通道。",
        "daily_observing": "下一步：等待收盘后的确定性日线扫描。",
        "daily_rejected": "下一步：等待下一交易日重新满足日线形态。",
        "intraday_observing": "下一步：等待已收盘15分钟K线达到70分。",
        "wait_confirmation": "下一步：等待第二根已收盘15分钟评分确认。",
        "quant_tradeable": "下一步：执行账户与组合风控；评分本身不等于订单授权。",
        "authorized": "下一步：强制同步账户事实并安全预填，最终确认由用户完成。",
        "waiting_user_confirmation": "下一步：在同花顺核对并由用户最终确认。",
        "filled": "下一步：按原计划跟踪止损、止盈和时间退出。",
        "exit_required": "下一步：核对T+1和真实可卖数量后处理退出。",
    }.get(state, "下一步：继续等待确定性状态更新。")


def _score_status(value: str) -> str:
    return {
        "data_incomplete": "数据不完整",
        "blocked": "已阻断",
        "observe": "继续观察",
        "wait_confirmation": "等待下一根确认",
        "eligible_for_risk": "可进入组合风控",
        "authorization_revoked": "授权已撤销",
    }.get(value, value or "状态未知")


def _number(value: Any) -> str:
    try:
        return f"{float(value):.3f}" if value is not None else "—"
    except (TypeError, ValueError):
        return "—"


def _money(value: Any) -> str:
    try:
        return f"¥ {float(value):,.2f}" if value is not None else "—"
    except (TypeError, ValueError):
        return "—"


def _short(value: str, length: int) -> str:
    return value if len(value) <= length else value[: length - 1] + "…"


def _time_only(value: str) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%m-%d %H:%M:%S")
    except ValueError:
        return _short(value, 19)


def _stylesheet() -> str:
    return f"""
    QWidget#stockPoolQuantWorkbench {{ background:#0d141b; color:{_TEXT};
        font-family:'Microsoft YaHei UI','Segoe UI'; font-size:12px; }}
    QFrame#workbenchNavigation, QFrame#globalHealthStrip, QFrame#workbenchPanel {{
        background:{_PANEL}; border:1px solid {_BORDER}; border-radius:7px; }}
    QLabel#quantBrand {{ color:{_ACCENT}; font-family:'Cascadia Mono','Consolas';
        font-size:11px; font-weight:700; letter-spacing:1px; }}
    QLabel#quantTitle {{ color:{_TEXT}; font-size:16px; font-weight:700; }}
    QPushButton#workspaceNavButton {{ background:transparent; border:none;
        padding:7px 12px; color:{_MUTED}; font-weight:600; }}
    QPushButton#workspaceNavButton:checked {{ color:{_TEXT}; background:#1b2a36;
        border-bottom:2px solid {_ACCENT}; }}
    QPushButton#returnToAnalysisButton {{ background:transparent; border:1px solid #3b4854;
        color:#b7c1ca; padding:6px 12px; }}
    QLabel#healthItem {{ color:#c5ced6; padding:3px 5px; }}
    QPushButton#healthIssueButton {{ background:#18242e; color:#b9c4cd;
        border:1px solid #32414e; padding:5px 9px; }}
    QPushButton#healthIssueButton[hasIssues="true"] {{ color:#f0c66b; border-color:#705b2d; }}
    QLabel#panelTitle {{ font-size:12px; font-weight:700; color:#dfe6ec;
        padding-bottom:3px; }}
    QLabel#sectionHint {{ color:{_MUTED}; font-size:11px; }}
    QLabel#selectedStockTitle {{ font-size:19px; font-weight:700; }}
    QLabel#selectedStockPrice {{ color:{_TEXT}; font-family:'Cascadia Mono','Consolas';
        font-size:21px; font-weight:700; }}
    QLabel#scoreCard, QLabel#accountMetric {{ background:{_SURFACE}; border:none;
        border-left:3px solid {_ACCENT}; padding:7px; color:#dce4ea;
        font-family:'Cascadia Mono','Consolas'; font-weight:600; }}
    QLabel#scoreSummary {{ background:#13242a; color:#67c9bd; padding:7px 9px;
        border:1px solid #28525a; }}
    QLabel#lifecycleStrip {{ background:#121c24; color:{_MUTED}; padding:8px;
        border:1px solid #24313c; }}
    QLabel#stageExplanation {{ color:#dce4ea; font-size:13px; font-weight:600; }}
    QLabel#nextCondition {{ background:#242012; color:#e3c77e; padding:8px;
        border-left:3px solid {_AMBER}; }}
    QLabel#workspaceBanner {{ background:#14202a; border-left:3px solid {_ACCENT};
        padding:10px 12px; color:#dce4ea; }}
    QLabel#inlineFeedback {{ background:#1c2832; color:#c8d2da; padding:7px;
        border-left:3px solid {_ACCENT}; }}
    QPushButton#primaryButton, QPushButton#primaryActionButton {{ background:#246f96;
        color:white; border:none; padding:8px 13px; font-weight:700; border-radius:4px; }}
    QPushButton#primaryButton:hover, QPushButton#primaryActionButton:hover {{ background:#2d84af; }}
    QPushButton#primaryButton:pressed, QPushButton#primaryActionButton:pressed {{
        background:#205f7f; }}
    QPushButton#tertiaryButton {{ background:transparent; border:1px solid #40505d;
        color:#bdc7cf; padding:7px; }}
    QPushButton#timeframeButton {{ background:transparent; color:{_MUTED};
        border:1px solid #34434f; padding:5px 10px; }}
    QPushButton#timeframeButton:checked {{ color:{_TEXT}; background:#1e3342;
        border-color:{_ACCENT}; }}
    QLineEdit, QComboBox {{ background:#101922; color:{_TEXT}; border:1px solid #33424f;
        padding:6px; selection-background-color:#275b77; }}
    QTreeWidget, QTableWidget, QTextBrowser {{ background:#0f171f; color:#d7dfe5;
        alternate-background-color:#121c25; border:1px solid #27343f;
        selection-background-color:#234b62; selection-color:white; }}
    QHeaderView::section {{ background:#17212a; color:#aeb9c3; border:none;
        border-right:1px solid #2c3945; padding:6px; font-weight:600; }}
    QTabWidget::pane {{ border:1px solid #293641; background:#0f171f; }}
    QTabBar::tab {{ background:#141e27; color:#8e9ba7; padding:7px 12px; }}
    QTabBar::tab:selected {{ color:#e6edf3; background:#1c2a35;
        border-bottom:2px solid {_ACCENT}; }}
    QSplitter::handle {{ background:#1d2a34; width:4px; }}
    QPushButton:focus, QLineEdit:focus, QComboBox:focus, QTreeWidget:focus,
    QTableWidget:focus {{ border:1px solid {_ACCENT}; }}
    """

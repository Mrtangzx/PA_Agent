"""Application-scoped runtime for deterministic quant data collection.

The quant workbench is a view over this coordinator.  Collection must continue
when that window has never been opened or has been closed.  This module never
prefills or submits an order; it only persists read-only broker snapshots,
deterministic inputs/scores, shadow plans, and lifecycle observations.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, time, timedelta
from typing import Any

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal

from pa_agent.trading.quant import SignalDecision
from pa_agent.trading.stock_sandbox import (
    StockSandboxState,
    StockTradingSandboxSnapshot,
    project_stock_sandboxes,
)
from pa_agent.trading.topdown import (
    MANUAL_EXCEPTION_STRATEGY_ID,
    TOPDOWN_SCORING_VERSION,
    TOPDOWN_STRATEGY_ID,
    TopDownScoreSnapshot,
)
from pa_agent.trading.universe import CLOUD_AI_AUTHORIZATION_SYMBOLS


class _UniverseWorker(QObject):
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, service: Any) -> None:
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

    def __init__(self, scanner: Any, pool_snapshot: dict[str, Any]) -> None:
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


class _ThsWatchlistScanWorker(QObject):
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, service: Any, *, force: bool) -> None:
        super().__init__()
        self.service = service
        self.force = force

    def run(self) -> None:
        try:
            report = self.service.scan(
                force=self.force,
                progress=lambda current, total, symbol: self.progress.emit(
                    current, total, symbol
                ),
            )
            self.finished.emit(report)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class _HotspotBatchWorker(QObject):
    snapshot_ready = pyqtSignal(object)
    failed = pyqtSignal(str, str)
    finished = pyqtSignal()

    def __init__(self, service: Any, symbols: list[str]) -> None:
        super().__init__()
        self.service = service
        self.symbols = symbols

    def run(self) -> None:
        for symbol in self.symbols:
            if QThread.currentThread().isInterruptionRequested():
                break
            try:
                self.snapshot_ready.emit(self.service.freeze(symbol))
            except Exception as exc:  # noqa: BLE001
                self.failed.emit(symbol, str(exc))
        self.finished.emit()


class _MarketSentimentWorker(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(
        self,
        service: Any,
        store: Any,
        captured_at: datetime,
    ) -> None:
        super().__init__()
        self.service = service
        self.store = store
        self.captured_at = captured_at

    def run(self) -> None:
        try:
            self.finished.emit(
                self.service.capture_for_store(
                    store=self.store,
                    captured_at=self.captured_at,
                )
            )
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class _MarketHistoryWorker(QObject):
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, service: Any, store: Any, captured_at: datetime) -> None:
        super().__init__()
        self.service = service
        self.store = store
        self.captured_at = captured_at

    def run(self) -> None:
        try:
            report = self.service.backfill(
                store=self.store,
                captured_at=self.captured_at,
                progress=lambda current, total, symbol: self.progress.emit(
                    current, total, symbol
                ),
                cancel_check=QThread.currentThread().isInterruptionRequested,
            )
            self.finished.emit(report)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class _TopDownBatchWorker(QObject):
    score_ready = pyqtSignal(object, object)
    failed = pyqtSignal(str, str)
    finished = pyqtSignal()

    def __init__(self, service: Any, jobs: list[dict[str, Any]]) -> None:
        super().__init__()
        self.service = service
        self.jobs = jobs

    def run(self) -> None:
        for job in self.jobs:
            if QThread.currentThread().isInterruptionRequested():
                break
            try:
                result = self.service.build_context(**job)
                score = self.service.evaluate(result)
                self.score_ready.emit(score, result.closed_stock_bar)
            except Exception as exc:  # noqa: BLE001
                self.failed.emit(job["symbol"], str(exc))
        self.finished.emit()


class _StockSelectionWorker(QObject):
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, service: Any, members: list[dict[str, Any]]) -> None:
        super().__init__()
        self.service = service
        self.members = members

    def run(self) -> None:
        try:
            snapshot = self.service.scan(
                extra_members=self.members,
                progress=lambda current, total, detail: self.progress.emit(
                    current, total, detail
                ),
                cancel_check=QThread.currentThread().isInterruptionRequested,
            )
            self.finished.emit(snapshot)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class _LifecycleDailySyncWorker(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, service: Any) -> None:
        super().__init__()
        self.service = service

    def run(self) -> None:
        try:
            self.finished.emit(self.service.sync_open_daily())
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class _OosMarketObservationWorker(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(
        self,
        service: Any,
        captured_at: datetime,
        monitor_universe: dict[str, Any],
    ) -> None:
        super().__init__()
        self.service = service
        self.captured_at = captured_at
        self.monitor_universe = monitor_universe

    def run(self) -> None:
        try:
            self.finished.emit(
                self.service.capture(
                    captured_at=self.captured_at,
                    monitor_universe=self.monitor_universe,
                )
            )
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class QuantRuntimeCoordinator(QObject):
    """Own long-lived quant collection independently of any workbench window."""

    updated = pyqtSignal()
    facts_updated = pyqtSignal(str, object, int)
    runtime_health_changed = pyqtSignal(object)
    broker_snapshot_changed = pyqtSignal(object)
    status_changed = pyqtSignal(str, str)
    task_failed = pyqtSignal(str, str)

    def __init__(
        self,
        ctx: Any,
        parent: QObject | None = None,
        *,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.store = ctx.trade_store
        self._now = now_provider or (lambda: datetime.now().astimezone())
        self.broker_snapshot: Any = None
        self.last_lifecycle_sync: dict[str, Any] = {}
        self._last_topdown_slot = ""
        self._last_broker_fact_gap_signature = ""
        self._active_reconciliation_plan_ids: set[str] = set()
        self._started = False
        self._stopping = False
        self._fact_revision = 0
        self._threads: dict[str, QThread | None] = {
            "universe": None,
            "daily_candidates": None,
            "ths_watchlist": None,
            "selection": None,
            "hotspots": None,
            "sentiment": None,
            "market_history": None,
            "topdown": None,
            "lifecycle": None,
            "oos_market": None,
        }
        self._workers: dict[str, QObject | None] = {key: None for key in self._threads}

        self._broker_timer = QTimer(self)
        self._broker_timer.timeout.connect(self.sync_broker)
        self._universe_timer = QTimer(self)
        self._universe_timer.timeout.connect(self.ensure_current_universe)
        self._daily_candidate_timer = QTimer(self)
        self._daily_candidate_timer.timeout.connect(self.ensure_daily_candidates)
        self._daily_candidate_timer.timeout.connect(self.ensure_ths_watchlist_scan)
        self._hotspot_timer = QTimer(self)
        self._hotspot_timer.timeout.connect(self.refresh_hotspots)
        self._stock_selection_timer = QTimer(self)
        self._stock_selection_timer.timeout.connect(self.ensure_stock_selection)
        self._topdown_timer = QTimer(self)
        self._topdown_timer.timeout.connect(self.refresh_topdown_scores)
        self._lifecycle_timer = QTimer(self)
        self._lifecycle_timer.timeout.connect(self.sync_daily_lifecycle)
        self._market_history_timer = QTimer(self)
        self._market_history_timer.timeout.connect(self.ensure_market_history)
        self._oos_market_timer = QTimer(self)
        self._oos_market_timer.timeout.connect(self.capture_oos_market_observations)
        self._last_oos_market_slot = ""
        self._oos_market_inflight_slot = ""
        self._oos_market_retry_not_before: datetime | None = None
        self._topdown_inflight_slot = ""
        self._topdown_retry_not_before: datetime | None = None
        self._market_history_auto_batches = 0
        self._market_history_auto_batch_limit = 12

    @property
    def started(self) -> bool:
        return self._started

    @property
    def active_tasks(self) -> tuple[str, ...]:
        """Expose read-only task activity so the workbench can report real progress."""
        return tuple(key for key in self._threads if self._task_active(key))

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stopping = False
        idle_seconds = max(5, int(self.ctx.settings.ths.idle_sync_seconds))
        hotspot_seconds = max(
            60, int(self.ctx.settings.topdown_scoring.hotspot_refresh_seconds)
        )
        self._broker_timer.start(idle_seconds * 1000)
        self._universe_timer.start(6 * 60 * 60 * 1000)
        self._daily_candidate_timer.start(5 * 60 * 1000)
        self._hotspot_timer.start(hotspot_seconds * 1000)
        selection_seconds = max(
            300, int(self.ctx.settings.stock_selection.refresh_seconds)
        )
        self._stock_selection_timer.start(selection_seconds * 1000)
        self._topdown_timer.start(10_000)
        self._lifecycle_timer.start(30 * 60 * 1000)
        self._market_history_timer.start(6 * 60 * 60 * 1000)
        self._oos_market_timer.start(10_000)
        self.ctx.logger.info(
            "常驻量化采集已启动: broker=%ss hotspot=%ss topdown=10s "
            "daily_candidates=300s lifecycle=1800s prefill=%s live=%s",
            idle_seconds,
            hotspot_seconds,
            bool(self.ctx.settings.ths.allow_prefill),
            bool(self.ctx.settings.portfolio_risk.live_trading_enabled),
        )
        self.status_changed.emit("runtime", "常驻量化采集已启动")
        QTimer.singleShot(0, self.sync_broker)
        QTimer.singleShot(500, self.ensure_current_universe)
        QTimer.singleShot(1500, self.ensure_daily_candidates)
        QTimer.singleShot(6500, self.ensure_ths_watchlist_scan)
        QTimer.singleShot(2000, self.refresh_hotspots)
        QTimer.singleShot(8000, self.ensure_stock_selection)
        QTimer.singleShot(2500, self.refresh_topdown_scores)
        QTimer.singleShot(3200, self.sync_daily_lifecycle)
        QTimer.singleShot(4000, self.ensure_market_history)
        QTimer.singleShot(4500, self.capture_oos_market_observations)

    def stop(self) -> None:
        if not self._started and not any(self._thread_running(k) for k in self._threads):
            return
        self._stopping = True
        for timer in (
            self._broker_timer,
            self._universe_timer,
            self._daily_candidate_timer,
            self._hotspot_timer,
            self._stock_selection_timer,
            self._topdown_timer,
            self._lifecycle_timer,
            self._market_history_timer,
            self._oos_market_timer,
        ):
            timer.stop()
        for key, thread in list(self._threads.items()):
            if thread is None:
                continue
            try:
                if thread.isRunning():
                    thread.requestInterruption()
                    thread.quit()
                    if not thread.wait(5000):
                        self.ctx.logger.warning("量化后台线程退出超时: %s", key)
            except RuntimeError:
                pass
            self._threads[key] = None
            self._workers[key] = None
        self._started = False
        self.ctx.logger.info("常驻量化采集已停止")
        self.status_changed.emit("runtime", "常驻量化采集已停止")

    def _thread_running(self, key: str) -> bool:
        thread = self._threads.get(key)
        if thread is None:
            return False
        try:
            return thread.isRunning()
        except RuntimeError:
            self._threads[key] = None
            self._workers[key] = None
            return False

    def _task_active(self, key: str) -> bool:
        """Deduplicate a task until its queued completion releases ownership."""
        thread = self._threads.get(key)
        worker = self._workers.get(key)
        if thread is None or worker is None:
            return False
        try:
            thread.objectName()
        except RuntimeError:
            self._threads[key] = None
            self._workers[key] = None
            return False
        return True

    def _hold_thread(self, key: str, thread: QThread, worker: QObject) -> None:
        self._threads[key] = thread
        self._workers[key] = worker
        thread.finished.connect(lambda key=key: self._release_thread(key))

    def _release_thread(self, key: str) -> None:
        self._threads[key] = None
        self._workers[key] = None

    def ensure_stock_selection(self, *, force: bool = False) -> None:
        """Refresh the A-share discovery board without creating trade plans."""
        service = getattr(self.ctx, "stock_selection_service", None)
        if self._stopping or service is None or self._task_active("selection"):
            return
        latest = self.store.latest_stock_selection_snapshot()
        if latest and not force:
            try:
                generated = datetime.fromisoformat(
                    str(latest.get("generated_at") or "").replace("Z", "+00:00")
                )
                if generated.tzinfo is None:
                    generated = generated.replace(tzinfo=self._now().tzinfo)
                age = (self._now() - generated.astimezone(self._now().tzinfo)).total_seconds()
                if age < int(self.ctx.settings.stock_selection.refresh_seconds):
                    return
            except ValueError:
                pass
        members: list[dict[str, Any]] = []
        universes = self.store.list_universe_snapshots(limit=1)
        if universes:
            members.extend(
                dict(item)
                for item in (universes[0].get("snapshot") or {}).get("members") or []
            )
        members.extend(
            {
                "symbol": item.get("symbol"),
                "name": item.get("name"),
                **dict(item.get("metadata") or {}),
            }
            for item in self.store.list_watchlist_members(active_only=True)
        )
        thread = QThread(self)
        worker = _StockSelectionWorker(service, members)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(
            lambda current, total, detail: self.status_changed.emit(
                "selection", f"正在选股 {current}/{total} · {detail}"
            )
        )
        worker.finished.connect(self._stock_selection_finished)
        worker.failed.connect(lambda error: self._fail("selection", error))
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._hold_thread("selection", thread, worker)
        self.status_changed.emit(
            "selection", "正在扫描全A候选种子并核验重大负面公告"
        )
        thread.start()

    def _stock_selection_finished(self, snapshot: Any) -> None:
        self.store.add_stock_selection_snapshot(snapshot)
        detail = (
            f"智能选股完成：扫描{snapshot.scanned_count}只种子，"
            f"入选{snapshot.candidate_count}只；重大负面股票已剔除"
        )
        if snapshot.data_gaps:
            detail += f"；{len(snapshot.data_gaps)}项数据源异常"
        self.status_changed.emit("selection", detail)
        self.updated.emit()
        self._emit_facts("selection")

    def sync_broker(self) -> Any:
        """Persist one read-only broker snapshot; never touches trading controls."""
        adapter = getattr(self.ctx, "broker_adapter", None)
        if adapter is None or self._stopping:
            return None
        try:
            snapshot = adapter.snapshot()
            self.broker_snapshot = snapshot
            if self.store.available:
                self.store.add_broker_snapshot(snapshot)
                if snapshot.complete and snapshot.total_equity is not None:
                    self.store.record_broker_financial_snapshot(snapshot)
                self._refresh_linked_broker_orders(snapshot)
                self._record_external_manual_fills(snapshot)
            self._report_broker_snapshot_health(snapshot)
            self.broker_snapshot_changed.emit(snapshot)
            self.refresh_stock_sandboxes()
            self.ctx.logger.info(
                "常驻量化只读同步完成: status=%s complete=%s positions=%s "
                "orders=%s fills=%s",
                snapshot.connection.status.value,
                snapshot.complete,
                len(snapshot.positions),
                len(snapshot.orders),
                len(snapshot.fills),
            )
            self.updated.emit()
            self._emit_facts("broker")
            return snapshot
        except Exception as exc:  # noqa: BLE001
            self._fail("broker", exc)
            return None

    def set_broker_snapshot(self, snapshot: Any) -> None:
        self.broker_snapshot = snapshot
        self._report_broker_snapshot_health(snapshot)
        self.broker_snapshot_changed.emit(snapshot)
        self.refresh_stock_sandboxes()
        self.updated.emit()
        self._emit_facts("broker")

    def begin_reconciliation(self, plan_id: str) -> None:
        """Prevent a just-prefilled order from being classified as manual."""
        if plan_id:
            self._active_reconciliation_plan_ids.add(plan_id)

    def end_reconciliation(self, plan_id: str) -> None:
        self._active_reconciliation_plan_ids.discard(plan_id)

    def _refresh_linked_broker_orders(self, snapshot: Any) -> None:
        from pa_agent.brokers.ths_adapter import broker_fact_snapshot_gaps
        from pa_agent.trading.broker_models import ReconciliationResult

        gaps = broker_fact_snapshot_gaps(
            snapshot, binding=self.ctx.settings.ths
        )
        if gaps:
            return

        for link in self.store.list_broker_order_links(
            account_fingerprint=snapshot.account_fingerprint
        ):
            broker_order = next(
                (
                    item
                    for item in snapshot.orders
                    if item.broker_order_id == link["broker_order_id"]
                ),
                None,
            )
            if broker_order is None:
                continue
            fills = [
                item
                for item in snapshot.fills
                if item.broker_order_id == broker_order.broker_order_id
            ]
            self.store.link_broker_order(
                ReconciliationResult(
                    status="matched",
                    plan_id=link["plan_id"],
                    matched_order_ids=[broker_order.broker_order_id],
                    matched_fill_ids=[
                        item.broker_fill_id for item in fills if item.broker_fill_id
                    ],
                    message="周期同步更新已关联委托成交",
                ),
                account_fingerprint=snapshot.account_fingerprint,
                details={
                    **link.get("details", {}),
                    "order": broker_order.model_dump(mode="json"),
                    "periodic_sync": True,
                },
            )
            status, event_type = self.ctx.broker_trade_lifecycle.broker_order_status(
                broker_order.status,
                broker_order.filled_quantity,
                broker_order.quantity,
            )
            self.store.upsert_broker_execution(
                plan_id=link["plan_id"],
                fills=fills,
                plan_status="executed_open" if status == "filled" else status,
                event_type=event_type,
                broker_order_id=broker_order.broker_order_id,
                account_fingerprint=snapshot.account_fingerprint,
            )

    def _record_external_manual_fills(self, snapshot: Any) -> None:
        from pa_agent.brokers.ths_adapter import broker_fact_snapshot_gaps

        gaps = broker_fact_snapshot_gaps(
            snapshot, binding=self.ctx.settings.ths
        )
        if gaps:
            return
        if self._active_reconciliation_plan_ids:
            return
        linked = self.store.linked_broker_fill_ids(
            account_fingerprint=snapshot.account_fingerprint
        )
        from pa_agent.trading.hotspot_risk import pending_reconciliation_order_ids

        protected_order_ids = pending_reconciliation_order_ids(self.store, snapshot)
        for fill in snapshot.fills:
            if not fill.broker_fill_id or fill.broker_fill_id in linked:
                continue
            if fill.broker_order_id in protected_order_ids:
                continue
            if self.store.add_external_broker_trade(
                fill, account_fingerprint=snapshot.account_fingerprint
            ):
                self.ctx.logger.info(
                    "EXTERNAL_MANUAL_TRADE %s %s %s",
                    fill.broker_fill_id,
                    fill.symbol,
                    fill.direction,
                )

    def _report_broker_snapshot_health(self, snapshot: Any) -> None:
        """Report account-fact health only when the blocking state changes."""
        from pa_agent.brokers.ths_adapter import broker_fact_snapshot_gaps

        gaps = broker_fact_snapshot_gaps(
            snapshot, binding=self.ctx.settings.ths
        )
        signature = "|".join(sorted(str(item) for item in gaps))
        if signature == self._last_broker_fact_gap_signature:
            return
        previous = self._last_broker_fact_gap_signature
        self._last_broker_fact_gap_signature = signature
        if gaps:
            self.ctx.logger.warning(
                "同花顺账户事实暂不可用，委托成交同步保持关闭: %s", gaps
            )
            message = str(getattr(snapshot.connection, "message", "") or "").strip()
            self.status_changed.emit(
                "broker",
                message or "账户事实不完整，实盘与预填保持关闭",
            )
            return
        if previous:
            self.ctx.logger.info("同花顺账户事实已恢复可信，委托成交同步恢复")
        self.status_changed.emit("broker", "资金、持仓、委托和成交事实已同步")

    def ensure_current_universe(self, *, force: bool = False) -> None:
        if self._stopping or not self.store.available:
            return
        service = getattr(self.ctx, "universe_service", None)
        if service is None or self._thread_running("universe"):
            return
        current_version = (
            service.current_version(self._now())
            if hasattr(service, "current_version")
            else f"hs300-{self._now():%Y-%m}"
        )
        if not force and any(
            item.get("version") == current_version
            and item.get("data_complete")
            for item in self.store.list_universe_snapshots(limit=3)
        ):
            self.refresh_stock_sandboxes()
            self.ensure_daily_candidates()
            return
        thread = QThread(self)
        worker = _UniverseWorker(service)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(
            lambda current, total, symbol: self.status_changed.emit(
                "universe", f"正在校验 {current}/{total}：{symbol}"
            )
        )
        worker.finished.connect(self._universe_generated)
        worker.failed.connect(lambda error: self._fail("universe", error))
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._hold_thread("universe", thread, worker)
        self.status_changed.emit("universe", "正在生成或刷新当前私人A股股票池")
        thread.start()

    def _universe_generated(self, snapshot: Any) -> None:
        self.store.upsert_universe_snapshot(
            snapshot,
            source_updated_at=(
                snapshot.source_as_of.isoformat() if snapshot.source_as_of else ""
            ),
            data_complete=snapshot.data_complete,
        )
        registry = getattr(self.ctx, "validation_epochs", None)
        if registry is not None:
            registry.activate(snapshot)
        recorder = getattr(self.ctx, "oos_observation_recorder", None)
        if recorder is not None:
            recorder.record_universe(snapshot)
        if snapshot.data_complete:
            detail = f"{snapshot.version} 已生成：{len(snapshot.symbols)}只"
        else:
            detail = "数据不完整，禁止授权：" + ", ".join(
                snapshot.completeness_reasons
            )
        self.status_changed.emit("universe", detail)
        self.refresh_stock_sandboxes()
        self.updated.emit()
        QTimer.singleShot(0, self.ensure_daily_candidates)

    def universe_revision_committed(self, snapshot: Any) -> None:
        """Activate a user-managed revision already committed by its service."""
        registry = getattr(self.ctx, "validation_epochs", None)
        if registry is not None:
            registry.activate(snapshot)
        recorder = getattr(self.ctx, "oos_observation_recorder", None)
        if recorder is not None:
            recorder.record_universe(snapshot)
        self.status_changed.emit(
            "universe",
            f"{snapshot.change_summary or snapshot.version}：当前 {len(snapshot.symbols)}只",
        )
        self.refresh_stock_sandboxes()
        self.updated.emit()
        QTimer.singleShot(0, self.ensure_daily_candidates)
        QTimer.singleShot(0, self.refresh_hotspots)
        QTimer.singleShot(0, self.refresh_topdown_scores)

    def ensure_daily_candidates(self) -> None:
        if self._stopping or not self.store.available:
            return
        scanner = getattr(self.ctx, "daily_candidate_scanner", None)
        if scanner is None or self._task_active("daily_candidates"):
            return
        universes = self.store.list_universe_snapshots(limit=1)
        if not universes or not universes[0].get("data_complete"):
            return
        pool = universes[0]["snapshot"]
        if not pool.get("symbols"):
            self.status_changed.emit(
                "daily_candidates",
                "当前私人A股股票池为空；请先新增股票，系统不会生成交易机会",
            )
            self.refresh_stock_sandboxes()
            self.updated.emit()
            return
        today = self._now()
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
        thread = QThread(self)
        worker = _DailyCandidateWorker(scanner, pool)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(
            lambda current, total, symbol: self.status_changed.emit(
                "daily_candidates", f"正在扫描 {current}/{total}：{symbol}"
            )
        )
        worker.finished.connect(self._daily_candidates_finished)
        worker.failed.connect(lambda error: self._fail("daily_candidates", error))
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._hold_thread("daily_candidates", thread, worker)
        self.status_changed.emit(
            "daily_candidates",
            f"正在扫描 {pool.get('version', '')} 的收盘后日线候选",
        )
        thread.start()

    def _daily_candidates_finished(self, result: Any) -> None:
        for decision in result.decisions:
            self.store.add_quant_signal(decision)
        if result.data_complete:
            detail = (
                f"{result.pool_version} 扫描{len(result.decisions)}只，"
                f"候选{len(result.allowed)}只，信号日{result.signal_date}"
            )
        else:
            detail = "数据不完整，禁止授权：" + ", ".join(result.data_gaps)
        self.status_changed.emit("daily_candidates", detail)
        self.refresh_stock_sandboxes()
        self.updated.emit()
        QTimer.singleShot(0, self.refresh_hotspots)
        QTimer.singleShot(0, self.refresh_topdown_scores)
        QTimer.singleShot(0, self.ensure_ths_watchlist_scan)

    def ensure_ths_watchlist_scan(self, *, force: bool = False) -> None:
        """Mirror all TongHuaShun A-share categories and scan them once per input."""
        service = getattr(self.ctx, "ths_watchlist_service", None)
        if (
            self._stopping
            or service is None
            or not self.store.available
            or self._task_active("ths_watchlist")
        ):
            return
        thread = QThread(self)
        worker = _ThsWatchlistScanWorker(service, force=force)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(
            lambda current, total, symbol: self.status_changed.emit(
                "ths_watchlist",
                f"正在扫描同花顺自选 {current}/{total}：{symbol}",
            )
        )
        worker.finished.connect(self._ths_watchlist_scan_finished)
        worker.failed.connect(lambda error: self._fail("ths_watchlist", error))
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._hold_thread("ths_watchlist", thread, worker)
        self.status_changed.emit("ths_watchlist", "正在同步并扫描同花顺全部A股自选分类")
        thread.start()

    def _ths_watchlist_scan_finished(self, report: Any) -> None:
        if report.data_complete:
            detail = (
                f"同花顺自选扫描完成：{report.total}只，"
                f"下个交易日候选{report.next_session_candidates}只，"
                f"当前完整放行{report.actionable}只"
            )
        else:
            detail = "同花顺自选扫描数据不完整：" + "，".join(report.data_gaps)
        self.status_changed.emit("ths_watchlist", detail)
        self.refresh_stock_sandboxes()
        self.updated.emit()
        self._emit_facts("ths_watchlist")
        QTimer.singleShot(0, self.refresh_hotspots)
        QTimer.singleShot(0, self.refresh_topdown_scores)

    def refresh_hotspots(self) -> None:
        service = getattr(self.ctx, "hotspot_service", None)
        if (
            self._stopping
            or service is None
            or not self.store.available
            or self._task_active("hotspots")
        ):
            return
        universes = self.store.list_universe_snapshots(limit=1)
        if not universes or not universes[0].get("data_complete"):
            self.status_changed.emit(
                "hotspots",
                "股票池数据不完整，热点与重大公告监控未启动；禁止新增交易",
            )
            return
        pool = universes[0].get("snapshot") or {}
        pool_symbols = {
            str(symbol)
            for symbol in pool.get("symbols") or []
            if str(symbol).strip()
        }
        if not pool_symbols:
            self.status_changed.emit(
                "hotspots",
                "股票池没有有效成员，热点与重大公告监控未启动；禁止新增交易",
            )
            return
        symbols = set(pool_symbols)
        if hasattr(self.store, "list_watchlist_members"):
            symbols.update(
                str(item.get("symbol") or "")
                for item in self.store.list_watchlist_members(active_only=True)
                if str(item.get("symbol") or "")
            )
        symbols.update({
            item["symbol"]
            for item in self.store.list_plans(lifecycle_open=True)
            if item.get("symbol")
        })
        symbols.update(
            item["symbol"]
            for item in self.store.list_quant_signals(limit=1000)
            if item.get("status") == "allow" and item.get("symbol")
        )
        if self.broker_snapshot is not None:
            symbols.update(item.symbol for item in self.broker_snapshot.positions)
        if not symbols:
            return
        epoch = self._current_validation_epoch()
        batch_epoch_id = epoch.epoch_id if epoch is not None else ""
        batch_pool_version = str(pool.get("version") or "")
        thread = QThread(self)
        worker = _HotspotBatchWorker(service, sorted(symbols))
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.snapshot_ready.connect(
            lambda snapshot,
            epoch_id=batch_epoch_id,
            pool_version=batch_pool_version: self._store_hotspot_snapshot(
                snapshot,
                expected_epoch_id=epoch_id,
                expected_pool_version=pool_version,
            )
        )
        worker.failed.connect(lambda symbol, error: self._fail(f"hotspot:{symbol}", error))
        worker.finished.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._hotspots_finished)
        self._hold_thread("hotspots", thread, worker)
        self.status_changed.emit(
            "hotspots",
            f"正在监控{len(symbols)}只股票的热点与重大公告（含完整股票池）",
        )
        thread.start()

    def _hotspots_finished(self) -> None:
        self.status_changed.emit("hotspots", "热点与重大公告监控已更新")
        self.refresh_stock_sandboxes()
        self.updated.emit()

    def _store_hotspot_snapshot(
        self,
        snapshot: Any,
        *,
        expected_epoch_id: str = "",
        expected_pool_version: str = "",
    ) -> None:
        registry = getattr(self.ctx, "validation_epochs", None)
        if registry is not None:
            snapshot = registry.bind_hotspot(
                snapshot,
                expected_epoch_id=expected_epoch_id,
                expected_pool_version=expected_pool_version,
            )
        self.store.add_hotspot_snapshot(snapshot)
        recorder = getattr(self.ctx, "oos_observation_recorder", None)
        if recorder is not None:
            recorder.record_hotspot(snapshot)
        from pa_agent.trading.hotspot_risk import apply_major_hotspot_risk

        apply_major_hotspot_risk(
            store=self.store,
            snapshot=snapshot,
            broker_adapter=getattr(self.ctx, "broker_adapter", None),
        )

    def refresh_topdown_scores(self, *, now: datetime | None = None) -> None:
        service = getattr(self.ctx, "topdown_market_data_service", None)
        if (
            self._stopping
            or service is None
            or not self.store.available
            or self.broker_snapshot is None
            or self._thread_running("topdown")
            or self._thread_running("sentiment")
        ):
            return
        now = now or self._now()
        from pa_agent.trading.topdown_market_data import expected_topdown_bar_close

        expected_close = expected_topdown_bar_close(now)
        if expected_close is None:
            return
        slot = expected_close.isoformat()
        if (
            slot == self._last_topdown_slot
            or slot == self._topdown_inflight_slot
            or (
                self._topdown_retry_not_before is not None
                and now < self._topdown_retry_not_before
            )
        ):
            return
        if self._oos_slot_symbols("market_sentiment", expected_close) == {""}:
            self._last_topdown_slot = slot
            return
        universes = self.store.list_universe_snapshots(limit=1)
        if not universes or not universes[0].get("data_complete"):
            return
        self._topdown_inflight_slot = slot
        # Freeze sentiment at the exact bar close, not at the scheduler's
        # second/microsecond.  The exporter requires one unambiguous snapshot
        # for every scoring slot.
        self._capture_market_sentiment(universes[0]["snapshot"], expected_close)

    def _capture_market_sentiment(
        self, universe: dict[str, Any], now: datetime
    ) -> None:
        service = getattr(self.ctx, "market_sentiment_service", None)
        if service is None:
            self._build_topdown_jobs(universe, now, None)
            return
        thread = QThread(self)
        worker = _MarketSentimentWorker(service, self.store, now)
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
        self._hold_thread("sentiment", thread, worker)
        thread.start()

    def ensure_market_history(self, *, force: bool = False) -> None:
        """Fill the real 21-session market baseline without blocking the UI."""
        service = getattr(self.ctx, "market_history_service", None)
        if (
            self._stopping
            or service is None
            or not self.store.available
            or self._thread_running("market_history")
        ):
            return
        if force:
            self._market_history_auto_batches = 0
        recent = self.store.list_validation_runs(
            strategy_version="market_sentiment_history_v1",
            limit=1,
        )
        if not force and recent:
            report = recent[0].get("report") or {}
            finished = str(report.get("finished_at") or "")
            if (
                report.get("status") == "complete"
                and finished[:10] == self._now().date().isoformat()
            ):
                return
        thread = QThread(self)
        worker = _MarketHistoryWorker(service, self.store, self._now())
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(
            lambda current, total, symbol: self.status_changed.emit(
                "market_history",
                f"正在补齐本批真实全A历史 {current}/{total}：{symbol}；可继续使用其他功能",
            )
        )
        worker.finished.connect(self._market_history_finished)
        worker.failed.connect(lambda error: self._fail("market_history", error))
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._hold_thread("market_history", thread, worker)
        self.status_changed.emit(
            "market_history",
            "正在准备真实全A 21个交易日的新高/新低基线",
        )
        thread.start()

    def _market_history_finished(self, report: Any) -> None:
        self.store.add_validation_run(
            report,
            dataset="market_history_backfill",
            promotion_eligible=False,
        )
        if report.status == "complete":
            self._market_history_auto_batches = 0
            detail = (
                f"真实全A历史基线已就绪：{report.requested_sessions}日，"
                f"{report.completed_symbols}/{report.universe_count}只完整"
            )
        else:
            self._market_history_auto_batches += 1
            minimum_coverage = min(report.coverage_by_date.values(), default=0)
            priority_symbol_count = int(
                getattr(report, "priority_symbol_count", 0) or 0
            )
            priority_completed_symbols = int(
                getattr(report, "priority_completed_symbols", 0) or 0
            )
            priority_expected_count = int(
                getattr(report, "priority_expected_count", 0) or 0
            )
            priority_incomplete = (
                priority_symbol_count > 0
                and priority_completed_symbols < priority_symbol_count
            )
            priority_source_incomplete = (
                priority_expected_count > 0
                and priority_symbol_count != priority_expected_count
            )
            should_continue = (
                not self._stopping
                and report.processed_symbols > 0
                and report.newly_completed_symbols > 0
                and report.remaining_symbols > 0
                and (
                    minimum_coverage < 3000
                    or priority_incomplete
                    or priority_source_incomplete
                )
                and self._market_history_auto_batches
                < self._market_history_auto_batch_limit
            )
            detail = (
                f"本批已处理{report.processed_symbols}只；累计完整"
                f"{report.completed_symbols}/{report.universe_count}只，"
                f"沪深300依赖{priority_completed_symbols}/"
                f"{priority_symbol_count}只，剩余{report.remaining_symbols}只；"
                "评分仍阻断，"
                + (
                    "20秒后自动续传下一批"
                    if should_continue
                    else "可点击补齐按钮继续续传"
                )
            )
            if should_continue:
                QTimer.singleShot(20_000, self.ensure_market_history)
        self.status_changed.emit("market_history", detail)
        self.updated.emit()

    def capture_oos_market_observations(self, *, now: datetime | None = None) -> None:
        service = getattr(self.ctx, "oos_market_observation_service", None)
        if self._stopping or service is None or self._thread_running("oos_market"):
            return
        now = (now or self._now()).astimezone()
        from pa_agent.trading.topdown_market_data import expected_oos_market_close

        expected = expected_oos_market_close(now)
        if expected is None:
            return
        daily_recovery_only = (
            expected.time() == time(15, 0)
            and now - expected > timedelta(minutes=5)
        )
        universes = self.store.list_universe_snapshots(limit=1)
        if not universes or not universes[0].get("data_complete"):
            return
        monitor_universe = dict(universes[0].get("snapshot") or {})
        monitor_symbols = _pool_monitor_symbols(monitor_universe)
        monitor_universe["monitor_symbols"] = sorted(monitor_symbols)
        slot = expected.isoformat()
        if (
            slot == self._last_oos_market_slot
            or slot == self._oos_market_inflight_slot
            or (
                self._oos_market_retry_not_before is not None
                and now < self._oos_market_retry_not_before
            )
        ):
            return
        epoch = self._current_validation_epoch()
        authorization_symbols = (
            set(epoch.authorization_symbols)
            if epoch is not None else set(CLOUD_AI_AUTHORIZATION_SYMBOLS)
        )
        # OOS completeness follows the current validation epoch. Analysis-only
        # members remain under hotspot monitoring but do not block executable
        # stock + four-index coverage.
        required_symbols = set(getattr(service, "INDEXES", ())) | set(
            authorization_symbols
        )
        intraday_symbols = self._oos_slot_symbols("intraday_15m", expected)
        daily_symbols = self._oos_slot_symbols("daily_bars", expected)
        daily_complete = expected.time() != time(15, 0) or daily_symbols == required_symbols
        monitor_complete = self._pool_monitor_slot_symbols(
            str(monitor_universe.get("version") or ""), expected
        ) == monitor_symbols
        if (
            daily_complete
            and (
                daily_recovery_only
                or (
                    intraday_symbols == required_symbols
                    and monitor_complete
                )
            )
        ):
            self._last_oos_market_slot = slot
            return
        self._oos_market_inflight_slot = slot
        thread = QThread(self)
        worker = _OosMarketObservationWorker(service, now, monitor_universe)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._oos_market_observations_finished)
        worker.failed.connect(self._oos_market_observations_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._hold_thread("oos_market", thread, worker)
        self.status_changed.emit(
            "oos_market",
            f"正在保存 {expected:%H:%M} 的当前验证纪元与四指数OOS原始K线",
        )
        thread.start()

    def _oos_market_observations_finished(self, report: dict[str, Any]) -> None:
        slot = str(report.get("bar_closed_at") or self._oos_market_inflight_slot)
        self._oos_market_inflight_slot = ""
        failures = list(report.get("failures") or [])
        monitor_failures = list(report.get("monitor_failures") or [])
        oos_complete = report.get("status") == "complete" and not failures
        monitor_complete = report.get("monitor_status", "not_requested") in {
            "complete", "not_requested",
        } and not monitor_failures
        complete = oos_complete and monitor_complete
        if complete:
            self._last_oos_market_slot = slot
            self._oos_market_retry_not_before = None
        else:
            self._oos_market_retry_not_before = self._now() + timedelta(seconds=60)
        daily_recovery_only = bool(report.get("daily_recovery_only"))
        detail = (
            f"已恢复{report.get('captured', 0)}/{report.get('required', 0)}"
            "条15:00日线；过期15分钟与情绪数据不回填"
            if daily_recovery_only
            else (
                f"已核验{report.get('captured', 0)}/{report.get('required', 0)}"
                "条真实OOS原始K线"
            )
        )
        monitor_required = int(report.get("monitor_required") or 0)
        if monitor_required:
            detail += (
                f"；当前股票池15分钟监控"
                f"{report.get('monitor_captured', 0)}/{monitor_required}只"
            )
        if not oos_complete:
            detail += f"；{len(failures)}项缺失，保持不可导出"
        if not monitor_complete:
            detail += f"；{len(monitor_failures)}只股票监控数据缺失"
        if not complete:
            detail += "；当前评分窗口内60秒后重试"
        self.status_changed.emit("oos_market", detail)
        self.refresh_stock_sandboxes()
        self.updated.emit()

    def _oos_market_observations_failed(self, error: str) -> None:
        self._oos_market_inflight_slot = ""
        self._oos_market_retry_not_before = self._now() + timedelta(seconds=60)
        self._fail("oos_market", error)

    def _oos_slot_symbols(self, kind: str, slot: datetime) -> set[str]:
        if not self.store.available:
            return set()
        epoch = self._current_validation_epoch()
        strategy_version = (
            epoch.observation_strategy_version
            if epoch is not None else TOPDOWN_STRATEGY_ID
        )
        return {
            str(row.get("symbol") or "")
            for row in self.store.list_oos_observations(
                strategy_version=strategy_version,
                kind=kind,
                since=slot.isoformat(),
                limit=100,
            )
            if str(row.get("effective_at") or "") == slot.isoformat()
        }

    def _current_validation_epoch(self):
        registry = getattr(self.ctx, "validation_epochs", None)
        if registry is None:
            recorder = getattr(self.ctx, "oos_observation_recorder", None)
            registry = getattr(recorder, "validation_epochs", None)
        return registry.require_current() if registry is not None else None

    def _pool_monitor_slot_symbols(
        self,
        pool_version: str,
        slot: datetime,
    ) -> set[str]:
        if not self.store.available or not pool_version:
            return set()
        from pa_agent.trading.oos_observations import pool_monitor_strategy_version

        return {
            str(row.get("symbol") or "")
            for row in self.store.list_oos_observations(
                strategy_version=pool_monitor_strategy_version(pool_version),
                kind="intraday_15m",
                since=slot.isoformat(),
                limit=1000,
            )
            if str(row.get("effective_at") or "") == slot.isoformat()
        }

    def _sentiment_captured(
        self, universe: dict[str, Any], now: datetime, snapshot: Any
    ) -> None:
        self.store.add_market_sentiment_snapshot(snapshot)
        recorder = getattr(self.ctx, "oos_observation_recorder", None)
        if recorder is not None:
            recorder.record_sentiment(snapshot)
        slot = self._topdown_inflight_slot
        self._topdown_inflight_slot = ""
        if snapshot.data_complete:
            self._last_topdown_slot = slot or str(snapshot.captured_at)
            self._topdown_retry_not_before = None
        else:
            self._topdown_retry_not_before = self._now() + timedelta(seconds=60)
        sentiment = snapshot.input if snapshot.data_complete else None
        self._build_topdown_jobs(universe, now, sentiment)

    def _sentiment_failed(
        self, universe: dict[str, Any], now: datetime, error: str
    ) -> None:
        self._topdown_inflight_slot = ""
        self._topdown_retry_not_before = self._now() + timedelta(seconds=60)
        self._fail("sentiment", error)
        self._build_topdown_jobs(universe, now, None)

    def _build_topdown_jobs(
        self, universe: dict[str, Any], now: datetime, sentiment: Any
    ) -> None:
        service = getattr(self.ctx, "topdown_market_data_service", None)
        if service is None or self._stopping:
            return
        candidates = _topdown_scoring_candidates(
            self.store.list_quant_signals(limit=1000),
            universe=universe,
            baseline_strategy_id=self.ctx.settings.strategy.strategy_id,
            now=now,
        )

        jobs: list[dict[str, Any]] = []
        for signal, scoring_pool in candidates:
            symbol = signal.symbol
            hotspot_record = self.store.latest_hotspot_snapshot(symbol)
            hotspot = None
            if hotspot_record:
                from pa_agent.trading.topdown import HotspotSnapshot

                candidate_hotspot = HotspotSnapshot.model_validate(
                    hotspot_record["snapshot"]
                )
                epoch = self._current_validation_epoch()
                if (
                    epoch is None
                    or not epoch.is_private_pool
                    or (
                        candidate_hotspot.validation_epoch_id == epoch.epoch_id
                        and candidate_hotspot.member_hash == epoch.member_hash
                        and candidate_hotspot.pool_version in epoch.pool_versions
                    )
                ):
                    hotspot = candidate_hotspot
            theme_metrics = (
                self.ctx.hotspot_service.theme_metrics(hotspot)
                if hotspot is not None and getattr(self.ctx, "hotspot_service", None)
                else None
            )
            previous_record = self.store.latest_topdown_score(
                symbol,
                strategy_version=TOPDOWN_STRATEGY_ID,
                scoring_version=TOPDOWN_SCORING_VERSION,
                pool_version=signal.pool_version,
            )
            previous = (
                TopDownScoreSnapshot.model_validate(previous_record["snapshot"])
                if previous_record
                else None
            )
            jobs.append(
                {
                    "symbol": symbol,
                    "daily_signal": signal,
                    "pool_snapshot": scoring_pool,
                    "broker": self.broker_snapshot,
                    "hotspot": hotspot,
                    "previous_score": previous,
                    "sentiment": sentiment,
                    "theme_metrics": theme_metrics,
                    "captured_at": now,
                    "authorization_open": any(
                        item.get("symbol") == symbol
                        and (item.get("risk_snapshot") or {}).get("pool_version")
                        == signal.pool_version
                        and item.get("status")
                        in {
                            "awaiting_user_confirmation",
                            "submitted",
                            "partially_filled",
                        }
                        for item in self.store.list_plans(symbol=symbol)
                    ),
                }
            )
        if not jobs:
            self.updated.emit()
            return
        thread = QThread(self)
        worker = _TopDownBatchWorker(service, jobs)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.score_ready.connect(self._store_topdown_score)
        worker.failed.connect(lambda symbol, error: self._fail(f"topdown:{symbol}", error))
        worker.finished.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._topdown_finished)
        self._hold_thread("topdown", thread, worker)
        self.status_changed.emit("topdown", f"正在计算{len(jobs)}只日线候选的15分钟评分")
        thread.start()

    def _topdown_finished(self) -> None:
        self.status_changed.emit("topdown", "最新闭合15分钟四层评分已更新")
        self.refresh_stock_sandboxes()
        self.updated.emit()

    def _store_topdown_score(self, score: Any, closed_stock_bar: Any = None) -> None:
        if closed_stock_bar is not None:
            # Production OOS bars are captured independently for the entire
            # executable universe.  Writing a second, signal-conditioned copy
            # here would make the append-only slot ambiguous once a candidate
            # exists and could bias the exported dataset.
            self.ctx.trade_lifecycle.process_closed_bar(
                symbol=score.symbol,
                timeframe="15m",
                bar=closed_stock_bar,
            )
        self.store.add_topdown_score(score)
        if score.eligible_for_risk and not score.hard_blocks and not score.data_gaps:
            record = next(
                (
                    item
                    for item in self.store.list_quant_signals(limit=1000)
                    if item.get("symbol") == score.symbol
                    and item.get("pool_version") == score.pool_version
                    and item.get("status") == "allow"
                    and item.get("strategy_id") in {
                        self.ctx.settings.strategy.strategy_id,
                        MANUAL_EXCEPTION_STRATEGY_ID,
                    }
                ),
                None,
            )
            if record is not None:
                daily = SignalDecision.model_validate(record["decision"])
                self.ctx.quant_workflow.create_topdown_plan(daily, score)
        if score.status.value == "authorization_revoked":
            from pa_agent.trading.authorization_risk import (
                apply_topdown_authorization_revocation,
            )

            apply_topdown_authorization_revocation(
                store=self.store,
                score=score,
                broker_adapter=getattr(self.ctx, "broker_adapter", None),
            )
        self.refresh_stock_sandboxes()

    def refresh_stock_sandboxes(self) -> list[StockTradingSandboxSnapshot]:
        """Project and persist one isolated real-time state for every pool stock."""
        if self._stopping or not self.store.available:
            return []
        universes = self.store.list_universe_snapshots(limit=1)
        if not universes:
            return []
        universe_record = universes[0]
        universe = dict(universe_record.get("snapshot") or {})
        universe["data_complete"] = bool(universe_record.get("data_complete"))
        symbols = [str(item) for item in universe.get("symbols") or [] if str(item)]
        watchlist = (
            self.store.list_watchlist_members(active_only=True)
            if hasattr(self.store, "list_watchlist_members") else []
        )
        member_symbols = {
            str(item.get("symbol") or "")
            for item in universe.get("members") or []
        }
        for watched in watchlist:
            symbol = str(watched.get("symbol") or "")
            if not symbol or symbol in symbols:
                continue
            metadata = dict(watched.get("metadata") or {})
            metadata.update({
                "symbol": symbol,
                "name": str(watched.get("name") or metadata.get("name") or symbol),
                "authorization_eligible": bool(
                    metadata.get("manual_exception_eligible", False)
                ),
                "eligibility_reasons": (
                    []
                    if metadata.get("manual_exception_eligible", False)
                    else ["outside_system_pool_manual_exception_required"]
                ),
                "theme": str(
                    metadata.get("theme")
                    or "、".join(metadata.get("ths_categories") or [])
                    or "用户关注"
                ),
            })
            symbols.append(symbol)
            if symbol not in member_symbols:
                universe.setdefault("members", []).append(metadata)
                member_symbols.add(symbol)
        universe["symbols"] = symbols
        if not symbols:
            return []
        hotspots = {
            symbol: record
            for symbol in symbols
            if (record := self.store.latest_hotspot_snapshot(symbol)) is not None
        }
        plans = self._plans_with_latest_authorization(str(universe.get("version") or ""))
        snapshots = project_stock_sandboxes(
            universe=universe,
            # Include manual_exception_4321_v1 signals for pool-external
            # TongHuaShun/personal watchlist stocks.  The projector itself
            # restricts them to the current base_pool_version.
            signals=self.store.list_quant_signals(limit=max(500, len(symbols) * 8)),
            scores=self.store.list_topdown_scores(
                strategy_version=TOPDOWN_STRATEGY_ID,
                scoring_version=TOPDOWN_SCORING_VERSION,
                limit=max(500, len(symbols) * 8),
            ),
            plans=plans,
            hotspots=hotspots,
            latest_prices=self._latest_pool_prices(
                symbols, str(universe.get("version") or "")
            ),
            broker_snapshot=self.broker_snapshot,
            observed_at=self._now().isoformat(),
        )
        for snapshot in snapshots:
            self.store.upsert_stock_sandbox(snapshot)
            if self._trade_opportunity_notification_allowed(snapshot):
                self._notify_quant_tradeable(snapshot)
            elif snapshot.state is StockSandboxState.EXIT_REQUIRED:
                self._notify_quant_exit(snapshot)
        self.updated.emit()
        self._emit_facts("sandboxes")
        return snapshots

    def _trade_opportunity_notification_allowed(
        self, snapshot: StockTradingSandboxSnapshot
    ) -> bool:
        """Notify only at the authorization layer appropriate to the mode."""
        state = self.store.current_strategy_state(TOPDOWN_STRATEGY_ID)
        live_enabled = bool(self.ctx.settings.portfolio_risk.live_trading_enabled)
        if live_enabled:
            return (
                state in {"active", "reduced"}
                and snapshot.state is StockSandboxState.AUTHORIZED
                and snapshot.account_risk_status == "authorized"
            )
        return state == "shadow" and snapshot.state is StockSandboxState.QUANT_TRADEABLE

    def _emit_facts(self, scope: str, symbol: str | None = None) -> None:
        """Emit a monotonic, fine-grained invalidation for the new workbench."""
        self._fact_revision += 1
        self.facts_updated.emit(str(scope), symbol, self._fact_revision)
        self.runtime_health_changed.emit({
            "started": self._started,
            "stopping": self._stopping,
            "active_tasks": list(self.active_tasks),
            "revision": self._fact_revision,
        })

    def _plans_with_latest_authorization(
        self, pool_version: str
    ) -> list[dict[str, Any]]:
        plans = self.store.list_plans(limit=2000)
        for plan in plans:
            if str(plan.get("strategy_version") or "") not in {
                TOPDOWN_STRATEGY_ID,
                MANUAL_EXCEPTION_STRATEGY_ID,
            }:
                continue
            if str((plan.get("risk_snapshot") or {}).get("pool_version") or "") != pool_version:
                continue
            events = self.store.list_events(str(plan.get("id") or ""))
            authorization = next(
                (
                    item.get("details") or {}
                    for item in reversed(events)
                    if item.get("event_type") == "risk_authorization"
                ),
                None,
            )
            if authorization is None:
                continue
            risk = dict(plan.get("risk_snapshot") or {})
            risk["authorization_status"] = str(
                authorization.get("status") or ""
            )
            risk["authorization_reasons"] = list(
                authorization.get("reasons") or []
            )
            plan["risk_snapshot"] = risk
        return plans

    def _latest_pool_prices(
        self,
        symbols: list[str],
        pool_version: str,
    ) -> dict[str, float]:
        """Return the newest frozen 15-minute close for every monitored stock."""
        from pa_agent.trading.oos_observations import pool_monitor_strategy_version

        wanted = set(symbols)
        result: dict[str, float] = {}
        observations = self.store.list_oos_observations(
            strategy_version=pool_monitor_strategy_version(pool_version),
            kind="intraday_15m",
            limit=max(1000, len(symbols) * 128),
            descending=True,
        )
        for item in observations:
            symbol = str(item.get("symbol") or "")
            if symbol not in wanted:
                continue
            if symbol in result:
                continue
            value = (item.get("payload") or {}).get("close")
            try:
                result[symbol] = float(value)
            except (TypeError, ValueError):
                continue
        return result

    def _notify_quant_tradeable(
        self, snapshot: StockTradingSandboxSnapshot
    ) -> None:
        """Claim and asynchronously deliver one state-transition notification."""
        feishu = getattr(self.ctx.settings, "feishu", None)
        if (
            feishu is None
            or not bool(getattr(feishu, "enabled", True))
            or not str(getattr(feishu, "webhook_url", "") or "").strip()
            or not snapshot.plan_id
        ):
            return
        score_record = self.store.latest_topdown_score(
            snapshot.symbol,
            strategy_version=TOPDOWN_STRATEGY_ID,
            scoring_version=TOPDOWN_SCORING_VERSION,
            pool_version=snapshot.pool_version,
        )
        score = (score_record or {}).get("snapshot") or {}
        bar_closed_at = str(score.get("bar_closed_at") or "")
        event_key = "|".join((
            str(snapshot.plan_id),
            "trade_opportunity",
            str(snapshot.input_hash),
        ))
        claimed = self.store.claim_quant_notification(
            event_key=event_key,
            symbol=snapshot.symbol,
            event_type="trade_opportunity",
            bar_closed_at=bar_closed_at,
            plan_id=snapshot.plan_id,
            details={
                "input_hash": snapshot.input_hash,
                "total_score": snapshot.total_score,
                "mode": (
                    "live"
                    if bool(self.ctx.settings.portfolio_risk.live_trading_enabled)
                    else "shadow"
                ),
            },
            retry_failed=True,
            max_attempts=3,
            retry_after_seconds=60,
            recover_pending_after_seconds=300,
        )
        if not claimed:
            return
        self.status_changed.emit(
            "feishu", f"{snapshot.name} {snapshot.symbol} 已达到量化可交易，正在发送提醒"
        )

        def _send() -> None:
            delivered = False
            error = ""
            try:
                from pa_agent.notify.feishu_notifier import send_quant_tradeable_signal

                delivered = send_quant_tradeable_signal(
                    sandbox=snapshot,
                    settings=self.ctx.settings,
                )
            except Exception as exc:  # noqa: BLE001
                error = str(exc)
                self.ctx.logger.warning(
                    "飞书量化提醒失败 %s: %s", snapshot.symbol, exc
                )
            self.store.finish_quant_notification(
                event_key,
                delivered=delivered,
                details={
                    "input_hash": snapshot.input_hash,
                    "total_score": snapshot.total_score,
                    "error": error,
                },
            )
            detail = (
                f"{snapshot.name} {snapshot.symbol} 提醒已发送"
                if delivered
                else f"{snapshot.name} {snapshot.symbol} 提醒发送失败，量化状态不受影响"
            )
            self.status_changed.emit("feishu", detail)
            self.updated.emit()

        threading.Thread(
            target=_send,
            name=f"quant-feishu-{snapshot.symbol}",
            daemon=True,
        ).start()

    def _notify_quant_exit(self, snapshot: StockTradingSandboxSnapshot) -> None:
        """Deliver one bounded exit-state alert; ordinary runtime faults stay in UI."""
        feishu = getattr(self.ctx.settings, "feishu", None)
        if (
            feishu is None
            or not bool(getattr(feishu, "enabled", True))
            or not str(getattr(feishu, "webhook_url", "") or "").strip()
            or not snapshot.plan_id
        ):
            return
        event_key = "|".join((
            str(snapshot.plan_id),
            "exit_required",
            str(snapshot.input_hash),
        ))
        if not self.store.claim_quant_notification(
            event_key=event_key,
            symbol=snapshot.symbol,
            event_type="exit_required",
            plan_id=snapshot.plan_id,
            details={"state_revision": snapshot.input_hash},
            retry_failed=True,
            max_attempts=3,
            retry_after_seconds=60,
            recover_pending_after_seconds=300,
        ):
            return

        def _send() -> None:
            delivered = False
            error = ""
            try:
                from pa_agent.notify.feishu_notifier import send_quant_exit_signal

                delivered = send_quant_exit_signal(
                    sandbox=snapshot,
                    settings=self.ctx.settings,
                )
            except Exception as exc:  # noqa: BLE001
                error = str(exc)
                self.ctx.logger.warning("飞书量化退出提醒失败 %s: %s", snapshot.symbol, exc)
            self.store.finish_quant_notification(
                event_key,
                delivered=delivered,
                details={"state_revision": snapshot.input_hash, "error": error},
            )
            self.status_changed.emit(
                "feishu",
                (
                    f"{snapshot.name} {snapshot.symbol} 退出提醒已发送"
                    if delivered else
                    f"{snapshot.name} {snapshot.symbol} 退出提醒发送失败"
                ),
            )
            self.updated.emit()

        threading.Thread(
            target=_send,
            name=f"quant-exit-feishu-{snapshot.symbol}",
            daemon=True,
        ).start()

    def sync_daily_lifecycle(self) -> None:
        lifecycle = getattr(self.ctx, "trade_lifecycle", None)
        if (
            self._stopping
            or not self.store.available
            or lifecycle is None
            or self._thread_running("lifecycle")
        ):
            return
        from pa_agent.trading.lifecycle_sync import LifecycleMarketDataSync

        thread = QThread(self)
        worker = _LifecycleDailySyncWorker(
            LifecycleMarketDataSync(self.store, lifecycle)
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._daily_lifecycle_synced)
        worker.failed.connect(lambda error: self._fail("lifecycle", error))
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._hold_thread("lifecycle", thread, worker)
        thread.start()

    def _daily_lifecycle_synced(self, result: Any) -> None:
        self.last_lifecycle_sync = result if isinstance(result, dict) else {}
        failures = self.last_lifecycle_sync.get("failures") or {}
        if failures:
            self.ctx.logger.warning("开放计划日线同步部分失败: %s", failures)
        self.status_changed.emit("lifecycle", "持仓与退出状态已同步")
        self.refresh_stock_sandboxes()
        self.updated.emit()

    def _fail(self, task: str, error: Any) -> None:
        message = str(error)
        self.ctx.logger.warning("量化常驻任务失败 %s: %s", task, message)
        self.task_failed.emit(task, message)
        self.status_changed.emit(task, f"失败关闭：{message}")
        self._emit_facts(f"error:{task}")
        self.updated.emit()


def _signal_is_active_at(signal: SignalDecision, now: datetime) -> bool:
    """Reject stale/future daily signals before any intraday data is fetched."""
    try:
        signal_time = datetime.fromisoformat(signal.signal_time).astimezone()
        valid_until = datetime.fromisoformat(signal.valid_until).astimezone()
    except (TypeError, ValueError):
        return False
    current = now.astimezone()
    return signal_time <= current <= valid_until


def _pool_monitor_symbols(universe: dict[str, Any]) -> set[str]:
    """Select executable pool members while keeping analysis-only rows isolated."""
    symbols = {
        str(symbol) for symbol in universe.get("symbols") or [] if str(symbol)
    }
    members = {
        str(item.get("symbol") or ""): item
        for item in universe.get("members") or []
        if isinstance(item, dict) and str(item.get("symbol") or "")
    }
    return {
        symbol
        for symbol in symbols
        if symbol not in members
        or bool(members[symbol].get("authorization_eligible", True))
    }


def _topdown_scoring_candidates(
    records: list[dict[str, Any]],
    *,
    universe: dict[str, Any],
    baseline_strategy_id: str,
    now: datetime,
) -> list[tuple[SignalDecision, dict[str, Any]]]:
    """Select current base-pool and manual-exception signals without mutation."""
    base_pool_version = str(universe.get("version") or "")
    base_symbols = {
        str(symbol) for symbol in universe.get("symbols") or [] if str(symbol)
    }
    latest_base: dict[str, SignalDecision] = {}
    latest_manual: dict[str, SignalDecision] = {}
    for record in records:
        if record.get("status") != "allow":
            continue
        try:
            signal = SignalDecision.model_validate(record["decision"])
        except Exception:  # noqa: BLE001
            continue
        if not _signal_is_active_at(signal, now):
            continue
        symbol = str(signal.symbol)
        if (
            signal.strategy_id == baseline_strategy_id
            and signal.pool_version == base_pool_version
            and symbol in base_symbols
        ):
            latest_base.setdefault(symbol, signal)
            continue
        if (
            signal.strategy_id == MANUAL_EXCEPTION_STRATEGY_ID
            and symbol not in base_symbols
            and str(signal.condition_snapshot.get("base_pool_version") or "")
            == base_pool_version
        ):
            latest_manual.setdefault(symbol, signal)

    candidates: list[tuple[SignalDecision, dict[str, Any]]] = []
    for symbol in sorted(base_symbols):
        signal = latest_base.get(symbol)
        if signal is not None:
            candidates.append((signal, universe))
    for symbol in sorted(latest_manual):
        signal = latest_manual[symbol]
        snapshot = signal.condition_snapshot
        candidates.append((signal, {
            "version": signal.pool_version,
            "as_of": str(signal.signal_time)[:10],
            "symbols": [symbol],
            "members": [{
                "symbol": symbol,
                "name": str(snapshot.get("expected_security_name") or symbol),
                "industry": str(snapshot.get("industry") or ""),
                "authorization_eligible": True,
            }],
            "data_complete": True,
            "source_kind": "manual_exception_read_only_context",
            "base_pool_version": base_pool_version,
        }))
    return candidates
